"""Configure the synthetic Gitea instance without logging credentials."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

root = Path('/var/lib/smolofis-identity-machine-lab')
if os.geteuid() != 0 or not (root / 'domain-ready').exists() or not Path('/etc/smolofis-m0-fullvm').exists():
    raise SystemExit('Refusing configuration outside the owned VM')
passwords = json.loads((root / 'passwords.json').read_text())
compose = ['docker-compose', '-f', '/opt/smolofis-identity-lab/vm/compose.yml']
def run(*args):
    result = subprocess.run([*compose, *args], capture_output=True, timeout=120)
    if result.returncode:
        raise SystemExit('FAIL: Gitea configuration command; output suppressed')
    return result.stdout.decode()

def healthy():
    for _ in range(120):
        try:
            with urllib.request.urlopen('http://127.0.0.1:3000/api/healthz', timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(2)
    raise SystemExit('FAIL: guest Gitea health readiness')

healthy()
if '--verify-only' not in sys.argv:
    run('cp', '/var/lib/samba/private/tls/ca.pem', 'gitea:/usr/local/share/ca-certificates/m0.crt')
    run('exec', '-T', '--user', 'root', 'gitea', 'update-ca-certificates')
    cli = ('exec', '-T', '--user', 'git', 'gitea', 'gitea', 'admin')
    if 'M0-Native' not in run(*cli, 'auth', 'list'):
        run(*cli, 'auth', 'add-ldap', '--name', 'M0-Native', '--security-protocol', 'ldaps',
            '--host', 'dc-m0.m0.smolofis.test', '--port', '636',
            '--bind-dn', 'CN=ldap-reader,CN=Users,DC=m0,DC=smolofis,DC=test',
            '--bind-password', passwords['ldap-reader'],
            '--user-search-base', 'CN=Users,DC=m0,DC=smolofis,DC=test',
            '--user-filter', '(&(objectClass=user)(sAMAccountName=%s)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))',
            '--username-attribute', 'sAMAccountName', '--email-attribute', 'mail')
    run('restart', 'gitea')
    healthy()
token = base64.b64encode(f'alice:{passwords["alice"]}'.encode()).decode()
request = urllib.request.Request('http://127.0.0.1:3000/api/v1/user',
                                 headers={'Authorization': 'Basic ' + token})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        user = json.load(response)
except Exception as error:
    raise SystemExit('FAIL: guest Gitea directory login (' + type(error).__name__ + ')') from None
if user.get('login') != 'alice' or user.get('is_admin'):
    raise SystemExit('FAIL: unexpected Gitea account or administrator privilege')
print('PASS: guest Gitea health and non-admin directory login using trusted LDAPS')
