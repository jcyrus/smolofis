"""Native read-only production setup checks in an isolated full-kernel VM namespace."""
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
SOURCE = Path('/opt/smolofis-production-preflight-lab')
BINARY = SOURCE / 'smolofis-panel'
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel VM required; never run on production')
if not sys.argv[1:]:
    created = []
    try:
        for path in (Path('/etc/smolofis'), Path('/var/lib/smolofis')):
            if not path.exists(): path.mkdir(mode=0o755); created.append(path)
        subprocess.run(['unshare', '--mount', '--fork', sys.executable, __file__, '--inside'], check=True, timeout=240)
    finally:
        for path in reversed(created): path.rmdir()
    raise SystemExit(0)
assert sys.argv[1:] == ['--inside']
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
for path in ('/etc/smolofis', '/var/lib/smolofis', '/etc/systemd/system'):
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=16m,mode=0755,nodev,nosuid', 'tmpfs', path], check=True)
os.umask(0o077)
evidence = ROOT / ('production-preflight-' + secrets.token_hex(5)); evidence.mkdir(mode=0o700)
checks = []
created_user = created_group = created_admin = created_ad_group = False
admin = 'pc-' + secrets.token_hex(5); group = 'M1-Preflight-' + secrets.token_hex(5)
password = 'Lab9!' + secrets.token_hex(18)
environment = {k:v for k,v in os.environ.items() if not k.startswith('SMOLOFIS_')}
config = Path('/etc/smolofis/panel-auth.json')
reader = Path('/etc/smolofis/reader-password')
state = Path('/etc/smolofis/panel-enrollment.json')

def required(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=40, **kwargs)
    assert result.returncode == 0, 'Lab prerequisite failed; credential-bearing diagnostics suppressed'
    return result

def passed(message):
    checks.append(message); print('PASS: ' + message, flush=True)

def snapshot():
    result = {}
    for base in (Path('/etc/smolofis'), Path('/var/lib/smolofis'), Path('/etc/systemd/system')):
        for path in [base, *sorted(base.rglob('*'))]:
            stat = path.lstat()
            result[str(path)] = (stat.st_ino, stat.st_uid, stat.st_gid, stat.st_mode,
                                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else '')
    return result

def probe(label):
    before = snapshot()
    result = subprocess.run([str(BINARY), '--console-setup-check'], capture_output=True, env=environment, timeout=30)
    assert result.returncode != 0  # No appliance marker or live tailnet in this fixture.
    report = json.loads(result.stdout)
    assert report['schema'] == 1 and not report['prerequisites_passed'] and not report['administration_enabled_by_check']
    assert len(report['checks']) == 14 and len({item['id'] for item in report['checks']}) == 14
    assert password.encode() not in result.stdout + result.stderr
    assert reader.read_bytes() not in result.stdout + result.stderr if reader.exists() else True
    assert snapshot() == before, 'Readiness check modified its protected inputs'
    (evidence / (label + '.json')).write_bytes(result.stdout)
    return {item['id']:item['passed'] for item in report['checks']}

