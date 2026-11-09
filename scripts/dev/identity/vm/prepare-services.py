"""Create synthetic identities only inside the owned full-kernel M0 guest."""
import json
import os
from pathlib import Path
import secrets
import subprocess

root = Path('/var/lib/smolofis-identity-machine-lab')
if os.geteuid() != 0 or not (root / 'domain-ready').exists() or not Path('/etc/smolofis-m0-fullvm').exists():
    raise SystemExit('Refusing to prepare services outside the owned VM')
os.umask(0o077)
password_file = root / 'passwords.json'
passwords = json.loads(password_file.read_text())

def command(*args, allow_failure=False):
    result = subprocess.run(args, capture_output=True)
    if result.returncode and not allow_failure:
        # Do not echo command arguments or output that might include credentials.
        raise SystemExit('Synthetic directory preparation failed: ' + args[1])
    return result.returncode

for name in ('reader', 'outsider', 'ldap-reader', 'lab-admin'):
    if name not in passwords:
        passwords[name] = 'Lab9!' + secrets.token_hex(18)
        password_file.write_text(json.dumps(passwords))
    if command('samba-tool', 'user', 'show', name, allow_failure=True):
        command('samba-tool', 'user', 'create', name, passwords[name],
                '--mail-address=' + name + '@m0.smolofis.test')
for group, member in (('Engineering', 'alice'), ('Readers', 'reader')):
    if command('samba-tool', 'group', 'show', group, allow_failure=True):
        command('samba-tool', 'group', 'add', group)
    command('samba-tool', 'group', 'addmembers', group, member)
print('PASS: synthetic users and share groups prepared')
