"""Native journal entry points, process interruption and sync failure in a private namespace."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import secrets
import time
from receipt_fault_tools import traced

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
SOURCE = Path('/opt/smolofis-operation-journal-lab')
BINARY = SOURCE / 'smolofis-panel'
if (os.geteuid() != 0 or platform.machine() != 'x86_64'
        or not Path('/etc/smolofis-m0-fullvm').exists() or not (ROOT / 'domain-ready').exists()
        or Path('/etc/smolofis-release').exists() or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel x86 VM required')
if not sys.argv[1:]:
    created = []
    try:
        for path in (Path('/var/lib/smolofis'), Path('/etc/smolofis')):
            if not path.exists():
                path.mkdir(mode=0o755); created.append(path)
        subprocess.run(['unshare', '--mount', '--fork', sys.executable, __file__, '--inside'], check=True, timeout=180)
    finally:
        for path in reversed(created):
            path.rmdir()
    raise SystemExit(0)
assert sys.argv[1:] == ['--inside']
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
for path in ('/usr/local/bin', '/var/lib/smolofis', '/etc/smolofis'):
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=48m,mode=0755,nodev,nosuid', 'tmpfs', path], check=True)
for name in ('smolofis-panel', 'smolofis-admin'):
    shutil.copyfile(SOURCE / name, '/usr/local/bin/' + name)
    Path('/usr/local/bin/' + name).chmod(0o755)
os.umask(0o077)
identity = Path('/etc/smolofis/identity'); identity.mkdir(mode=0o700)
shutil.copyfile('/etc/machine-id', identity / 'machine-id')
evidence = ROOT / ('operation-journal-' + secrets.token_hex(5)); evidence.mkdir(mode=0o700)
checks, cases = [], []
base = Path('/var/lib/smolofis/operations')
env = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}

def command(args, allowed=True, uid=0):
    result = subprocess.run(args, env=env, capture_output=True, timeout=15, user=uid, group=uid, extra_groups=())
    assert (result.returncode == 0) == allowed, result.stderr.decode()
    return json.loads(result.stdout) if allowed else result

def wrapper(action, *args, **kwargs):
    return command(['/usr/local/bin/smolofis-admin', 'console-job-' + action, *args], **kwargs)

def direct(action, folder):
    return command([str(BINARY), '--operation-' + action, str(folder)])

def passed(label):
    checks.append(label); print('PASS: ' + label, flush=True)

wrapper('init')
first = wrapper('run')
assert first['outcome'] == 'identity_verified' and first['actor_uid'] == 0
assert [t['phase'] for t in first['transitions']] == ['queued', 'running', 'succeeded']
assert wrapper('status', first['id'].upper()) == first
assert wrapper('list')['operations'] == [first]
before = (base / 'journal.json').read_bytes()
wrapper('init')
assert (base / 'journal.json').read_bytes() == before
assert (base / 'journal.json').stat().st_mode & 0o777 == 0o600
passed('packaged root commands persist ordered execution and independent-process status without resetting history')

with (base / 'journal.lock').open('rb') as held:
    fcntl.flock(held, fcntl.LOCK_EX)
    start = time.monotonic(); wrapper('run', allowed=False)
    assert time.monotonic() - start < 5
assert (base / 'journal.json').read_bytes() == before
assert b'root' in wrapper('run', allowed=False, uid=65534).stderr
wrapper('status', 'bad-id', allowed=False)
passed('live lock contention, unprivileged callers and malformed IDs fail without changing stored operations')

saved_identity = (identity / 'machine-id').read_bytes()
(identity / 'machine-id').write_text('ab' * 16)
result = wrapper('run', allowed=False)
failed = json.loads(result.stdout)
assert failed['transitions'][-1]['phase'] == 'failed' and failed['outcome'] == 'identity_mismatch'
(identity / 'machine-id').write_bytes(saved_identity)
passed('actual identity mismatch records a typed failed result instead of success')

path = base / 'journal.json'; good = path.read_bytes()
record = json.loads(good); record['machine_id_digest'] = 'ab' * 32
path.write_text(json.dumps(record)); foreign = path.read_bytes()
wrapper('run', allowed=False); wrapper('recover', allowed=False)
assert path.read_bytes() == foreign
path.write_bytes(b'partial'); wrapper('recover', allowed=False)
assert path.read_bytes() == b'partial'
path.write_bytes(good); path.chmod(0o644); wrapper('run', allowed=False)
path.chmod(0o600)
passed('foreign-appliance binding, corrupt journal and unsafe permissions refuse execution and recovery')

for fault, phase, after in [('EIO', p, False) for p in range(1, 7)] + [('SIGKILL', p, False) for p in range(1, 7)] + [('SIGKILL', p, True) for p in (2, 4, 6)]:
    folder = evidence / f'{fault}-{phase}-{after}'; folder.mkdir(mode=0o700)
    direct('init', folder)
    code, count, paths, output, error = traced(BINARY, folder / 'journal.json', folder / 'unused-audit',
        args=['--operation-run', str(folder)], fail=phase if fault == 'EIO' else None,
        kill=phase if fault == 'SIGKILL' else None, after=after)
    assert not output
    if fault == 'EIO':
        assert code != 0 and b'os error 5' in error
    else:
        assert code == -signal.SIGKILL and count == phase
    data = direct('list', folder)
    phases = [op['transitions'][-1]['phase'] for op in data['operations']]
    snapshot = (folder / 'journal.json').read_bytes()
    unfinished = any(p in ('queued', 'running') for p in phases)
    if unfinished:
        command([str(BINARY), '--operation-run', str(folder)], allowed=False)
        assert (folder / 'journal.json').read_bytes() == snapshot
    recovered = direct('recover', folder)
    assert recovered['recovered'] == int(unfinished)
    data = json.loads((folder / 'journal.json').read_text())
    if unfinished:
        assert data['operations'][0]['outcome'] == 'interrupted'
        assert data['operations'][0]['transitions'][-1]['phase'] == 'unknown'
    else:
        assert (folder / 'journal.json').read_bytes() == snapshot
    assert direct('recover', folder)['recovered'] == 0
    prior_operations = data['operations']
    completed = direct('run', folder)
    assert completed['outcome'] == 'identity_verified'
    assert json.loads((folder / 'journal.json').read_text())['operations'][:-1] == prior_operations
    cases.append({'fault': fault, 'phase': phase, 'after_sync': after, 'sync_paths': paths,
                  'observed_phases': phases, 'recovered': recovered['recovered']})
    passed(f'{fault} {"after" if after else "before"} sync {phase} preserves valid history and recoverable unfinished work')

report = {'checks': checks, 'cases': cases,
          'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
          'wrapper_sha256': hashlib.file_digest((SOURCE / 'smolofis-admin').open('rb'), 'sha256').hexdigest(),
          'private_mount_namespace': True, 'production_modified': False,
          'hardware_power_loss_tested': False, 'appliance_ab_tested': False}
(evidence / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} native operation-journal checks; evidence retained at {evidence}', flush=True)
