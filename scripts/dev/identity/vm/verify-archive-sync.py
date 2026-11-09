"""Linux x86_64 ptrace fault injection; never loaded by product code."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import subprocess
import sys
import time

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
BINARY = Path('/opt/smolofis-panel-audit-lab/smolofis-panel')
if (os.geteuid() != 0 or platform.machine() != 'x86_64'
        or not Path('/etc/smolofis-m0-fullvm').exists() or not (ROOT / 'domain-ready').exists()
        or Path('/etc/smolofis-release').exists() or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel x86_64 VM required')
os.umask(0o077)
libc = ctypes.CDLL(None, use_errno=True)
libc.ptrace.restype = ctypes.c_long

def trace(request, pid, address=0, data=0):
    result = libc.ptrace(request, pid, ctypes.c_void_p(address), data)
    if result == -1:
        raise OSError(ctypes.get_errno(), 'ptrace')
    return result

def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def fixture(folder):
    folder.mkdir(mode=0o700)
    source, destination = folder / 'events.jsonl', folder / 'archives'
    destination.mkdir(mode=0o700)
    source.write_bytes(b'power-history\n' * 2000)
    source.chmod(0o600)
    with source.open('rb') as stream:
        os.fsync(stream.fileno())
    sync_directory(destination)
    sync_directory(folder)
    sync_directory(folder.parent)
    return source, destination

def run_traced(source, destination, fail_at=None, pause_at=None, ready=None):
    output = source.parent / 'stdout'
    errors = source.parent / 'stderr'
    pid = os.fork()
    if pid == 0:
        os.dup2(os.open(output, os.O_WRONLY | os.O_CREAT, 0o600), 1)
        os.dup2(os.open(errors, os.O_WRONLY | os.O_CREAT, 0o600), 2)
        trace(0, 0)  # PTRACE_TRACEME
        os.kill(os.getpid(), signal.SIGSTOP)
        os.execv(BINARY, [str(BINARY), '--auth-audit-archive', str(source), str(destination)])
    try:
        _, status = os.waitpid(pid, 0)
        assert os.WIFSTOPPED(status)
        trace(0x4200, pid, 0, 1)  # PTRACE_O_TRACESYSGOOD
        entering, injected, syncs = True, False, 0
        while True:
            trace(24, pid)  # PTRACE_SYSCALL
            _, status = os.waitpid(pid, 0)
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                return os.waitstatus_to_exitcode(status), syncs, output.read_bytes(), errors.read_bytes()
            if os.WSTOPSIG(status) != signal.SIGTRAP | 0x80:
                assert os.WSTOPSIG(status) == signal.SIGTRAP
                continue
            registers = (ctypes.c_ulonglong * 27)()
            trace(12, pid, 0, ctypes.byref(registers))
            if entering and registers[15] == 74:  # x86_64 fsync
                syncs += 1
                if syncs == pause_at:
                    print(json.dumps(ready), flush=True)
                    while True:
                        time.sleep(1)  # host kills only the owned QEMU container
                if syncs == fail_at:
                    registers[15] = 0xffffffffffffffff  # invalid syscall, skip real fsync
                    trace(13, pid, 0, ctypes.byref(registers))
                    injected = True
            elif not entering and injected:
                registers[10] = (1 << 64) - 5  # -EIO
                trace(13, pid, 0, ctypes.byref(registers))
                injected = False
            entering = not entering
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except ProcessLookupError:
            pass

mode = sys.argv[1] if len(sys.argv) > 1 else 'sync'
if mode == 'recover':
    directory = Path(sys.argv[2])
    assert directory.parent == ROOT and directory.name.startswith('archive-power-')
    plan = json.loads((directory / 'plan.json').read_text())
    source, destination = directory / 'events.jsonl', directory / 'archives'
    content = source.read_bytes()
    snapshots = list(destination.glob('*.jsonl'))
    assert len(snapshots) == 1 and hashlib.sha256(snapshots[0].read_bytes()).hexdigest() == plan['sha256']
    manifest = json.loads(snapshots[0].with_suffix('.manifest.json').read_text())
    assert manifest['sha256'] == plan['sha256'] and manifest['status'] == 'snapshot_saved'
    assert content == b'' or hashlib.sha256(content).hexdigest() == plan['sha256']
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert boot != plan['boot_id']
    assert source.stat().st_mode & 0o777 == 0o600 and snapshots[0].stat().st_mode & 0o777 == 0o400
    report = dict(plan, recovered_boot_id=boot, live_log='empty' if not content else 'original',
                  archive_intact=True, production_modified=False)
    (directory / 'report.json').write_text(json.dumps(report, indent=2))
    print('PASS: abrupt QEMU termination after rename preserves synced snapshot and a valid live log across boot; ' + str(directory))
elif mode == 'power':
    directory = ROOT / ('archive-power-' + secrets.token_hex(5))
    source, destination = fixture(directory)
    plan = {'directory': str(directory), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
            'pause': 'after rename, before parent directory fsync'}
    with (directory / 'plan.json').open('w') as stream:
        json.dump(plan, stream)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(directory)
    sync_directory(ROOT)
    run_traced(source, destination, pause_at=5, ready=plan)
else:
    assert mode == 'sync'
    directory = ROOT / ('archive-sync-' + secrets.token_hex(5))
    directory.mkdir(mode=0o700)
    checks = []
    for phase in range(1, 6):
        source, destination = fixture(directory / str(phase))
        original = source.read_bytes()
        code, count, output, error = run_traced(source, destination, fail_at=phase)
        assert code != 0 and count == phase and b'os error 5' in error, (phase, code, count, error)
        assert not output
        snapshots = list(destination.glob('*.jsonl'))
        assert len(snapshots) == 1 and snapshots[0].read_bytes() == original
        if phase < 5:
            assert source.read_bytes() == original
        else:
            assert source.read_bytes() == b'' and b'replaced but directory sync failed' in error
        checks.append('fsync EIO phase ' + str(phase))
        print('PASS: ' + checks[-1] + ' retains recoverable history and reports failure', flush=True)
    report = {'checks': checks, 'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
              'production_modified': False, 'injection': 'ptrace fsync syscall return -EIO'}
    (directory / 'report.json').write_text(json.dumps(report, indent=2))
    print('PASS: five real Linux sync syscall fault checks; evidence retained at ' + str(directory), flush=True)
