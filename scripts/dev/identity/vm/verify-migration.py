"""Migrate one synthetic local Gitea account through supported APIs in the VM."""
import base64
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tempfile
import urllib.error
import urllib.request

root = Path('/var/lib/smolofis-identity-machine-lab')
if os.geteuid() != 0 or not (root / 'domain-ready').exists() or not Path('/etc/smolofis-m0-fullvm').exists():
    raise SystemExit('Refusing migration outside the owned VM')
passwords = json.loads((root / 'passwords.json').read_text())
compose = ['docker-compose', '-f', '/opt/smolofis-identity-lab/vm/compose.yml']
cli = [*compose, 'exec', '-T', '--user', 'git', 'gitea', 'gitea', 'admin', 'user']
def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=90, **kwargs)
    if result.returncode:
        raise SystemExit('FAIL: migration command; output suppressed')
    return result.stdout
if b'lab-admin' not in command([*cli, 'list']):
    command([*cli, 'create', '--username', 'lab-admin', '--password', passwords['lab-admin'],
             '--email', 'lab-admin@m0.smolofis.test', '--admin', '--must-change-password=false'])
admin = ('lab-admin', passwords['lab-admin'])
suffix = secrets.token_hex(5)
username, directory_name = 'legacy-' + suffix, 'm-' + suffix
local = (username, 'Lab9!' + secrets.token_hex(18))
directory = (username, 'Lab9!' + secrets.token_hex(18))
command(['samba-tool', 'user', 'create', directory_name, directory[1],
         '--mail-address=' + directory_name + '@m0.smolofis.test'])

def api(path, method='GET', data=None, credentials=admin, token=None, expected=200):
    headers = {'Content-Type': 'application/json'}
    headers['Authorization'] = ('token ' + token if token else 'Basic ' + base64.b64encode(':'.join(credentials).encode()).decode())
    request = urllib.request.Request('http://127.0.0.1:3000/api/v1' + path,
        data=json.dumps(data).encode() if data is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    if status != expected:
        raise SystemExit(f'FAIL: migration API HTTP {status}, expected {expected}')
    return json.loads(body) if body else None

def require(condition, label):
    if not condition:
        raise SystemExit('FAIL: ' + label)
    print('PASS: ' + label, flush=True)

api('/admin/users', 'POST', {'username': username, 'password': local[1],
    'email': username + '@m0.smolofis.test', 'must_change_password': False}, expected=201)
identity = api('/user', credentials=local)
repo = api('/user/repos', 'POST', {'name': 'continuity', 'private': True, 'auto_init': True,
           'default_branch': 'main'}, credentials=local, expected=201)
repo_path = f'/repos/{username}/continuity'
commits = [item['sha'] for item in api(repo_path + '/commits', credentials=local)]
require(bool(commits), 'local account owns a private repository with commit history')
with tempfile.TemporaryDirectory() as directory_path:
    temp = Path(directory_path)
    key_path = temp / 'key'
    command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key_path)])
    key = api('/user/keys', 'POST', {'title': 'migration-proof', 'key': key_path.with_suffix('.pub').read_text()}, credentials=local, expected=201)
    token = api('/users/' + username + '/tokens', 'POST', {'name': 'migration-proof',
                'scopes': ['read:user', 'read:repository']}, credentials=local, expected=201)
    host = command([*compose, 'exec', '-T', '--user', 'root', 'gitea', 'cat', '/data/ssh/ssh_host_ed25519_key.pub']).decode().split()
    known = temp / 'known_hosts'
    known.write_text(f'[127.0.0.1]:2223 {host[0]} {host[1]}\n')
    ssh = 'ssh -i ' + shlex.quote(str(key_path)) + ' -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=' + shlex.quote(str(known))
    def ssh_refs():
        return command(['git', 'ls-remote', f'ssh://git@127.0.0.1:2223/{username}/continuity.git'], env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_SSH_COMMAND': ssh})
    before_refs = ssh_refs()
    require(bool(before_refs), 'SSH key accesses repository before migration')
    source = next(user['source_id'] for user in api('/admin/users') if user['login'] == 'alice')
    require(source > 0, 'source ID resolved from authenticated directory account')
    api('/admin/users/' + username, 'PATCH', {'source_id': source, 'login_name': directory_name,
        'email': identity['email'], 'must_change_password': False})
    migrated = api('/user', credentials=directory)
    require(migrated['id'] == identity['id'] and migrated['login'] == username, 'migration retains Gitea account ID and username')
    after_repo = api(repo_path, credentials=directory)
    after_commits = [item['sha'] for item in api(repo_path + '/commits', credentials=directory)]
    require(after_repo['id'] == repo['id'] and after_repo['owner']['id'] == identity['id'] and after_commits == commits,
            'migration retains repository ownership and commit history')
    require(ssh_refs() == before_refs, 'existing SSH key still accesses unchanged repository refs')
    require(api('/user', token=token['sha1'])['id'] == identity['id'], 'existing API token still maps to the same account')
    api('/user', credentials=local, expected=401)
    print('PASS: old local password rejected after directory association', flush=True)
    # Remove synthetic bearer credentials after proving continuity.
    api(f'/users/{username}/tokens/{token["id"]}', 'DELETE', expected=204)
    api(f'/admin/users/{username}/keys/{key["id"]}', 'DELETE', expected=204)
report = {'username': username, 'directory_username': directory_name,
          'account_id': identity['id'], 'repo_id': repo['id'], 'commits': commits,
          'source_id': source, 'ssh_continuity_passed': True, 'token_continuity_passed': True,
          'synthetic_key_and_token_removed': True}
(root / 'migration-report.json').write_text(json.dumps(report, indent=2))
