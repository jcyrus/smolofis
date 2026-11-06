"""Verify real systemd activation and native Samba state in a separate lab OS."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import ldb
from samba.auth import system_session
from samba.param import LoadParm
from samba.samdb import SamDB

root = Path('/var/lib/smolofis-identity-machine-lab')
if not (root / 'owned').is_file() or os.geteuid() != 0:
    raise SystemExit('Refusing to inspect a machine not owned by the isolated lab')
action = sys.argv[1]
if action not in ('snapshot', 'compare'):
    raise SystemExit('usage: verify-machine.py snapshot|compare')
passwords = json.loads((root / 'passwords.json').read_text())
with tempfile.TemporaryDirectory() as temporary:
    password = Path(temporary) / 'password'
    password.write_text(passwords['alice'])
    password.chmod(0o600)
    for _ in range(30):
        result = subprocess.run([
            'ldapwhoami', '-x', '-H', 'ldaps://localhost',
            '-D', 'alice@M0.SMOLOFIS.TEST', '-y', str(password),
        ], env={**os.environ, 'LDAPTLS_CACERT': '/var/lib/samba/private/tls/ca.pem',
                'LDAPTLS_REQCERT': 'demand'}, capture_output=True, timeout=5)
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise SystemExit('FAIL: native directory certificate-verified authentication')
    auth = Path(temporary) / 'smb-auth'
    auth.write_text('username = alice\ndomain = M0\npassword = ' + passwords['alice'] + '\n')
    auth.chmod(0o600)
    result = subprocess.run(['smbclient', '//127.0.0.1/sysvol', '-A', str(auth), '-c', 'ls'],
                            capture_output=True, timeout=15)
    if result.returncode != 0:
        raise SystemExit('FAIL: native built-in sysvol read access')

lp = LoadParm()
lp.load('/etc/samba/smb.conf')
db = SamDB(url='/var/lib/samba/private/sam.ldb', session_info=system_session(), lp=lp)
user = db.search(base=db.domain_dn(), scope=ldb.SCOPE_SUBTREE,
                 expression='(&(objectClass=user)(sAMAccountName=alice))',
                 attrs=['objectGUID', 'objectSid'])[0]
identity = {key: bytes(user[key][0]).hex() for key in ('objectGUID', 'objectSid')}
invocation = subprocess.check_output([
    'systemctl', 'show', 'samba-ad-dc.service', '-p', 'InvocationID', '--value',
], text=True).strip()
if not invocation:
    raise SystemExit('FAIL: native service has no systemd invocation ID')
baseline = root / 'baseline.json'
if action == 'snapshot':
    report = {'identity': identity, 'last_invocation_id': invocation, 'cycles_verified': 0}
else:
    report = json.loads(baseline.read_text())
    if report['identity'] != identity or report['last_invocation_id'] == invocation:
        raise SystemExit('FAIL: identity changed or service did not start in a new systemd invocation')
    report.update(last_invocation_id=invocation, cycles_verified=report['cycles_verified'] + 1)
report.update({
    'native_ldaps_passed': True, 'native_sysvol_read_passed': True,
    'container_runtime': Path('/run/systemd/container').read_text().strip()
        if Path('/run/systemd/container').exists() else None,
    'kernel_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
    'samba_version': subprocess.check_output(['samba', '--version'], text=True).strip(),
})
baseline.write_text(json.dumps(report, indent=2) + '\n')
print(f'PASS: native systemd directory {action}; verified service cycles={report["cycles_verified"]}', flush=True)
