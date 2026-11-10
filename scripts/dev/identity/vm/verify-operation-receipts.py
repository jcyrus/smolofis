"""Separate root processes and a real audit-completion crash in the owned VM."""
import hashlib
import json
import os
from pathlib import Path
import resource
import secrets
import shutil
import signal
import subprocess
import time

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
BINARY = Path('/opt/smolofis-panel-audit-lab/smolofis-panel')
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel lab VM required')
reports = list(ROOT.glob('local-audit-*/report.json'))
if not reports:
    raise SystemExit('Run check-local-audit with the rebuilt binary first')
source = max(reports, key=lambda path: path.stat().st_mtime)
previous = json.loads(source.read_text())
assert previous['binary_sha256'] == hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest()
fixture = Path(previous['runtime'])
assert fixture.parent == Path('/run') and fixture.name.startswith('smolofis-local-audit-')
if not (fixture / 'auth.json').is_file():
    raise SystemExit('Run check-local-audit after reboot before operation-receipt acceptance')
admin = 'a-' + fixture.name.rsplit('-', 1)[1]
password = json.loads((source.parent / 'passwords.json').read_text())['admin']
directory = ROOT / ('operation-receipts-' + secrets.token_hex(5))
directory.mkdir(mode=0o700)
config, state, log = directory / 'config.json', directory / 'state.json', directory / 'events.jsonl'
shutil.copyfile(fixture / 'auth.json', config)
config.chmod(0o600)
log.touch(mode=0o600)
environment = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}
environment['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(log)
checks = []

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def command(operation, *, limit=None):
    args = [str(BINARY), operation]
    args += [str(config), str(state), admin] if operation == '--auth-enroll' else [str(state)]
    def file_limit():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    return subprocess.run(args, env=environment, input=(password if operation == '--auth-enroll' else '').encode(),
        capture_output=True, timeout=60, preexec_fn=file_limit if limit is not None else None)

def status():
    result = command('--auth-status')
    assert result.returncode == 0
    return json.loads(result.stdout)

assert command('--auth-enroll').returncode == 0
initial = status()
receipt = initial['last_operation']
assert initial['version'] == 2 and receipt['operation'] == 'enrollment' and receipt['outcome'] == 'applied'
events = [json.loads(line) for line in log.read_text().splitlines()]
assert [event['event'] for event in events] == ['local_enrollment_authorized', 'local_enrollment_applied']
assert all(event['operation_id'] == receipt['id'] for event in events)
assert events[-1]['outcome'] == 'applied'
passed('independent CLI status process recovers the atomically stored enrollment receipt and correlated outcome')
assert command('--auth-lock').returncode == 0
locked = status()
assert locked['locked'] and locked['last_operation']['id'] != receipt['id']
assert locked['last_operation']['operation'] == 'lockdown'
shutil.copyfile(log, directory / 'normal-events.jsonl')
passed('lockdown has its own durable operation ID and applied receipt')

# A limit slightly beyond the synced authorization event permits state writing
# but kills the process during the completion event. No core or global limits.
padding = json.dumps({'version': 1, 'timestamp_ms': 0, 'event': 'login_denied', 'actor_guid': None}).encode()
with log.open('wb') as stream:
    stream.write(padding + b' ' * (16384 - len(padding) - 1) + b'\n')
sample = {'version': 1, 'timestamp_ms': int(time.time() * 1000),
          'event': 'local_enrollment_authorized', 'actor_guid': initial['first_admin_guid'],
          'actor_uid': 0, 'operation_id': '0' * 64, 'operation': 'enrollment'}
authorization_bytes = len(json.dumps(sample, separators=(',', ':')).encode()) + 1
# Re-enrollment now durably checkpoints the previous receipt before authorizing
# a replacement. Allow that complete record before crashing the new completion.
checkpoint = {'version': 1, 'timestamp_ms': sample['timestamp_ms'],
              'event': 'local_receipt_preserved', 'actor_guid': None, 'actor_uid': 0,
              'operation_id': locked['last_operation']['id'], 'operation': 'lockdown',
              'outcome': 'applied'}
checkpoint_bytes = len(json.dumps(checkpoint, separators=(',', ':')).encode()) + 1
result = command('--auth-enroll', limit=16384 + checkpoint_bytes + authorization_bytes + 1)
assert result.returncode == -signal.SIGXFSZ
after_crash = status()
crashed = after_crash['last_operation']
assert not after_crash['locked'] and crashed['id'] != locked['last_operation']['id']
assert crashed['operation'] == 'enrollment' and crashed['outcome'] == 'applied'
raw = log.read_bytes()
assert not raw.endswith(b'\n')
complete = [json.loads(line) for line in raw.split(b'\n')[:-1]]
assert complete[-1]['event'] == 'local_enrollment_authorized'
assert complete[-1]['operation_id'] == crashed['id']
assert not any(event.get('event') == 'local_enrollment_applied' for event in complete)
shutil.copyfile(log, directory / 'crashed-events')
passed('real completion-write crash leaves durable authorization and an applied state receipt visible after process restart')

result = command('--auth-lock')
assert result.returncode != 0 and b'Console is locked' in result.stderr
final = status()
assert final['locked'] and final['last_operation']['operation'] == 'lockdown'
assert final['last_operation']['outcome'] == 'applied'
assert final['last_operation']['id'].encode() in result.stderr
assert log.read_bytes() == raw
passed('lockdown retains its applied receipt despite corrupt audit history and reports the same ID in the partial-failure error')
report = {'checks': checks, 'state': str(state), 'last_operation': final['last_operation'],
          'crashed_operation': crashed, 'binary_sha256': previous['binary_sha256'],
          'crash_signal': 'SIGXFSZ', 'production_modified': False,
          'kernel_reboot_verified': False}
(directory / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} operation-receipt checks; private evidence retained at {directory}', flush=True)
