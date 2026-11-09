"""Run the shipped admin wrapper in a private mount namespace on the owned VM."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
BINARY = Path('/opt/smolofis-panel-audit-lab/smolofis-panel')
WRAPPER = Path('/opt/smolofis-identity-lab/vm/smolofis-admin.sh')
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel lab VM required')
if sys.argv[1:] in ([], ['--lifecycle']):
    created = []
    try:
        for path in (Path('/var/lib/smolofis'), Path('/etc/smolofis')):
            if not path.exists():
                path.mkdir(mode=0o755)
                created.append(path)
        subprocess.run(['unshare', '--mount', '--fork', sys.executable, __file__,
                        '--inside-lifecycle' if sys.argv[1:] else '--inside'], check=True, timeout=180)
    finally:
        for path in reversed(created):
            path.rmdir()  # Never recursively remove a mount or pre-existing directory.
    raise SystemExit(0)
assert sys.argv[1:] in (['--inside'], ['--inside-lifecycle'])
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
for path in ('/usr/local/bin', '/var/lib/smolofis', '/etc/smolofis'):
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=32m,mode=0755,nodev,nosuid', 'tmpfs', path], check=True)
shutil.copyfile(BINARY, '/usr/local/bin/smolofis-panel')
Path('/usr/local/bin/smolofis-panel').chmod(0o755)
shutil.copyfile(WRAPPER, '/usr/local/bin/smolofis-admin')
Path('/usr/local/bin/smolofis-admin').chmod(0o755)
os.umask(0o077)
evidence = ROOT / ('audit-workflow-' + secrets.token_hex(5))
evidence.mkdir(mode=0o700)
if sys.argv[1:] == ['--inside-lifecycle']:
    helper = Path('/opt/smolofis-identity-lab/vm/verify-audit-lifecycle.sh')
    subprocess.run(['bash', str(helper), 'prepare'], check=True, timeout=30)
    subprocess.run(['bash', str(helper), 'verify'], check=True, timeout=30)
    state_path = Path('/etc/smolofis/panel-enrollment.json')
    before = state_path.read_bytes()
    assert subprocess.run(['bash', str(helper), 'prepare'], capture_output=True).returncode != 0
    assert state_path.read_bytes() == before
    state_path.write_bytes(before + b' ')
    assert subprocess.run(['bash', str(helper), 'verify'], capture_output=True).returncode != 0
    state_path.write_bytes(before)
    subprocess.run(['bash', str(helper), 'verify'], check=True, timeout=30)
    report = {'checks': ['prepare and verify locked schema-v2 fixture',
                         'refuse fixture overwrite', 'detect changed state bytes'],
              'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
              'helper_sha256': hashlib.file_digest(helper.open('rb'), 'sha256').hexdigest(),
              'private_mount_namespace': True, 'actual_ab_boot_tested': False,
              'production_modified': False}
    (evidence / 'report.json').write_text(json.dumps(report, indent=2))
    print('PASS: three lifecycle gate component checks; evidence retained at ' + str(evidence), flush=True)
    raise SystemExit(0)
previous_path = max(ROOT.glob('local-audit-*/report.json'), key=lambda p: p.stat().st_mtime)
previous = json.loads(previous_path.read_text())
assert previous['binary_sha256'] == hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest()
fixture = Path(previous['runtime'])
assert fixture.parent == Path('/run') and fixture.name.startswith('smolofis-local-audit-')
password = json.loads((previous_path.parent / 'passwords.json').read_text())['admin']
admin = 'a-' + fixture.name.rsplit('-', 1)[1]
shutil.copyfile(fixture / 'auth.json', '/etc/smolofis/panel-auth.json')
base = Path('/var/lib/smolofis/local-audit')
state = Path('/etc/smolofis/panel-enrollment.json')
log = base / 'events.jsonl'
checks = []

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def command(*args, allowed=True, override=None, uid=0):
    environment = {k: v for k, v in os.environ.items() if not k.startswith('SMOLOFIS_')}
    if override is not None:
        environment['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(override)
    result = subprocess.run(['/usr/local/bin/smolofis-admin', *args], env=environment,
        input=password.encode() if args[0] == 'console-enroll' else b'',
        capture_output=True, timeout=60, user=uid, group=uid, extra_groups=())
    assert (result.returncode == 0) == allowed, (args[0], result.stderr)
    return result

assert not base.exists()
command('console-enroll', admin)
assert state.exists() and not base.exists()
passed('packaged root enrollment retains opt-in semantics before audit initialization')
result = command('console-audit-init')
assert json.loads(result.stdout)['history_preserved']
assert base.stat().st_mode & 0o777 == 0o700 and (base / 'archives').stat().st_mode & 0o777 == 0o700
assert log.stat().st_mode & 0o777 == 0o600
command('console-enroll', admin)
receipt = json.loads(command('console-status').stdout)['last_operation']
assert len(log.read_text().splitlines()) == 2
original, inode = log.read_bytes(), log.stat().st_ino
command('console-audit-init')
assert log.read_bytes() == original and log.stat().st_ino == inode
passed('explicit initialization provisions private storage; repeat initialization preserves inode and all history')
command('console-lock')
assert len(log.read_text().splitlines()) == 3
result = json.loads(command('console-operation-status', receipt['id']).stdout)
assert result['source'] == 'audit_history' and result['outcome'] == 'applied'
passed('wrapper automatically selects initialized history for enrollment, lockdown and historical receipt lookup')
original = log.read_bytes()
result = json.loads(command('console-audit-archive').stdout)
archive = Path(result['archive'])
assert archive.read_bytes() == original and log.read_bytes() == b''
assert json.loads(command('console-operation-status', receipt['id'], override=archive).stdout)['outcome'] == 'applied'
command('console-enroll', admin)
assert len(log.read_text().splitlines()) == 2 and archive.read_bytes() == original
passed('fixed-path archive command preserves history, supports explicit archived-ID inspection and fresh enrollment')
assert b'standard local log' in command('console-audit-archive', override=archive, allowed=False).stderr
assert b'no additional arguments' in command('console-audit-init', 'unexpected', allowed=False).stderr
assert b'must run as root' in command('console-audit-init', uid=65534, allowed=False).stderr
passed('maintenance rejects custom-path ambiguity, extra arguments and non-root callers')
log.unlink()
assert command('console-enroll', admin, allowed=False).returncode
assert b'Console is locked' in command('console-lock', allowed=False).stderr
assert json.loads(command('console-status').stdout)['locked']
assert not log.exists()
passed('missing opted-in log blocks enrollment while lockdown still persists with explicit audit failure')
# Reinitialization must reject unsafe aliases, without altering their target.
target = evidence / 'untouched'
target.write_bytes(b'preserved')
log.symlink_to(target)
command('console-audit-init', allowed=False)
assert target.read_bytes() == b'preserved'
log.unlink()
command('console-audit-init')
log.write_bytes(b'{"crash-tail":')
command('console-audit-init')
assert log.read_bytes() == b'{"crash-tail":'
command('console-enroll', admin, allowed=False)
passed('initialization rejects symlinks and preserves incomplete history instead of silently repairing it')
report = {'checks': checks, 'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
          'wrapper_sha256': hashlib.file_digest(WRAPPER.open('rb'), 'sha256').hexdigest(),
          'private_mount_namespace': True, 'production_modified': False}
(evidence / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} packaged workflow checks; evidence retained at {evidence}', flush=True)
