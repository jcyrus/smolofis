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
if Path('/run/systemd/container').exists() or subprocess.run(
        ['systemd-detect-virt', '--vm'], capture_output=True).returncode:
    raise SystemExit('FAIL: native acceptance requires a full-kernel VM, not a container')
if not (root / 'owned').is_file() or os.geteuid() != 0:
    raise SystemExit('Refusing to inspect a machine not owned by the isolated lab')
action = sys.argv[1]
if action not in ('snapshot', 'compare', 'compare-reboot'):
    raise SystemExit('usage: verify-machine.py snapshot|compare|compare-reboot')
passwords = json.loads((root / 'passwords.json').read_text())
with tempfile.TemporaryDirectory() as temporary:
    password = Path(temporary) / 'password'
    password.write_text(passwords['alice'])
    password.chmod(0o600)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            result = subprocess.run([
                'ldapwhoami', '-x', '-H', 'ldaps://127.0.0.1',
                '-D', 'alice@M0.SMOLOFIS.TEST', '-y', str(password),
            ], env={**os.environ, 'LDAPTLS_CACERT': '/var/lib/samba/private/tls/ca.pem',
                    'LDAPTLS_REQCERT': 'demand'}, capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            time.sleep(1)
            continue
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise SystemExit('FAIL: native directory certificate-verified authentication')
    auth = Path(temporary) / 'smb-auth'
    auth.write_text('username = alice\ndomain = M0\npassword = ' + passwords['alice'] + '\n')
    auth.chmod(0o600)
    # systemd activation and LDAP readiness can precede the SMB listener.
    deadline = time.monotonic() + 90
    while True:
        try:
            result = subprocess.run(['smbclient', '//127.0.0.1/sysvol', '-A', str(auth), '-c', 'ls'],
                                    capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    if result is None or result.returncode != 0:
        raise SystemExit('FAIL: native built-in sysvol read access')

lp = LoadParm()
lp.load('/etc/samba/smb.conf')
db = SamDB(url=str(Path(lp.get('private dir')) / 'sam.ldb'), session_info=system_session(), lp=lp)
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
    if action == 'compare-reboot' and report['kernel_boot_id'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        raise SystemExit('FAIL: kernel boot ID did not change')
    report.update(last_invocation_id=invocation, cycles_verified=report['cycles_verified'] + 1)
    if action == 'compare-reboot':
        report['kernel_reboots_verified'] = report.get('kernel_reboots_verified', 0) + 1
report.update({
    'native_ldaps_passed': True, 'native_sysvol_read_passed': True,
    'container_runtime': Path('/run/systemd/container').read_text().strip()
        if Path('/run/systemd/container').exists() else None,
    'kernel_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
    'samba_version': subprocess.check_output(['samba', '--version'], text=True).strip(),
    'architecture': subprocess.check_output(['uname', '-m'], text=True).strip(),
    'root_filesystem': subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', '/'], text=True).strip(),
    'native_sysvol_ntacl_bytes': len(os.getxattr(lp.get('path', 'sysvol'), 'security.NTACL')),
})
baseline.write_text(json.dumps(report, indent=2) + '\n')
print(f'PASS: native systemd directory {action}; verified service cycles={report["cycles_verified"]}', flush=True)
