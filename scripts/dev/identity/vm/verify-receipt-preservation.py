"""Native root crash-receipt acceptance with isolated state and disposable directory accounts."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import secrets
import signal
import subprocess
import time

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
SOURCE = Path('/opt/smolofis-receipt-journal-lab')
BINARY = SOURCE / 'smolofis-panel'
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel lab VM required')
prior = max(ROOT.glob('operation-receipts-*/report.json'), key=lambda p: p.stat().st_mtime)
old = json.loads(prior.read_text())
record = json.loads(Path(old['state']).read_text())
record['version'] = 1
record.pop('last_operation', None)
os.umask(0o077)
evidence = ROOT / ('receipt-journal-' + secrets.token_hex(5))
evidence.mkdir(mode=0o700)
state, log = evidence / 'state.json', evidence / 'events.jsonl'
state.write_text(json.dumps(record))
log.touch(mode=0o600)
env = {k: v for k, v in os.environ.items() if not k.startswith('SMOLOFIS_')}
env['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(log)
checks = []

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def run(args, *, limit=None, uid=0, allowed=True, password=b''):
    def limits():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    result = subprocess.run([str(BINARY), *args], env=env, capture_output=True,
                            timeout=30, user=uid, group=uid, extra_groups=(), input=password,
                            preexec_fn=limits if limit is not None else None)
    assert (result.returncode == 0) == allowed, result.stderr.decode()
    return result

def status():
    return json.loads(run(['--auth-status', str(state)]).stdout)

def lookup(receipt):
    return json.loads(run(['--auth-operation-status', str(state), receipt['id']]).stdout)

# Legacy state has no prior receipt to checkpoint. Crash after the atomic state
# write, while appending its first completion; no global file limit or clock change.
padding = json.dumps({'version': 1, 'timestamp_ms': 0, 'event': 'login_denied', 'actor_guid': None}).encode()
log.write_bytes(padding + b' ' * (16384 - len(padding) - 1) + b'\n')
result = run(['--auth-lock', str(state)], limit=16385, allowed=False)
assert result.returncode == -signal.SIGXFSZ
crashed = status()['last_operation']
assert crashed['operation'] == 'lockdown' and crashed['outcome'] == 'applied'
raw = log.read_bytes()
assert not raw.endswith(b'\n')
(evidence / 'crash-before-archive.jsonl').write_bytes(raw)
assert lookup(crashed)['source'] == 'state_receipt'
passed('real SIGXFSZ completion-write crash leaves a readable applied state receipt')

archive_dir = evidence / 'archives'; archive_dir.mkdir(mode=0o700)
run(['--auth-audit-archive', str(log), str(archive_dir)])
assert log.read_bytes() == b''
run(['--auth-lock', str(state)])
latest = status()['last_operation']
assert latest['id'] != crashed['id']
result = lookup(crashed)
assert result['source'] == 'audit_history' and result['outcome'] == 'applied'
rows = [json.loads(line) for line in log.read_text().splitlines()]
assert rows[0]['event'] == 'local_receipt_preserved' and rows[0]['operation_id'] == crashed['id']
assert rows[0]['actor_uid'] == 0 and rows[0]['actor_guid'] is None
assert rows[1]['event'] == 'local_console_locked' and rows[1]['operation_id'] == latest['id']
passed('after explicit crash-tail archival, a superseded receipt resolves from a durable root checkpoint')

before = log.read_bytes()
run(['--auth-lock', str(state)])
assert lookup(crashed)['outcome'] == 'applied' and lookup(latest)['outcome'] == 'applied'
assert log.read_bytes().startswith(before)
passed('later actions retain earlier outcomes without rewriting retained records')

with log.open('rb') as held:
    fcntl.flock(held, fcntl.LOCK_EX)
    prior_id = status()['last_operation']['id']
    start = time.monotonic()
    result = run(['--auth-lock', str(state)], allowed=False)
    assert time.monotonic() - start < 5 and b'Console is locked' in result.stderr
    assert status()['locked'] and status()['last_operation']['id'] != prior_id
passed('busy checkpoint journal fails promptly after lockdown commits and reports partial failure')

log.write_bytes(b'partial')
prior_id = status()['last_operation']['id']
result = run(['--auth-lock', str(state)], allowed=False)
assert b'Console is locked' in result.stderr and status()['last_operation']['id'] != prior_id
assert log.read_bytes() == b'partial'
passed('damaged checkpoint journal cannot prevent access removal or silently repair history')

# Real directory verification must not permit granting access when preservation fails.
name = 'j-' + secrets.token_hex(5)
group = 'M1-Journal-' + secrets.token_hex(5)
password = 'Lab9!' + secrets.token_hex(18)
created_user = created_group = False

def directory_command(args):
    result = subprocess.run(args, capture_output=True, timeout=60)
    assert result.returncode == 0, 'Lab directory prerequisite failed; diagnostics suppressed'

try:
    directory_command(['samba-tool', 'user', 'create', name, password]); created_user = True
    directory_command(['samba-tool', 'group', 'add', group]); created_group = True
    directory_command(['samba-tool', 'group', 'addmembers', group, name])
    reader = evidence / 'reader-password'
    reader.write_text(json.loads((ROOT / 'passwords.json').read_text())['ldap-reader'])
    config = evidence / 'auth.json'
    config.write_text(json.dumps({'origin': 'http://127.0.0.1:18093', 'directory': {
        'url': 'ldaps://127.0.0.1', 'realm': 'M0.SMOLOFIS.TEST',
        'base_dn': 'DC=m0,DC=smolofis,DC=test',
        'admin_group_dn': f'CN={group},CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_dn': 'CN=ldap-reader,CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_password_file': str(reader), 'ca_file': '/var/lib/samba/private/tls/ca.pem'}}))
    before = state.read_bytes()
    result = run(['--auth-enroll', str(config), str(state), name], allowed=False, password=password.encode())
    assert b'previous operation receipt could not be preserved' in result.stderr
    assert state.read_bytes() == before and log.read_bytes() == b'partial'
    passed('verified LDAPS administrator cannot replace enrollment when the prior receipt cannot be journaled')
    log.write_bytes(b'')
    run(['--auth-enroll', str(config), str(state), name], password=password.encode())
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r['event'] for r in rows] == ['local_receipt_preserved', 'local_enrollment_authorized', 'local_enrollment_applied']
    assert not status()['locked'] and rows[0]['operation_id'] == json.loads(before)['last_operation']['id']
    assert password not in log.read_text() and name not in log.read_text()
    passed('successful re-enrollment checkpoints the old receipt before authorization and access commit')
finally:
    if created_user:
        directory_command(['samba-tool', 'user', 'delete', name])
    if created_group:
        directory_command(['samba-tool', 'group', 'delete', group])
    if (evidence / 'reader-password').exists():
        (evidence / 'reader-password').unlink()

# A checkpoint cannot turn an unvalidated/foreign state into trusted evidence.
foreign = json.loads(state.read_text()); foreign['machine_id_digest'] = 'ab' * 32
state.write_text(json.dumps(foreign))
unchanged = state.read_bytes()
unchanged_log = log.read_bytes()
run(['--auth-lock', str(state)], allowed=False)
assert state.read_bytes() == unchanged and log.read_bytes() == unchanged_log
run(['--auth-lock', str(state)], uid=65534, allowed=False)
passed('foreign appliance state and non-root callers cannot manufacture receipt evidence')

report = {'checks': checks, 'source_receipt_report': str(prior), 'crashed_receipt': crashed,
          'crash_signal': 'SIGXFSZ', 'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
          'directory_contacted': True, 'synthetic_directory_accounts_removed': True, 'production_modified': False,
          'lockdown_checkpoint_crash_window_remains': True}
(evidence / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} receipt-journal checks; evidence retained at {evidence}', flush=True)
