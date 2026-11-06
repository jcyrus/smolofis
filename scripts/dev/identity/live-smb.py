"""Synthetic live-session tests; server control is fixed to the lab's Alice UID."""
import json
import os
from pathlib import Path
import pty
import pwd
import re
import select
import signal
import subprocess
import sys
import tempfile
import time


def fail(message):
    raise SystemExit('FAIL: ' + message)


def status():
    result = subprocess.check_output(['smbstatus', '--json'], timeout=10)
    document = json.loads(result)
    if not document.get('version', '').startswith('4.22.11-'):
        fail('untested Samba session schema')
    return document


def disconnect():
    alice_uid = pwd.getpwnam('LAB\\alice').pw_uid
    master = int(Path('/run/samba/smbd.pid').read_text())
    before = status()['sessions']
    targets = {int(value['server_id']['pid']): value['server_id']['unique_id']
               for value in before.values() if value['uid'] == alice_uid}
    if not targets:
        fail('no synthetic Alice session exists to revoke')
    closed = 0
    for pid, unique_id in targets.items():
        if pid <= 1 or pid == master:
            fail('refusing to signal the file-server master')
        with_pid = [value for value in before.values() if int(value['server_id']['pid']) == pid]
        if any(value['uid'] != alice_uid for value in with_pid):
            fail('worker also hosts a different user; no collateral disconnect allowed')
        fd = os.pidfd_open(pid)
        try:
            # smbd workers may be non-dumpable, making /proc/PID/exe unreadable
            # without ptrace privileges. Validate their kernel parent/name instead.
            process_stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            if (int(process_stat[1]) != master
                    or not Path(f'/proc/{pid}/comm').read_text().strip().startswith('smbd')):
                fail('worker is not a child of the file-server master')
            current = list(status()['sessions'].values())
            if any(int(value['server_id']['pid']) == pid and value['uid'] != alice_uid for value in current):
                fail('worker now hosts another user; refusing collateral disconnect')
            if not any(value['uid'] == alice_uid and int(value['server_id']['pid']) == pid
                       and value['server_id']['unique_id'] == unique_id for value in current):
                fail('worker session changed during validation')
            # pidfd pins the checked process, so a reused numeric PID cannot be targeted.
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            closed += 1
        finally:
            os.close(fd)
    for _ in range(50):
        if not any(int(value['server_id']['pid']) in targets for value in status()['sessions'].values()):
            break
        time.sleep(0.1)
    else:
        fail('target workers did not close')
    if int(Path('/run/samba/smbd.pid').read_text()) != master:
        fail('file-server master changed during targeted revocation')
    print(json.dumps({'closed_workers': closed, 'master_preserved': True}), flush=True)


