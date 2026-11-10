"""Owned x86 VM only: ptrace sync/crash boundaries and pre-checkpoint reader checks."""
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import subprocess
from receipt_fault_tools import environment, traced as run_traced

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
BINARY = Path('/opt/smolofis-receipt-journal-lab/smolofis-panel')
OLDER = Path('/opt/smolofis-operation-listing-lab/smolofis-panel')
if (os.geteuid() != 0 or platform.machine() != 'x86_64'
        or not Path('/etc/smolofis-m0-fullvm').exists() or not (ROOT / 'domain-ready').exists()
        or Path('/etc/smolofis-release').exists() or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel x86 VM required')
os.umask(0o077)

prior = max(ROOT.glob('operation-receipts-*/report.json'), key=lambda p: p.stat().st_mtime)
template = json.loads(Path(json.loads(prior.read_text())['state']).read_text())
evidence = ROOT / ('receipt-faults-' + secrets.token_hex(5)); evidence.mkdir(mode=0o700)
checks = []

def passed(label):
    checks.append(label); print('PASS: ' + label, flush=True)

def sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def fixture(name):
    folder = evidence / name; folder.mkdir(mode=0o700)
    state, log = folder / 'state.json', folder / 'events.jsonl'
    record = dict(template, locked=False, version=2,
                  last_operation={'id': secrets.token_hex(32), 'operation': 'enrollment', 'outcome': 'applied'})
    state.write_text(json.dumps(record)); state.chmod(0o644)
    log.touch(mode=0o600)
    for path in (state, log, folder, evidence):
        sync(path)
    return state, log, state.read_bytes(), record['last_operation']['id']


def query(binary, state, log, operation, *args):
    result = subprocess.run([str(binary), operation, str(state), *args], env=environment(log),
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    return json.loads(result.stdout)

def traced(state, log, **kwargs):
    return run_traced(BINARY, state, log, **kwargs)

state, log, before, old = fixture('baseline')
code, count, paths, _, _ = traced(state, log)
assert code == 0 and count == 6
assert paths[:2] == [str(state), str(state.parent)]
assert Path(paths[2]).name.startswith('.panel-enrollment-')
assert paths[3:] == [str(state.parent), str(log), str(log)]
assert query(BINARY, state, log, '--auth-operation-status', old)['outcome'] == 'applied'
passed('baseline identifies six actual sync calls: old state/directory, new state/directory, checkpoint and completion')

cases = []
for phase in range(1, 7):
    state, log, before, old = fixture('eio-' + str(phase))
    code, count, paths, output, error = traced(state, log, fail=phase)
    assert code != 0 and not output and b'os error 5' in error, (phase, code, error)
    current = json.loads(state.read_text())
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    if phase == 3:
        assert state.read_bytes() == before
        assert rows[-1]['event'] == 'local_operation_uncertain'
    else:
        assert current['locked'] and current['last_operation']['id'] != old
        if phase in (1, 2):
            assert not rows
        elif phase == 4:
            assert rows[-1]['event'] == 'local_operation_uncertain'
        elif phase == 5:
            assert [r['event'] for r in rows] == ['local_receipt_preserved']
        else:
            assert [r['event'] for r in rows] == ['local_receipt_preserved', 'local_console_locked']
    if phase in (1, 2, 5, 6):
        assert b'Console is locked' in error
    cases.append({'fault': 'EIO', 'phase': phase, 'sync_paths': paths,
                  'state_changed': state.read_bytes() != before})
    passed(f'sync EIO at phase {phase} reports failure without claiming an unapplied change succeeded')

for phase, after in [(p, False) for p in range(1, 7)] + [(5, True), (6, True)]:
    state, log, before, old = fixture(f'kill-{phase}-' + ('after' if after else 'before'))
    code, count, paths, output, error = traced(state, log, kill=phase, after=after)
    assert code == -signal.SIGKILL and count == phase and not output
    current = json.loads(state.read_text())
    if phase <= 3:
        assert state.read_bytes() == before and not log.read_bytes()
    else:
        assert current['locked'] and current['last_operation']['id'] != old
        receipt = query(BINARY, state, log, '--auth-status')['last_operation']
        assert receipt['id'] == current['last_operation']['id'] and receipt['outcome'] == 'applied'
    if after:
        result = query(BINARY, state, log, '--auth-operation-status', old)
        assert result['source'] == 'audit_history' and result['outcome'] == 'applied'
    cases.append({'fault': 'SIGKILL', 'phase': phase, 'after_sync': after,
                  'sync_paths': paths, 'state_changed': state.read_bytes() != before})
    passed(f'process death {"after" if after else "before"} sync phase {phase} preserves interpretable state/history')

# Never let the old reader mutate checkpoint-bearing history during this test.
older_report = ROOT / 'operation-listing-0ec98ed3f7/report.json'
assert hashlib.file_digest(OLDER.open('rb'), 'sha256').hexdigest() == json.loads(older_report.read_text())['binary_sha256']
older_copy = evidence / 'pre-checkpoint-panel'
shutil.copyfile(OLDER, older_copy); older_copy.chmod(0o700)
OLDER = older_copy
state, log, before, old = fixture('older-reader')
assert traced(state, log)[0] == 0
saved_state, saved_log = state.read_bytes(), log.read_bytes()
current = query(OLDER, state, log, '--auth-status')
assert current['locked'] and current['last_operation']['outcome'] == 'applied'
historical = query(OLDER, state, log, '--auth-operation-status', old)
assert historical['outcome'] == 'unknown' and historical['audit_status'] == 'unavailable'
assert state.read_bytes() == saved_state and log.read_bytes() == saved_log
passed('verified pre-checkpoint binary reads current v2 receipt but refuses unsupported historical events without modifying bytes')
report = {'checks': checks, 'cases': cases,
          'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
          'older_binary_sha256': hashlib.file_digest(OLDER.open('rb'), 'sha256').hexdigest(),
          'injection': 'ptrace x86_64 fsync/fdatasync -EIO and process-only SIGKILL',
          'hardware_power_loss_tested': False, 'appliance_ab_rollback_tested': False,
          'production_modified': False}
(evidence / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} receipt fault/compatibility checks; private evidence retained at {evidence}', flush=True)
