"""Root archival acceptance in the owned full-kernel VM; preserve evidence."""
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
BINARY = Path('/opt/smolofis-panel-audit-lab/smolofis-panel')
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel lab VM required')
os.umask(0o077)
directory = ROOT / ('audit-archive-' + secrets.token_hex(5))
directory.mkdir(mode=0o700)
checks = []

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def fixture(name, content=b'{"partial":'):
    folder = directory / name
    folder.mkdir(mode=0o700)
    source, destination = folder / 'events.jsonl', folder / 'archives'
    source.write_bytes(content)
    source.chmod(0o600)
    destination.mkdir(mode=0o700)
    return source, destination

def archive(source, destination, *, uid=0, limit=None):
    def limits():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    return subprocess.run([str(BINARY), '--auth-audit-archive', str(source), str(destination)],
        capture_output=True, timeout=20, user=uid, group=uid, extra_groups=(),
        preexec_fn=limits if limit is not None else None)

source, destination = fixture('success')
original, inode = source.read_bytes(), source.stat().st_ino
result = archive(source, destination)
assert result.returncode == 0, result.stderr
response = json.loads(result.stdout)
snapshot = Path(response['archive'])
manifest = json.loads(snapshot.with_suffix('.manifest.json').read_text())
assert snapshot.read_bytes() == original and source.read_bytes() == b''
assert source.stat().st_ino != inode and source.stat().st_uid == 0
assert source.stat().st_mode & 0o777 == 0o600
assert snapshot.stat().st_mode & 0o777 == 0o400 and snapshot.stat().st_uid == 0
assert manifest['sha256'] == response['sha256'] == hashlib.sha256(original).hexdigest()
assert manifest['status'] == 'snapshot_saved' and response['current_log_replaced']
passed('Linux root CLI preserves exact crash-tail bytes and SHA256 before replacing the inode with a protected empty log')

source, destination = fixture('busy')
original = source.read_bytes()
with source.open('rb') as writer:
    fcntl.flock(writer, fcntl.LOCK_EX)
    start = time.monotonic()
    result = archive(source, destination)
    assert result.returncode and b'busy' in result.stderr and time.monotonic() - start < 10
assert source.read_bytes() == original and not list(destination.iterdir())
passed('active writer lock causes immediate refusal without modifying source or archive')

for name in ('public', 'owner', 'hard', 'symbolic', 'oversized'):
    source, destination = fixture(name)
    if name == 'public':
        source.chmod(0o644)
    elif name == 'owner':
        os.chown(source, 65534, 65534)
    elif name == 'hard':
        os.link(source, source.parent / 'other-link')
    elif name == 'symbolic':
        source.rename(source.parent / 'real')
        source.symlink_to(source.parent / 'real')
    else:
        with source.open('wb') as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    assert archive(source, destination).returncode and not list(destination.iterdir())
assert b'require root' in archive(source, destination, uid=65534).stderr
passed('Linux ownership, public modes, hard links, symlinks, oversize and non-root callers are rejected')

source, destination = fixture('concurrent', b'concurrent-history\n' * 1024)
original = source.read_bytes()
args_archive = [str(BINARY), '--auth-audit-archive', str(source), str(destination)]
processes = [subprocess.Popen(args_archive, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
results = [(process, process.communicate(timeout=20)) for process in processes]
successes = [json.loads(output[0]) for process, output in results if process.returncode == 0]
assert successes and all(process.returncode == 0 or b'busy' in output[1] or b'changed' in output[1]
                         for process, output in results)
assert any(Path(result['archive']).read_bytes() == original for result in successes)
assert source.read_bytes() == b''
passed('concurrent root archival preserves the original history with only successful or safely refused contenders')

source, destination = fixture('capacity')
for index in range(64):
    (destination / str(index)).write_bytes(b'retained')
assert archive(source, destination).returncode
assert source.read_bytes() == b'{"partial":' and len(list(destination.iterdir())) == 64
passed('full archive refuses replacement and retains every existing entry')

source, destination = fixture('crash', b'x' * 32768)
original, inode = source.read_bytes(), source.stat().st_ino
result = archive(source, destination, limit=4096)
assert result.returncode == -signal.SIGXFSZ, result.stderr
assert source.read_bytes() == original and source.stat().st_ino == inode
partial = list(destination.glob('*.jsonl'))
assert len(partial) == 1 and partial[0].read_bytes() == original[:4096]
assert not list(destination.glob('*.manifest.json'))
result = archive(source, destination)
assert result.returncode == 0, result.stderr
assert Path(json.loads(result.stdout)['archive']).read_bytes() == original
assert partial[0].read_bytes() == original[:4096]
passed('real SIGXFSZ during snapshot preserves the live log; retry saves complete history without deleting partial evidence')

# A root enrollment process opens its audit descriptor before reading stdin.
reports = list(ROOT.glob('local-audit-*/report.json'))
previous_path = max(reports, key=lambda p: p.stat().st_mtime)
previous = json.loads(previous_path.read_text())
assert previous['binary_sha256'] == hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest()
fixture_path = Path(previous['runtime'])
assert fixture_path.parent == Path('/run') and fixture_path.name.startswith('smolofis-local-audit-')
config = fixture_path / 'auth.json'
password = json.loads((previous_path.parent / 'passwords.json').read_text())['admin']
admin = 'a-' + fixture_path.name.rsplit('-', 1)[1]
source, destination = fixture('stale', b'')
state = source.parent / 'state.json'
environment = {k: v for k, v in os.environ.items() if not k.startswith('SMOLOFIS_')}
environment['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(source)
args = [str(BINARY), '--auth-enroll', str(config), str(state), admin]
process = subprocess.Popen(args, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    deadline = time.monotonic() + 15
    while True:
        descriptors = Path(f'/proc/{process.pid}/fd')
        if any(p.resolve() == source for p in descriptors.iterdir()):
            break
        assert process.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    result = archive(source, destination)
    assert result.returncode == 0, result.stderr
    snapshot = Path(json.loads(result.stdout)['archive'])
    stdout, stderr = process.communicate(password.encode(), timeout=60)
    assert process.returncode and b'replaced or permissions changed' in stderr, stderr
    assert not state.exists() and snapshot.read_bytes() == b'' and source.read_bytes() == b''
finally:
    if process.poll() is None:
        process.kill()
        process.communicate()
passed('real enrollment with an open old descriptor fails before state mutation and cannot append to archived history')
result = subprocess.run(args, env=environment, input=password.encode(), capture_output=True, timeout=60)
assert result.returncode == 0, result.stderr
assert state.exists() and len(source.read_text().splitlines()) == 2 and snapshot.read_bytes() == b''
passed('fresh enrollment opens the replacement log and records both correlated events')

report = {'checks': checks, 'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
          'production_modified': False, 'power_loss_and_sync_failure_tested': False}
(directory / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} native archival checks; evidence retained at {directory}', flush=True)
