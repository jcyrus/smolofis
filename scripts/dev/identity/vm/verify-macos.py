"""Native macOS SMB mounts through a loopback-only tunnel to the restored lab."""
import errno
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

@contextmanager
def mount_directory():
    directory = Path(tempfile.mkdtemp(prefix='m0-native-mac-', dir=lab.STATE))
    try:
        yield directory
    finally:
        # Never recursively remove a mount or files on a remote share.
        for path in (directory / 'share', directory):
            try:
                path.rmdir()
            except OSError:
                pass

spec = importlib.util.spec_from_file_location('m0_lab', Path(__file__).with_name('lab.py'))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
lab.PORT = '12223'
if subprocess.check_output(['uname', '-s'], text=True).strip() != 'Darwin':
    raise SystemExit('Native macOS client required')
result = subprocess.run(lab.ssh_args() + ['sudo -n cat /var/lib/smolofis-identity-machine-lab/passwords.json'],
                        capture_output=True, timeout=30)
if result.returncode:
    raise SystemExit('Unable to read synthetic lab credentials; output suppressed')
passwords = json.loads(result.stdout)
tunnel = subprocess.Popen(lab.ssh_args()[:-1] + ['-o', 'ExitOnForwardFailure=yes', '-N',
    '-L', '127.0.0.1:11445:127.0.0.1:1445', lab.ssh_args()[-1]],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(30):
        if tunnel.poll() is not None:
            raise SystemExit('Lab-only SSH tunnel failed')
        try:
            with socket.create_connection(('127.0.0.1', 11445), timeout=1):
                break
        except OSError:
            time.sleep(1)
    else:
        raise SystemExit('Lab-only SSH tunnel readiness timed out')
    with mount_directory() as directory:
        mount = Path(directory) / 'share'
        mount.mkdir()
        for user in (('outsider',) if '--outsider-only' in sys.argv else ('alice', 'reader', 'outsider')):
            url = '//M0;' + user + ':' + urllib.parse.quote(passwords[user], safe='') + '@127.0.0.1:11445/projects'
            try:
                result = subprocess.run(['/sbin/mount', '-t', 'smbfs', '-o',
                    'nobrowse,forcenewsession,nopassprompt', url, str(mount)], capture_output=True, timeout=60)
            except subprocess.TimeoutExpired:
                raise SystemExit('FAIL: native SMB mount timeout; credential output suppressed') from None
            if result.returncode:
                if user == 'outsider':
                    diagnostic = (result.stdout + result.stderr).lower()
                    if not any(message in diagnostic for message in
                               (b'authentication error', b'permission denied', b'access denied',
                                b'logon failure', b'operation not permitted')):
                        detail = diagnostic.decode(errors='replace')
                        for value in (url, passwords[user], urllib.parse.quote(passwords[user], safe='')):
                            detail = detail.replace(value.lower(), '[redacted]')
                        raise SystemExit(f'FAIL: outsider mount without access-denial diagnostic (exit {result.returncode}): {detail[:500]}')
                    print('PASS: native macOS outsider mount denied', flush=True)
                    continue
                raise SystemExit(f'FAIL: native macOS {user} mount (exit {result.returncode}; output suppressed)')
            try:
                if user == 'outsider':
                    raise SystemExit('FAIL: native macOS outsider mounted share')
                expected = 'M0 cold restore retained share evidence\n'
                if (mount / 'm0-restore-evidence.txt').read_text() != expected:
                    raise SystemExit('FAIL: native macOS shared content')
                probe = mount / 'm0-macos-probe.txt'
                try:
                    probe.write_text('Native macOS permission evidence\n')
                except OSError as error:
                    if user != 'reader' or error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                        raise SystemExit('FAIL: native macOS unexpected write error') from None
                    print('PASS: native macOS reader reads but cannot write', flush=True)
                else:
                    probe.unlink()
                    if user != 'alice':
                        raise SystemExit('FAIL: native macOS reader wrote share')
                    print('PASS: native macOS writer reads, writes, and deletes', flush=True)
            finally:
                if subprocess.run(['/sbin/umount', str(mount)], capture_output=True, timeout=30).returncode:
                    raise SystemExit('FAIL: native macOS unmount; inspect retained lab mount')
finally:
    tunnel.terminate()
    try:
        tunnel.wait(timeout=10)
    except subprocess.TimeoutExpired:
        tunnel.kill()
        tunnel.wait()
