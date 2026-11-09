"""Record/compare synthetic file metadata and Gitea mapping across cold restore."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

root = Path('/var/lib/smolofis-identity-machine-lab')
if os.geteuid() != 0 or not (root / 'domain-ready').exists() or not Path('/etc/smolofis-m0-fullvm').exists():
    raise SystemExit('Refusing checkpoint checks outside the owned VM')
action = sys.argv[1]
if action not in ('snapshot', 'compare'):
    raise SystemExit('usage: verify-checkpoint.py snapshot|compare')
password = json.loads((root / 'passwords.json').read_text())['alice']
with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    auth = temp / 'auth'
    auth.write_text('username = alice\ndomain = M0\npassword = ' + password + '\n')
    auth.chmod(0o600)
    payload = temp / 'payload'
    payload.write_text('M0 cold restore retained share evidence\n')
    operation = f'put {payload} m0-restore-evidence.txt' if action == 'snapshot' else f'get m0-restore-evidence.txt {temp}/download'
    result = subprocess.run(['smbclient', '//127.0.0.1/projects', '-p', '1445', '-A', str(auth), '-c', operation], capture_output=True, timeout=45)
    if result.returncode or (action == 'compare' and (temp / 'download').read_bytes() != payload.read_bytes()):
        raise SystemExit('FAIL: checkpoint SMB contents; output suppressed')

member = ['docker', 'exec', 'smolofis-m0-native-files-1', 'python3', '-c']
if action == 'snapshot':
    subprocess.run(member + ['import os; os.setxattr("/srv/shares/projects/m0-restore-evidence.txt", "user.m0", b"cold-restore")'], check=True, capture_output=True)
metadata = subprocess.check_output(member + ['''import hashlib,json,os,stat,subprocess
p="/srv/shares/projects/m0-restore-evidence.txt"
s=os.stat(p)
print(json.dumps(dict(uid=s.st_uid,gid=s.st_gid,mode=stat.S_IMODE(s.st_mode),
 sha256=hashlib.sha256(open(p,"rb").read()).hexdigest(),
 acl=subprocess.check_output(["getfacl","-cpn",p],text=True),
 xattrs={k:os.getxattr(p,k).hex() for k in os.listxattr(p)})))
'''])
token = base64.b64encode(('alice:' + password).encode()).decode()
request = urllib.request.Request('http://127.0.0.1:3000/api/v1/user', headers={'Authorization': 'Basic ' + token})
with urllib.request.urlopen(request, timeout=45) as response:
    account = json.load(response)
report = {'file': json.loads(metadata), 'gitea': {key: account[key] for key in ('id', 'login', 'is_admin')}}
baseline = root / 'checkpoint.json'
if action == 'snapshot':
    baseline.write_text(json.dumps(report, indent=2))
elif report != json.loads(baseline.read_text()):
    raise SystemExit('FAIL: restored file metadata or Gitea account mapping changed')
print('PASS: checkpoint ' + action + ': file contents, numeric ownership, mode, ACL, xattrs, Gitea account ID')