def client(user):
    if user not in ('alice', 'reader'):
        fail('unsupported synthetic user')
    passwords = json.loads(Path('/lab/passwords.json').read_text())
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        auth = root / 'auth'
        auth.write_text(f'username = {user}\ndomain = LAB\npassword = {passwords[user]}\n')
        auth.chmod(0o600)
        master, slave = pty.openpty()
        child = subprocess.Popen(['smbclient', '//files/projects', '-A', str(auth)],
                                 stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        remote = 'live-session-' + os.urandom(8).hex() + '.txt'
        payload = root / 'payload'
        payload.write_bytes(os.urandom(64))

        def prompt(command=None):
            if command is not None:
                if child.poll() is not None:
                    return False
                os.write(master, (command + '\n').encode())
            data = b''
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        block = os.read(master, 8192)
                    except OSError:
                        return False
                    if not block:
                        return False
                    data += block
                    if re.search(rb'smb: [^\r\n]*>\s*$', data):
                        return b'NT_STATUS_' not in data
                if child.poll() is not None:
                    return False
            return False

        def probe():
            if user == 'reader':
                return prompt('ls')
            downloaded = root / ('download-' + os.urandom(6).hex())
            allowed = prompt(f'get {remote} {downloaded}')
            return allowed and downloaded.is_file() and downloaded.read_bytes() == payload.read_bytes()

        try:
            if not prompt():
                fail('persistent SMB client did not authenticate')
            if user == 'alice' and not prompt(f'put {payload} {remote}'):
                fail('could not create a synthetic shared file')
            print(json.dumps({'ready': probe()}), flush=True)
            for line in sys.stdin:
                action = line.strip()
                if action == 'probe':
                    print(json.dumps({'allowed': probe()}), flush=True)
                elif action == 'finish':
                    break
                else:
                    fail('unknown live-session request')
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
            os.close(master)
            if user == 'alice':
                # The host restores the synthetic account/group before finishing.
                subprocess.run(['smbclient', '//files/projects', '-A', str(auth), '-c', f'del {remote}'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def host():
    compose = ['docker', 'compose', '-f', str(Path(__file__).resolve().parent / 'compose.yml')]

    def run(*args):
        result = subprocess.run(compose + list(args), capture_output=True, timeout=30)
        if result.returncode:
            # These fixed control commands contain no passwords or auth files.
            detail = result.stderr.decode(errors='replace').strip().splitlines()
            fail('fixed lab control ' + ' '.join(args[:5]) + ' failed: ' + (detail[-1] if detail else 'no diagnostic'))
        return result.stdout

    def control(action):
        run('exec', '-T', 'dc', 'python3', '/usr/local/bin/lab-control', action)

    def group(action):
        run('exec', '-T', 'dc', 'samba-tool', 'group', action, 'Engineering', 'alice')

    def receive(process):
        if not select.select([process.stdout], [], [], 25)[0]:
            fail('live SMB client timed out')
        line = process.stdout.readline()
        if not line:
            fail('live SMB client exited unexpectedly')
        return json.loads(line)

    def start(user):
        process = subprocess.Popen(compose + [
            'run', '--rm', '-T', '--no-deps', 'verify', 'live-smb', user,
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            if not receive(process).get('ready'):
                fail('live SMB initial access failed')
        except BaseException:
            process.terminate()
            process.communicate(timeout=10)
            raise
        return process

    def probe(process):
        process.stdin.write('probe\n')
        process.stdin.flush()
        return receive(process)['allowed']

    observations = {}
    for scenario in ('account-disable', 'group-removal'):
        alice = reader = None
        removed = False
        try:
            alice = start('alice')
            reader = start('reader')
            if scenario == 'account-disable':
                control('disable')
            else:
                group('removemembers')
                removed = True
                run('exec', '-T', 'files', 'net', 'cache', 'flush')
            observations[scenario + '-old-session-before-disconnect'] = probe(alice)
            result = json.loads(run('exec', '-T', 'files', 'python3',
                                    '/usr/local/bin/lab-live-smb', 'disconnect'))
            if not result['closed_workers'] or not result['master_preserved'] or probe(alice):
                fail('Alice retained access after targeted worker revocation')
            if not probe(reader):
                fail('targeted revocation interrupted the unrelated reader session')
            print('PASS: ' + scenario + ' revokes existing Alice access and preserves the live reader', flush=True)
        finally:
            if removed:
                group('addmembers')
                run('exec', '-T', 'files', 'net', 'cache', 'flush')
            control('enable')
            for process in (alice, reader):
                if process:
                    try:
                        process.stdin.write('finish\n')
                        process.stdin.flush()
                        process.communicate(timeout=20)
                    except (BrokenPipeError, subprocess.TimeoutExpired):
                        process.terminate()
                        process.communicate(timeout=10)
    run('exec', '-T', 'files', 'python3', '-c',
        'import json; from pathlib import Path; Path("/evidence/live-smb-report.json").write_text(json.dumps(' +
        repr({**observations, 'targeted_revocation_passed': True, 'unrelated_session_preserved': True}) + '))')
    print('PASS: synthetic account and group membership restored', flush=True)


if __name__ == '__main__':
    if sys.argv[1] == 'host':
        host()
    elif sys.argv[1] == 'disconnect':
        disconnect()
    elif sys.argv[1] == 'client':
        client(sys.argv[2])
    else:
        fail('unknown mode')