try:
    initial = probe('missing')
    assert not initial['directory_configuration'] and not initial['device_enrollment'] and not initial['operation_journal']
    passed('missing setup produces a complete blocked report without creating files or exposing credentials')
    denied = subprocess.run([str(BINARY), '--console-setup-check'], capture_output=True, user=65534, group=65534, extra_groups=(), timeout=10)
    assert denied.returncode != 0 and denied.stdout == b''
    passed('non-root callers cannot inspect production setup inputs')
    try: account = pwd.getpwnam('smolofis-panel')
    except KeyError:
        try: grp.getgrnam('smolofis-panel'); args=['--gid','smolofis-panel']
        except KeyError: args=['--user-group']; created_group=True
        required(['useradd','--system','--no-create-home','--home-dir','/nonexistent','--shell','/usr/sbin/nologin',*args,'smolofis-panel'])
        created_user=True; account=pwd.getpwnam('smolofis-panel')
    uid,gid=account.pw_uid,account.pw_gid
    required(['samba-tool','user','create',admin,password]); created_admin=True
    required(['samba-tool','group','add',group]); created_ad_group=True
    required(['samba-tool','group','addmembers',group,admin])
    identity=Path('/etc/smolofis/identity'); identity.mkdir(mode=0o700)
    shutil.copyfile('/etc/machine-id',identity/'machine-id')
    reader.write_text(json.loads((ROOT/'passwords.json').read_text())['ldap-reader']); reader.chmod(0o640); os.chown(reader,0,gid)
    ca=Path('/etc/smolofis/ca.pem'); shutil.copyfile('/var/lib/samba/private/tls/ca.pem',ca); ca.chmod(0o644)
    configuration={'origin':'https://smolofis-preflight.tail72ba9d.ts.net','directory':{
        'url':'ldaps://127.0.0.1','realm':'M0.SMOLOFIS.TEST','base_dn':'DC=m0,DC=smolofis,DC=test',
        'admin_group_dn':f'CN={group},CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_dn':'CN=ldap-reader,CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_password_file':str(reader),'ca_file':str(ca)}}
    def save_config(): config.write_text(json.dumps(configuration)); config.chmod(0o644)
    save_config()
    required([str(BINARY),'--auth-audit-init','/var/lib/smolofis/local-audit'],env=environment)
    environment['SMOLOFIS_LOCAL_AUDIT_FILE']='/var/lib/smolofis/local-audit/events.jsonl'
    required([str(BINARY),'--auth-enroll',str(config),str(state),admin],input=password.encode(),env=environment)
    required([str(BINARY),'--operation-init','/var/lib/smolofis/operations'],env=environment)
    audit=Path('/var/lib/smolofis/panel-audit'); audit.mkdir(mode=0o755); audit.chmod(0o755)
    log=audit/'auth.jsonl'; log.touch(mode=0o600); os.chown(log,uid,gid)
    (Path('/etc/smolofis')/'admin-worker.env').write_text(f'PANEL_UID={uid}\n')
    for source in (SOURCE/'units').rglob('*'):
        if source.is_file():
            target=Path('/etc/systemd/system')/source.relative_to(SOURCE/'units')
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
            target.parent.chmod(0o755); shutil.copyfile(source,target); target.chmod(0o644)
    baseline=probe('valid-local')
    assert all(value for key,value in baseline.items() if key not in ('installed_appliance','private_transport_prerequisites'))
    passed('real LDAPS reader/group, device enrollment, UID mapping, protected logs/journal and unit files pass while missing production/tailnet gates stay blocked')
    original=reader.read_bytes(); reader.write_text('Wrong9!'+secrets.token_hex(10))
    assert not probe('wrong-reader')['directory_reader_and_group']
    reader.write_bytes(original)
    passed('invalid reader credentials fail the actual LDAPS probe without changing directory data')
    configuration['directory']['base_dn']='DC=wrong,DC=test'; save_config()
    assert not probe('wrong-context')['directory_reader_and_group']
    configuration['directory']['base_dn']='DC=m0,DC=smolofis,DC=test'; save_config()
    configuration['directory']['admin_group_dn']='CN=Missing-Preflight,CN=Users,DC=m0,DC=smolofis,DC=test'; save_config()
    assert not probe('missing-group')['directory_reader_and_group']
    configuration['directory']['admin_group_dn']=f'CN={group},CN=Users,DC=m0,DC=smolofis,DC=test'; save_config()
    passed('actual LDAPS naming-context mismatch and missing administrator group are rejected')
    reader.chmod(0o644); assert not probe('world-reader')['directory_material']; reader.chmod(0o640)
    link=reader.with_name('reader-hardlink'); os.link(reader,link)
    assert not probe('linked-reader')['directory_material']; link.unlink()
    saved=reader.with_name('reader-saved'); reader.rename(saved); reader.symlink_to(saved)
    assert not probe('symlink-reader')['directory_material']; reader.unlink(); saved.rename(reader)
    passed('world-readable, hard-linked and symlinked reader secrets fail before credential loading')
    config.chmod(0o666); assert not probe('writable-config')['directory_configuration']; config.chmod(0o644)
    os.chown(reader,0,65534); assert not probe('inaccessible-reader')['directory_material']; os.chown(reader,0,gid)
    passed('untrusted configuration and panel-inaccessible secrets are reported as blocked')
    log.chmod(0o644); assert not probe('public-audit')['panel_audit']; log.chmod(0o600)
    (Path('/etc/smolofis')/'admin-worker.env').write_text('PANEL_UID=0\n'); assert not probe('root-worker-uid')['service_files']
    (Path('/etc/smolofis')/'admin-worker.env').write_text(f'PANEL_UID={uid}\n')
    passed('public panel audit files and mismatched root worker peers are rejected')
    (identity/'machine-id').write_text('01'*16+'\n'); assert not probe('foreign-identity')['saved_machine_identity']; shutil.copyfile('/etc/machine-id',identity/'machine-id')
    required([str(BINARY),'--auth-lock',str(state)],env=environment)
    assert not probe('locked')['device_enrollment']
    passed('saved machine identity mismatch and local enrollment lockdown remain explicit blockers')
    report={'checks':checks,'binary_sha256':hashlib.file_digest(BINARY.open('rb'),'sha256').hexdigest(),
            'read_only_inventory_verified':True,'real_ldaps':True,'production_modified':False,'services_started':False}
    (evidence/'report.json').write_text(json.dumps(report,indent=2))
    print(f'PASS: {len(checks)} native preflight checks; evidence {evidence}',flush=True)
finally:
    if created_admin: required(['samba-tool','user','delete',admin])
    if created_ad_group: required(['samba-tool','group','delete',group])
    if created_user: required(['userdel','smolofis-panel'])
    if created_group:
        try: grp.getgrnam('smolofis-panel')
        except KeyError: pass
        else: required(['groupdel','smolofis-panel'])
