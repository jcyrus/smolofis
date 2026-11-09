"""Exercise supported offline directory backup/restore only in the owned clone."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import secrets
import sys
import re

root = Path('/var/lib/smolofis-identity-machine-lab')
if os.geteuid() != 0 or not (root / 'domain-ready').exists() or not Path('/etc/smolofis-m0-fullvm').exists():
    raise SystemExit('Refusing directory restore outside the owned VM')
resume = len(sys.argv) == 2 and sys.argv[1].startswith('--resume=')
suffix = sys.argv[1].split('=', 1)[1] if resume else secrets.token_hex(5)
if not re.fullmatch('[0-9a-f]{10}', suffix):
    raise SystemExit('Invalid owned restore attempt identifier')
attempt = root / ('domain-restore-' + suffix)
if not resume:
    attempt.mkdir(mode=0o700)
# Keep Unix socket paths short and allow SYSVOL traversal without exposing
# the private backup directory. Samba's private subtree remains root-only.
restore_base = Path('/var/lib/m0-ad')
restore_base.mkdir(mode=0o755, exist_ok=True)
target = restore_base / suffix
backup = attempt / 'directory-backup'
saved = attempt / 'smb-before-domain-restore.conf'
compose = ['docker-compose', '-f', '/opt/smolofis-identity-lab/vm/compose.yml']
def required(args, label, timeout=180):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode:
        (attempt / 'last-error.log').write_bytes(result.stdout + result.stderr)
        raise SystemExit('FAIL: ' + label + '; private diagnostics retained in guest')
    print('PASS: ' + label, flush=True)
    return result.stdout

config = Path('/etc/samba/smb.conf')
original = config.read_bytes()
if resume:
    if not saved.is_file() or saved.read_bytes() != original or not (target / 'etc/smb.conf').is_file():
        raise SystemExit('Refusing to resume without matching retained original config and restored target')
else:
    saved.write_bytes(original)
    backup.mkdir(mode=0o700)
restored = False
try:
    required([*compose, 'stop'], 'stop application clients')
    required(['systemctl', 'stop', 'samba-ad-dc'], 'stop original DC')
    if not resume:
        required(['samba-tool', 'domain', 'backup', 'offline', '--targetdir=' + str(backup)], 'offline directory backup', timeout=600)
    archives = list(backup.glob('*.tar.bz2'))
    if len(archives) != 1:
        raise SystemExit('FAIL: expected exactly one directory backup archive')
    archive = archives[0]
    if not resume:
        required(['samba-tool', 'domain', 'backup', 'restore', '--backup-file=' + str(archive),
                  '--targetdir=' + str(target), '--newservername=dc-r0', '--host-ip=172.31.83.10'],
                 'restore into a fresh directory tree', timeout=600)
    generated = target / 'etc/smb.conf'
    if not generated.is_file():
        raise SystemExit('FAIL: restored directory configuration missing')
    # The restore tool emits this cache path but does not populate runtime caches.
    (target / 'cache').mkdir(mode=0o700, exist_ok=True)
    (target / 'private').chmod(0o700)
    # Use the TLS material recovered from the backup, retaining the trusted alias.
    tls = target / 'private/tls'
    if not all((tls / name).is_file() for name in ('ca.pem', 'cert.pem', 'key.pem')):
        raise SystemExit('FAIL: restored TLS material missing')
    import re
    text = generated.read_text()
    for key, value in {'old password allowed period': '0',
                       'tls cafile': str(tls / 'ca.pem'),
                       'tls certfile': str(tls / 'cert.pem'),
                       'tls keyfile': str(tls / 'key.pem')}.items():
        pattern = r'(?im)^\s*' + re.escape(key) + r'\s*=.*$'
        if re.search(pattern, text):
            text = re.sub(pattern, '\t' + key + ' = ' + value, text)
        else:
            text = re.sub(r'(?im)^\[global\]\s*$', '[global]\n\t' + key + ' = ' + value, text, count=1)
    config.write_text(text)
    required(['testparm', '-s'], 'validate restored configuration')
    required(['samba-tool', 'ntacl', 'sysvolreset'], 'restore default SYSVOL ACLs', timeout=180)
    required(['samba-tool', 'ntacl', 'sysvolcheck'], 'validate SYSVOL ACLs', timeout=180)
    required(['systemctl', 'start', 'samba-ad-dc'], 'start restored DC')
    required(['python3', '/opt/smolofis-identity-lab/verify-machine.py', 'compare'], 'restored active DB identity, LDAPS, and SYSVOL', timeout=360)
    required([*compose, 'start'], 'start existing domain member and Gitea')
    required(['python3', '/opt/smolofis-identity-lab/vm/configure-gitea.py', '--verify-only'], 'Gitea login after directory restore', timeout=360)
    required(['python3', '/opt/smolofis-identity-lab/vm/verify-services.py'], 'SMB permissions after directory restore', timeout=180)
    required(['python3', '/opt/smolofis-identity-lab/vm/verify-checkpoint.py', 'compare'], 'retained metadata and mappings after directory restore', timeout=180)
    restored = True
    report = {'backup_sha256': hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest(),
              'restored_dc': 'dc-r0', 'active_private_directory': str(target / 'private'),
              'stable_identity': True, 'verified_ldaps_sysvol': True, 'gitea_and_smb_passed': True,
              'file_metadata_and_mapping_preserved': True}
    (root / 'domain-restore-report.json').write_text(json.dumps(report, indent=2))
    print('PASS: supported offline directory backup/restore, stable identities, trusted LDAP, SYSVOL, Gitea, SMB, and retained file metadata', flush=True)
finally:
    if not restored:
        subprocess.run(['systemctl', 'stop', 'samba-ad-dc'], capture_output=True, timeout=120)
        config.write_bytes(original)
        subprocess.run(['systemctl', 'start', 'samba-ad-dc'], capture_output=True, timeout=120)
        subprocess.run([*compose, 'start'], capture_output=True, timeout=120)
        print('Original DC configuration restored; inspect service health before retrying.', flush=True)
