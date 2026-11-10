"""Root-only operation discovery through the real wrapper in a private VM namespace."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
SOURCE = Path('/opt/smolofis-operation-listing-lab')
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel lab VM required')
if not sys.argv[1:]:
    created = []
    try:
        path = Path('/var/lib/smolofis')
        if not path.exists():
            path.mkdir(mode=0o755)
            created.append(path)
        subprocess.run(['unshare', '--mount', '--fork', sys.executable, __file__, '--inside'],
                       check=True, timeout=180)
    finally:
        for path in reversed(created):
            path.rmdir()
    raise SystemExit(0)
assert sys.argv[1:] == ['--inside']
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
for target in ('/usr/local/bin', '/var/lib/smolofis'):
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=48m,mode=0755,nodev,nosuid', 'tmpfs', target], check=True)
for name in ('smolofis-panel', 'smolofis-admin'):
    shutil.copyfile(SOURCE / name, '/usr/local/bin/' + name)
    Path('/usr/local/bin/' + name).chmod(0o755)
os.umask(0o077)
evidence = ROOT / ('operation-listing-' + secrets.token_hex(5))
evidence.mkdir(mode=0o700)
base = Path('/var/lib/smolofis/local-audit')
base.mkdir(mode=0o700)
log = base / 'events.jsonl'
source = max(ROOT.glob('operation-receipts-*/report.json'), key=lambda p: p.stat().st_mtime)
normal = (source.parent / 'normal-events.jsonl').read_bytes()
crash = (source.parent / 'crashed-events').read_bytes()
log.write_bytes(normal)
checks = []

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def query(path=None, uid=0, allowed=True, extra=()):
    env = {k: v for k, v in os.environ.items() if not k.startswith('SMOLOFIS_')}
    if path is not None:
        env['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(path)
    result = subprocess.run(['/usr/local/bin/smolofis-admin', 'console-operation-list', *extra],
                            env=env, capture_output=True, timeout=10, user=uid, group=uid, extra_groups=())
    assert (result.returncode == 0) == allowed, result.stderr.decode()
    return json.loads(result.stdout) if allowed else result

listing = query()
assert listing['source'] == 'selected_audit_file' and not listing['incomplete_tail']
assert {op['operation'] for op in listing['operations']} == {'enrollment', 'lockdown'}
assert all(op['outcome'] == 'applied' for op in listing['operations'])
passed('packaged wrapper discovers completed operations from the standard log without LDAP or enrollment state')
archive = base / 'archive.jsonl'
archive.write_bytes(normal)
archive.chmod(0o400)
assert query(archive) == listing
passed('explicit root override reads a protected read-only archive')
crashed = base / 'crashed.jsonl'
crashed.write_bytes(crash)
result = query(crashed)
assert result['incomplete_tail'] and any(op['outcome'] == 'unknown' for op in result['operations'])
passed('retained real crash history flags its partial tail and preserves unknown outcomes')
with archive.open('rb') as held:
    fcntl.flock(held, fcntl.LOCK_EX)
    start = time.monotonic()
    query(archive, allowed=False)
    assert time.monotonic() - start < 5
assert query(archive) == listing
passed('busy history fails promptly and becomes readable after unlocking')
public = base / 'public'; public.write_bytes(normal); public.chmod(0o644)
wrong = base / 'wrong-owner'; wrong.write_bytes(normal); os.chown(wrong, 65534, 65534)
link = base / 'symlink'; link.symlink_to(archive)
hard = base / 'hardlink'; os.link(archive, hard)
for path in (public, wrong, link, hard):
    query(path, allowed=False)
hard.unlink()
passed('Linux ownership, permissions, symlink and hardlink violations are rejected')
bad = base / 'bad'
for content in (normal + b'invalid\n', b'{"version":99}\n'):
    bad.write_bytes(content)
    query(bad, allowed=False)
row = next(json.loads(line) for line in normal.splitlines() if json.loads(line)['event'] == 'local_enrollment_applied')
bad.write_text(json.dumps(row) + '\n')
query(bad, allowed=False)
passed('corrupt, unsupported and orphan completion history fails rather than returning fabricated results')
large = base / 'oversized'
with large.open('wb') as stream:
    stream.truncate(16 * 1024 * 1024 + 1)
query(large, allowed=False)
empty = base / 'empty'; empty.touch(mode=0o600)
assert query(empty)['operations'] == []
query(base / 'missing', allowed=False)
passed('oversized and missing files fail; a valid empty log returns an empty listing')
assert b'root' in query(uid=65534, allowed=False).stderr
query(extra=('unexpected',), allowed=False)
assert log.read_bytes() == normal and archive.read_bytes() == normal and crashed.read_bytes() == crash
assert archive.stat().st_mode & 0o777 == 0o400
passed('root-only and argument enforcement preserve standard log, read-only archive and crash bytes')
report = {'checks': checks, 'binary_sha256': hashlib.file_digest((SOURCE / 'smolofis-panel').open('rb'), 'sha256').hexdigest(),
          'wrapper_sha256': hashlib.file_digest((SOURCE / 'smolofis-admin').open('rb'), 'sha256').hexdigest(),
          'source_receipt_report': str(source), 'private_mount_namespace': True, 'production_modified': False}
(evidence / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} native wrapper checks; evidence retained at {evidence}', flush=True)
