"""Cold restore evidence for a synthetic shared file, including ACLs and owner."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

fixture = Path('/srv/shares/projects/persistence-fixture')
baseline = Path('/evidence/storage-baseline.json')


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=20).decode()


def state(path):
    metadata = path.stat()
    return {
        'uid': metadata.st_uid, 'gid': metadata.st_gid,
        'mode': metadata.st_mode & 0o7777,
        'acl': run('getfacl', '-cn', str(path)),
        'xattrs': {name: os.getxattr(path, name).hex() for name in os.listxattr(path)},
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
    }


if sys.argv[1] == 'snapshot':
    user = run('getent', 'passwd', 'LAB\\alice').split(':')
    group = run('getent', 'group', 'LAB\\Engineering').split(':')
    fixture.write_bytes(os.urandom(128))
    os.chown(fixture, int(user[2]), int(group[2]))
    os.chmod(fixture, 0o660)
    run('setfacl', '-m', 'g:LAB\\Readers:r', str(fixture))
    os.setxattr(fixture, 'user.smolofis-lab', b'cold-restore-probe')
    baseline.parent.mkdir(mode=0o700, exist_ok=True)
    baseline.write_text(json.dumps({'file': state(fixture), 'share': state(fixture.parent)}, indent=2))
elif sys.argv[1] == 'compare':
    restored = {'file': state(fixture), 'share': state(fixture.parent)}
    if json.loads(baseline.read_text()) != restored:
        raise SystemExit('FAIL: file contents, owner, group, mode, ACL, or xattrs changed on restore')
else:
    raise SystemExit('usage: lab-storage snapshot|compare')
print('PASS: shared-file storage ' + sys.argv[1], flush=True)
