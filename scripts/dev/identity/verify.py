"""Network checks only. Never include subprocess output containing secrets."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

passwords = json.loads(Path('/lab/passwords.json').read_text())
disabled = '--disabled' in sys.argv


def check(label, command, expected=True, env=None):
    result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    success = result.returncode == 0
    if success != expected:
        raise SystemExit(f'FAIL: {label} (exit {result.returncode}; output suppressed)')
    print(f'PASS: {label} ({"allowed" if expected else "denied"})', flush=True)


for attempt in range(60):
    try:
        with urllib.request.urlopen('http://git:3000/api/healthz', timeout=2) as response:
            if response.status == 200:
                break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit('FAIL: Gitea did not become healthy')
print('PASS: Gitea health', flush=True)

token = base64.b64encode(f'alice:{passwords["alice"]}'.encode()).decode()
request = urllib.request.Request('http://git:3000/api/v1/user', headers={
    'Authorization': f'Basic {token}',
})
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        account = json.load(response)
    if disabled:
        raise SystemExit('FAIL: disabled user authenticated to Gitea')
    if account.get('login') != 'alice' or account.get('is_admin'):
        raise SystemExit('FAIL: unexpected Gitea identity or admin privilege')
except urllib.error.HTTPError as error:
    if not disabled or error.code != 401:
        raise SystemExit(f'FAIL: Gitea directory authentication (HTTP {error.code})') from None
except Exception as error:
    raise SystemExit(f'FAIL: Gitea directory authentication ({type(error).__name__})') from None
print(f'PASS: Gitea directory authentication ({"disabled user denied" if disabled else "non-admin user allowed"})', flush=True)

with tempfile.TemporaryDirectory() as temp:
    for user in ('alice', 'reader', 'outsider'):
        password = Path(temp) / 'password'
        password.write_text(passwords[user])
        password.chmod(0o600)
        check(f'{user} verified LDAPS bind', [
            'ldapwhoami', '-x', '-H', 'ldaps://dc',
            '-D', f'{user}@lab.smolofis.test', '-y', str(password),
        ], expected=not (disabled and user == 'alice'),
              env={**os.environ, 'LDAPTLS_CACERT': '/lab/ca.crt'})

    payload = Path(temp) / 'payload'
    payload.write_text('SmolOfis identity permission probe\n')
    probe = f'probe-{time.time_ns()}.txt'
    for user, writable in (('alice', True), ('reader', False), ('outsider', False)):
        auth = Path(temp) / 'smb-auth'
        auth.write_text(f'username = {user}\npassword = {passwords[user]}\ndomain = LAB\n')
        auth.chmod(0o600)
        base = ['smbclient', '//files/projects', '-A', str(auth)]
        allowed = user != 'outsider' and not (disabled and user == 'alice')
        check(f'{user} list access', base + ['-c', 'ls'], allowed)
        check(f'{user} write access', base + ['-c', f'put {payload} {probe}'], writable and allowed)
        if user == 'reader' and not disabled:
            check('reader downloads writer file', base + ['-c', f'get {probe} {temp}/download'])
            if (Path(temp) / 'download').read_bytes() != payload.read_bytes():
                raise SystemExit('FAIL: shared file contents differ')
    if not disabled:
        auth.write_text(f'username = alice\npassword = {passwords["alice"]}\ndomain = LAB\n')
        check('writer cleanup', ['smbclient', '//files/projects', '-A', str(auth), '-c', f'del {probe}'])

print('Container smoke checks passed; VM/client/lifecycle gate remains open.', flush=True)
