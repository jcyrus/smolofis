"""Lab-only Linux x86_64 child syscall fault tracer; never installed in product images."""
import ctypes
import os
import signal

libc = ctypes.CDLL(None, use_errno=True)
libc.ptrace.restype = ctypes.c_long

def trace(request, pid, address=0, data=0):
    result = libc.ptrace(request, pid, ctypes.c_void_p(address), data)
    if result == -1:
        raise OSError(ctypes.get_errno(), 'ptrace')
    return result


def environment(log):
    env = {k: v for k, v in os.environ.items() if not k.startswith('SMOLOFIS_')}
    env['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(log)
    return env


def traced(binary, state, log, fail=None, kill=None, after=False, args=None, password=None):
    out, err = state.parent / 'stdout', state.parent / 'stderr'
    input_fd = None
    if password is not None:
        assert len(password) <= 1024
        input_fd, writer = os.pipe()
        os.write(writer, password); os.close(writer)
    pid = os.fork()
    if pid == 0:
        if input_fd is not None:
            os.dup2(input_fd, 0); os.close(input_fd)
        os.dup2(os.open(out, os.O_WRONLY | os.O_CREAT, 0o600), 1)
        os.dup2(os.open(err, os.O_WRONLY | os.O_CREAT, 0o600), 2)
        trace(0, 0)
        os.kill(os.getpid(), signal.SIGSTOP)
        os.execve(binary, [str(binary), *(args if args is not None else ['--auth-lock', str(state)])], environment(log))
    if input_fd is not None:
        os.close(input_fd)
    reaped = False
    try:
        _, status = os.waitpid(pid, 0); assert os.WIFSTOPPED(status)
        trace(0x4200, pid, 0, 0x100001)  # TRACESYSGOOD | EXITKILL
        entering, injected, count = True, False, 0
        current_sync = False
        observed = []
        while True:
            trace(24, pid)
            _, status = os.waitpid(pid, 0)
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                reaped = True
                return os.waitstatus_to_exitcode(status), count, observed, out.read_bytes(), err.read_bytes()
            stop = os.WSTOPSIG(status)
            if stop != signal.SIGTRAP | 0x80:
                assert stop == signal.SIGTRAP
                continue
            regs = (ctypes.c_ulonglong * 27)(); trace(12, pid, 0, ctypes.byref(regs))
            if entering:
                current_sync = regs[15] in (74, 75)  # x86_64 fsync / fdatasync
                if current_sync:
                    count += 1
                    observed.append(os.readlink(f'/proc/{pid}/fd/{regs[14]}'))
                    if count == kill and not after:
                        os.kill(pid, signal.SIGKILL)
                        _, killed = os.waitpid(pid, 0); reaped = True
                        assert os.WIFSIGNALED(killed)
                        return os.waitstatus_to_exitcode(killed), count, observed, out.read_bytes(), err.read_bytes()
                    if count == fail:
                        regs[15] = (1 << 64) - 1
                        trace(13, pid, 0, ctypes.byref(regs)); injected = True
            else:
                if injected:
                    regs[10] = (1 << 64) - 5
                    trace(13, pid, 0, ctypes.byref(regs)); injected = False
                if current_sync and count == kill and after:
                    assert regs[10] == 0
                    os.kill(pid, signal.SIGKILL)
                    _, killed = os.waitpid(pid, 0); reaped = True
                    assert os.WIFSIGNALED(killed)
                    return os.waitstatus_to_exitcode(killed), count, observed, out.read_bytes(), err.read_bytes()
            entering = not entering
    finally:
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
            except ProcessLookupError:
                pass
