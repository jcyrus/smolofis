"""Root CLI historical lookup against retained real crash evidence; no LDAP."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
BINARY = Path('/opt/smolofis-panel-audit-lab/smolofis-panel')
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel lab VM required')
reports = list(ROOT.glob('operation-receipts-*/report.json'))
if not reports:
    raise SystemExit('Run operation-receipt acceptance first')
source = max(reports, key=lambda path: path.stat().st_mtime)
previous = json.loads(source.read_text())
os.umask(0o077)
directory = ROOT / ('operation-history-' + secrets.token_hex(5))
directory.mkdir(mode=0o700)
state, archive, crash = directory / 'state.json', directory / 'archive.jsonl', directory / 'crashed-events'
shutil.copyfile(previous['state'], state)
shutil.copyfile(source.parent / 'normal-events.jsonl', archive)
shutil.copyfile(source.parent / 'crashed-events', crash)
archive.chmod(0o400)
crash.chmod(0o400)
events = [json.loads(line) for line in archive.read_text().splitlines()]
enrollment = next(event['operation_id'] for event in events if event['event'] == 'local_enrollment_applied')
lockdown = next(event['operation_id'] for event in events if event['event'] == 'local_console_locked')
latest = previous['last_operation']['id']
crashed = previous['crashed_operation']['id']
checks = []
state_before, archive_before = state.read_bytes(), archive.read_bytes()

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def query(operation_id, log=archive, uid=0, allowed=True):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}
    environment.update(SMOLOFIS_LOCAL_AUDIT_FILE=str(log), SMOLOFIS_AUTH_CONFIG='/nonexistent')
    result = subprocess.run([str(BINARY), '--auth-operation-status', str(state), operation_id],
        env=environment, capture_output=True, timeout=15, user=uid, group=uid, extra_groups=())
    assert (result.returncode == 0) == allowed
    return json.loads(result.stdout) if allowed else result

for operation_id, kind in ((enrollment, 'enrollment'), (lockdown, 'lockdown')):
    result = query(operation_id.upper())
    assert result['source'] == 'audit_history' and result['outcome'] == 'applied'
    assert result['operation'] == kind and result['audit_status'] == 'complete'
    assert result['operation_id'] == operation_id and result['directory_checked'] is False
assert archive.stat().st_mode & 0o777 == 0o400
passed('older completed enrollment/lockdown IDs resolve from a read-only archive after a newer state receipt')

result = query(crashed, log=crash)
assert result['source'] == 'audit_history' and result['found'] and result['outcome'] == 'unknown'
assert result['operation'] == 'enrollment' and result['audit_status'] == 'incomplete_tail'
passed('real crash authorization without a completion remains unknown after its receipt was superseded')
result = query(latest, log=directory / 'missing.jsonl')
assert result['source'] == 'state_receipt' and result['outcome'] == 'applied'
assert result['audit_status'] == 'not_checked'
passed('current state receipt resolves independently of unavailable audit history')
result = query('fe' * 32)
assert not result['found'] and result['outcome'] == 'unknown'
passed('absent operation is unknown rather than fabricated as failed or applied')

with archive.open('rb') as writer:
    fcntl.flock(writer, fcntl.LOCK_EX)
    start = time.monotonic()
    result = query(enrollment)
    assert time.monotonic() - start < 10
    assert result['outcome'] == 'unknown' and result['audit_status'] == 'unavailable'
    fcntl.flock(writer, fcntl.LOCK_UN)
assert query(enrollment)['outcome'] == 'applied'
passed('busy writer never blocks historical lookup and unlocking restores the result')

bad = directory / 'corrupt.jsonl'
bad.write_bytes(archive_before + b'not-json\n')
conflict = directory / 'conflict.jsonl'
row = next(event.copy() for event in events if event['event'] == 'local_console_locked')
row['operation_id'] = enrollment
conflict.write_bytes(archive_before + json.dumps(row).encode() + b'\n')
orphan = directory / 'orphan.jsonl'
row = next(event for event in events if event['event'] == 'local_enrollment_applied')
orphan.write_bytes(json.dumps(row).encode() + b'\n')
for log in (bad, conflict, orphan):
    before = log.read_bytes()
    result = query(enrollment, log)
    assert result['outcome'] == 'unknown' and result['audit_status'] == 'unavailable'
    assert log.read_bytes() == before
passed('corruption, conflicting kinds, and orphan completions cannot fabricate an applied historical outcome')

public = directory / 'public.jsonl'
public.write_bytes(archive_before)
public.chmod(0o644)
wrong_owner = directory / 'wrong-owner.jsonl'
wrong_owner.write_bytes(archive_before)
os.chown(wrong_owner, 65534, 65534)
link = directory / 'symlink.jsonl'
link.symlink_to(archive)
hard = directory / 'hardlink.jsonl'
os.link(archive, hard)
try:
    for log in (public, wrong_owner, link, hard):
        result = query(enrollment, log)
        assert result['outcome'] == 'unknown' and result['audit_status'] == 'unavailable'
finally:
    hard.unlink()
oversized = directory / 'oversized.jsonl'
with oversized.open('wb') as stream:
    stream.truncate(16 * 1024 * 1024 + 1)
assert query(enrollment, oversized)['audit_status'] == 'unavailable'
passed('unsafe ownership/mode/links and oversized history are rejected by the Linux CLI')
assert b'require root' in query(enrollment, uid=65534, allowed=False).stderr
assert b'64 hex characters' in query('invalid-id', allowed=False).stderr
assert state.read_bytes() == state_before and archive.read_bytes() == archive_before
passed('root-only and bounded-ID enforcement leave state and archive bytes unchanged')
report = {'checks': checks, 'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
          'source_receipt_report': str(source), 'directory_contacted': False,
          'history_opened_readonly': True, 'production_modified': False}
(directory / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} operation-history checks; private evidence retained at {directory}', flush=True)
