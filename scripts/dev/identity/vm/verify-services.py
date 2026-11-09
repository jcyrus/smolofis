"""Synthetic share checks from the owned guest; native client acceptance is separate."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

root = Path('/var/lib/smolofis-identity-machine-lab')
if os.geteuid() != 0 or not (root / 'domain-ready').exists() or not Path('/etc/smolofis-m0-fullvm').exists():
    raise SystemExit('Refusing checks outside the owned VM')
passwords = json.loads((root / 'passwords.json').read_text())
with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    payload = temp / 'payload'
    payload.write_text('M0 full VM permission evidence\n')
    probe = f'm0-probe-{time.time_ns()}.txt'
    for user in ('alice', 'reader', 'outsider'):
        auth = temp / 'auth'
        auth.write_text(f'username = {user}\ndomain = M0\npassword = {passwords[user]}\n')
        auth.chmod(0o600)
        def check(label, operation, expected):
            result = subprocess.run(['smbclient', '//127.0.0.1/projects', '-p', '1445',
                                     '-A', str(auth), '-c', operation], capture_output=True, timeout=45)
            if (result.returncode == 0) != expected:
                raise SystemExit(f'FAIL: {user} {label}; output suppressed')
            print(f'PASS: {user} {label} ({"allowed" if expected else "denied"})', flush=True)
        check('list', 'ls', user != 'outsider')
        check('write', f'put {payload} {probe}', user == 'alice')
        if user == 'reader':
            check('read writer file', f'get {probe} {temp}/download', True)
            if (temp / 'download').read_bytes() != payload.read_bytes():
                raise SystemExit('FAIL: shared content changed')
    user = 'alice'
    auth.write_text(f'username = alice\ndomain = M0\npassword = {passwords["alice"]}\n')
    check('cleanup', f'del {probe}', True)
print('Guest SMB checks passed; native Windows/macOS acceptance remains open.')
