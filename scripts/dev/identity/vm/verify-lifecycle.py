"""Fresh-login rotation/disable checks for the synthetic Alice account only."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import urllib.error
import urllib.request

root = Path('/var/lib/smolofis-identity-machine-lab')
if os.geteuid() != 0 or not (root / 'domain-ready').exists() or not Path('/etc/smolofis-m0-fullvm').exists():
    raise SystemExit('Refusing lifecycle checks outside the owned VM')
original = json.loads((root / 'passwords.json').read_text())['alice']
rotated = 'Lab9!' + secrets.token_hex(18)
def directory(*args):
    if subprocess.run(['samba-tool', 'user', *args], capture_output=True).returncode:
        raise SystemExit('FAIL: synthetic account mutation; output suppressed')

def check(password, allowed, label):
    with tempfile.TemporaryDirectory() as directory_name:
        temp = Path(directory_name)
        secret = temp / 'password'
        secret.write_text(password)
        secret.chmod(0o600)
        auth = temp / 'smb-auth'
        auth.write_text('username = alice\ndomain = M0\npassword = ' + password + '\n')
        auth.chmod(0o600)
        commands = {
            'LDAPS': ['ldapwhoami', '-x', '-H', 'ldaps://127.0.0.1',
                      '-D', 'alice@M0.SMOLOFIS.TEST', '-y', str(secret)],
            'SMB': ['smbclient', '//127.0.0.1/projects', '-p', '1445', '-A', str(auth), '-c', 'ls'],
        }
        for service, command in commands.items():
            result = subprocess.run(command, capture_output=True, timeout=45,
                                    env={**os.environ, 'LDAPTLS_CACERT': '/var/lib/samba/private/tls/ca.pem',
                                         'LDAPTLS_REQCERT': 'demand'})
            if (result.returncode == 0) != allowed:
                raise SystemExit(f'FAIL: {label} {service}; output suppressed')
        token = base64.b64encode(('alice:' + password).encode()).decode()
        request = urllib.request.Request('http://127.0.0.1:3000/api/v1/user',
                                         headers={'Authorization': 'Basic ' + token})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                account = json.load(response)
            success = account.get('login') == 'alice' and not account.get('is_admin')
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise SystemExit(f'FAIL: {label} Gitea unexpected HTTP {error.code}') from None
            success = False
        if success != allowed:
            raise SystemExit(f'FAIL: {label} Gitea')
        print(f'PASS: {label}: LDAPS, SMB, Gitea ({"allowed" if allowed else "denied"})', flush=True)

try:
    directory('setpassword', 'alice', '--newpassword=' + rotated)
    check(original, False, 'old password after rotation')
    check(rotated, True, 'new password after rotation')
    directory('disable', 'alice')
    check(rotated, False, 'disabled account fresh login')
finally:
    directory('enable', 'alice')
    directory('setpassword', 'alice', '--newpassword=' + original)
check(original, True, 'restored synthetic account')
print('Fresh-login lifecycle passed; existing-session revocation is separate acceptance.')
