"""Real root/UID/filesystem/LDAPS acceptance in the owned full-kernel M0 VM."""
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
BINARY = Path('/opt/smolofis-panel-audit-lab/smolofis-panel')
if (os.geteuid() != 0 or not (ROOT / 'domain-ready').exists()
        or not Path('/etc/smolofis-m0-fullvm').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Refusing audit acceptance outside the owned full-kernel VM')
if not BINARY.is_file():
    raise SystemExit('Stage the reviewed Linux test binary first')
os.umask(0o077)
suffix = secrets.token_hex(5)
runtime = Path('/run') / ('smolofis-local-audit-' + suffix)
runtime.mkdir(mode=0o755)
runtime.chmod(0o755)
evidence = ROOT / ('local-audit-' + suffix)
evidence.mkdir(mode=0o700)
checks = []

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def required(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=90, **kwargs)
    if result.returncode:
        raise SystemExit('FAIL: audit lab prerequisite; output suppressed')
    return result.stdout

passwords = {name: 'Lab9!' + secrets.token_hex(18) for name in ('admin', 'nonadmin')}
(evidence / 'passwords.json').write_text(json.dumps(passwords))
admin, nonadmin = 'a-' + suffix, 'n-' + suffix
for name, password in ((admin, passwords['admin']), (nonadmin, passwords['nonadmin'])):
    required(['samba-tool', 'user', 'create', name, password])
group = 'M1-Audit-' + suffix
required(['samba-tool', 'group', 'add', group])
required(['samba-tool', 'group', 'addmembers', group, admin])
source_passwords = json.loads((ROOT / 'passwords.json').read_text())
reader = runtime / 'reader-password'
reader.write_text(source_passwords['ldap-reader'])
reader.chmod(0o640)
os.chown(reader, 0, 65534)
ca = runtime / 'ca.pem'
shutil.copyfile('/var/lib/samba/private/tls/ca.pem', ca)
ca.chmod(0o644)
config = runtime / 'auth.json'
config.write_text(json.dumps({'origin': 'http://127.0.0.1:18093', 'directory': {
    'url': 'ldaps://127.0.0.1', 'realm': 'M0.SMOLOFIS.TEST',
    'base_dn': 'DC=m0,DC=smolofis,DC=test',
    'admin_group_dn': f'CN={group},CN=Users,DC=m0,DC=smolofis,DC=test',
    'reader_dn': 'CN=ldap-reader,CN=Users,DC=m0,DC=smolofis,DC=test',
    'reader_password_file': str(reader), 'ca_file': str(ca)}}))
config.chmod(0o644)
state = runtime / 'state.json'
audit = runtime / 'events.jsonl'
audit.touch(mode=0o600)

def control(operation, *, log=audit, username=admin, password=None, target=state,
            allowed=True, uid=0, reason=None):
    args = [str(BINARY), operation]
    args += [str(config), str(target), username] if operation == '--auth-enroll' else [str(target)]
    environment = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}
    environment['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(log)
    result = subprocess.run(args, input=(password or '').encode(), capture_output=True, timeout=60,
                            env=environment, user=uid, group=uid, extra_groups=())
    if (result.returncode == 0) != allowed:
        raise SystemExit(f'FAIL: {operation} unexpected exit {result.returncode}; output suppressed')
    if reason and reason.encode() not in result.stderr:
        raise SystemExit(f'FAIL: {operation} did not report expected failure classification')
    return result

control('--auth-enroll', username=nonadmin, password=passwords['nonadmin'], allowed=False)
control('--auth-enroll', password='incorrect', allowed=False)
assert not state.exists() and audit.read_bytes() == b''
passed('failed directory verification neither enrolls nor claims authorization')
control('--auth-enroll', password=passwords['admin'])
first = json.loads(state.read_text())
events = [json.loads(line) for line in audit.read_text().splitlines()]
assert first['locked'] is False and state.stat().st_uid == 0
assert state.stat().st_mode & 0o777 == 0o644
assert audit.stat().st_uid == 0 and audit.stat().st_mode & 0o777 == 0o600
assert events[0]['event'] == 'local_enrollment_authorized' and events[-1]['event'] == 'local_enrollment_applied'
assert events[0]['operation_id'] == events[-1]['operation_id'] == first['last_operation']['id']
assert first['version'] == 2 and first['last_operation']['outcome'] == 'applied'
assert events[-1]['actor_uid'] == 0
assert events[-1]['actor_guid'] == first['first_admin_guid']
assert not any(value in audit.read_text() for value in (*passwords.values(), admin, nonadmin))
passed('real verified administrator enrollment emits root-attributed GUID without credentials')

before_state, before_log = state.read_bytes(), audit.read_bytes()
for operation in ('--auth-enroll', '--auth-lock', '--auth-status'):
    control(operation, password=passwords['admin'], uid=65534, allowed=False, reason='require root')
assert state.read_bytes() == before_state and audit.read_bytes() == before_log
passed('unprivileged Linux UID cannot invoke root recovery commands or modify state/history')

control('--auth-lock')
locked = json.loads(state.read_text())
last = json.loads(audit.read_text().splitlines()[-1])
assert locked['locked'] and locked['generation'] != first['generation']
assert last['event'] == 'local_console_locked' and last['actor_uid'] == 0 and last['actor_guid'] is None
passed('completed lockdown rotates generation and attributes root, not the directory administrator')
shutil.copyfile(audit, evidence / 'initial-events.jsonl')

audit.write_bytes(b'partial')
before = state.read_bytes()
control('--auth-enroll', password=passwords['admin'], allowed=False, reason='incomplete final record')
assert state.read_bytes() == before and audit.read_bytes() == b'partial'
passed('crash-tail audit failure preserves locked state and generation')
shutil.copyfile(audit, evidence / 'injected-crash-tail')

good = runtime / 'healthy.jsonl'
good.touch(mode=0o600)
def reject_audit(log, reason=None):
    control('--auth-enroll', log=good, password=passwords['admin'])
    before = state.read_bytes()
    control('--auth-enroll', log=log, password=passwords['admin'], allowed=False, reason=reason)
    assert state.read_bytes() == before
    control('--auth-lock', log=log, allowed=False, reason='Console is locked')
    after = json.loads(state.read_text())
    assert after['locked'] and after['generation'] != json.loads(before)['generation']
    status = json.loads(control('--auth-status', log=log).stdout)
    assert status['locked'] and status['directory_checked'] is False

reject_audit(runtime / 'missing.jsonl')
reject_audit(audit, 'incomplete final record')
wrong_mode = runtime / 'wrong-mode.jsonl'
wrong_mode.touch(mode=0o644)
wrong_mode.chmod(0o644)
reject_audit(wrong_mode, 'one link and no world access')
wrong_owner = runtime / 'wrong-owner.jsonl'
wrong_owner.touch(mode=0o600)
os.chown(wrong_owner, 65534, 65534)
reject_audit(wrong_owner, 'caller-owned')
link = runtime / 'linked.jsonl'
link.symlink_to(good)
reject_audit(link)
link.unlink()
os.link(good, link)
try:
    before = state.read_bytes()
    control('--auth-enroll', log=link, password=passwords['admin'], allowed=False, reason='one link')
    assert state.read_bytes() == before
    control('--auth-lock', log=link, allowed=False, reason='Console is locked')
finally:
    link.unlink()
passed('missing, corrupt, wrong-mode/owner, symlink and hard-link logs deny enrollment but permit lockdown')

for protected in (state, config):
    before = state.read_bytes(), protected.read_bytes()
    control('--auth-enroll', log=protected, password=passwords['admin'], allowed=False, reason='must be separate')
    assert (state.read_bytes(), protected.read_bytes()) == before
passed('state/config alias audit paths cannot corrupt control files')

full = runtime / 'full'
full.mkdir(mode=0o755)
required(['mount', '-t', 'tmpfs', '-o', 'size=64k,mode=0755', 'tmpfs', str(full)])
try:
    full_log = full / 'events.jsonl'
    full_log.touch(mode=0o600)
    with (full / 'filler').open('wb', buffering=0) as filler:
        while True:
            try:
                filler.write(b'x' * 4096)
            except OSError as error:
                if error.errno != errno.ENOSPC:
                    raise
                break
    reject_audit(full_log)
    assert full_log.read_bytes() == b''
    passed('real ENOSPC on tiny tmpfs prevents enrollment while independent state storage still locks')
finally:
    required(['umount', str(full)])

shared = runtime / 'concurrent.jsonl'
shared.touch(mode=0o600)
children = []
for index in range(4):
    directory = runtime / ('writer-' + str(index))
    directory.mkdir(mode=0o755)
    fixture = directory / 'state.json'
    fixture.write_bytes(state.read_bytes())
    fixture.chmod(0o644)
    environment = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}
    environment['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(shared)
    children.append(subprocess.Popen([str(BINARY), '--auth-lock', str(fixture)],
        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
for child in children:
    _, _ = child.communicate(timeout=60)
    assert child.returncode == 0
events = [json.loads(line) for line in shared.read_text().splitlines()]
assert len(events) == 4 and all(event['event'] == 'local_console_locked' for event in events)
passed('independent root CLI processes append complete records through Linux file locks')

report = {'version': 1, 'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
          'checks': checks, 'kernel': os.uname().release, 'architecture': os.uname().machine,
          'runtime': str(runtime), 'tmpfs_unmounted': True, 'production_modified': False}
(evidence / 'report.json').write_text(json.dumps(report, indent=2))
shutil.copyfile(good, evidence / 'recovery-events.jsonl')
shutil.copyfile(shared, evidence / 'concurrent-events.jsonl')
print(f'PASS: {len(checks)} Linux local-audit checks; private evidence retained at {evidence}', flush=True)
