"""Real LDAPS, unprivileged HTTP backend and root Unix worker in an owned VM namespace."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
SOURCE = Path('/opt/smolofis-management-api-lab')
BINARY = SOURCE / 'smolofis-panel'
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel lab VM required')
if not sys.argv[1:]:
    assert not Path('/run/smolofis-admin').exists(), 'Existing worker runtime must be inspected first'
    created = []
    try:
        for path in (Path('/var/lib/smolofis'), Path('/etc/smolofis'), Path('/run/smolofis-admin')):
            if not path.exists():
                path.mkdir(mode=0o755); created.append(path)
        subprocess.run(['unshare', '--mount', '--fork', sys.executable, __file__, '--inside'], check=True, timeout=240)
    finally:
        for path in reversed(created):
            path.rmdir()
    raise SystemExit(0)
assert sys.argv[1:] == ['--inside']
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
for target in ('/var/lib/smolofis', '/etc/smolofis', '/run/smolofis-admin'):
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=16m,mode=0755,nodev,nosuid', 'tmpfs', target], check=True)
os.umask(0o077)
evidence = ROOT / ('management-api-' + secrets.token_hex(5)); evidence.mkdir(mode=0o700)
checks, processes = [], []
origin = 'https://smolofis-api-lab.tail72ba9d.ts.net'
base_url = 'http://127.0.0.1:8091'
uid = 65534
for directory in (Path('/etc/smolofis'), Path('/run/smolofis-admin')):
    os.chown(directory, 0, uid); directory.chmod(0o750)
identity = Path('/etc/smolofis/identity'); identity.mkdir(mode=0o700)
shutil.copyfile('/etc/machine-id', identity / 'machine-id')
audit_dir = Path('/var/lib/smolofis/api-audit'); audit_dir.mkdir(mode=0o750); os.chown(audit_dir, 0, uid); audit_dir.chmod(0o750)
audit = audit_dir / 'events.jsonl'; audit.touch(mode=0o600); os.chown(audit, uid, uid)
config = Path('/etc/smolofis/panel-auth.json')
state = Path('/etc/smolofis/panel-enrollment.json')
journal = Path('/var/lib/smolofis/operations')
runtime = Path('/run/smolofis-admin/control.sock')
admin, outsider = 'ma-' + secrets.token_hex(5), 'mn-' + secrets.token_hex(5)
group = 'M1-API-' + secrets.token_hex(5)
password = 'Lab9!' + secrets.token_hex(18)
created_users = []; created_group = False
clean_env = {k: v for k, v in os.environ.items() if not k.startswith('SMOLOFIS_')}
backend = worker = None

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def required(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=60, **kwargs)
    assert result.returncode == 0, 'Lab prerequisite failed; credential-bearing diagnostics suppressed'
    return result

def passed(label):
    checks.append(label); print('PASS: ' + label, flush=True)

def request(path, cookie='', fields=None, data=None, extra=None):
    headers = {'Host': urllib.parse.urlparse(origin).netloc, 'Origin': origin}
    if cookie: headers['Cookie'] = cookie
    if fields is not None:
        data = urllib.parse.urlencode(fields).encode(); headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif data is not None:
        data = json.dumps(data).encode(); headers['Content-Type'] = 'application/json'
    if extra: headers.update(extra)
    try:
        response = opener.open(urllib.request.Request(base_url + path, data=data, headers=headers), timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.code, response.read().decode(), response.headers

def login(name):
    code, html, headers = request('/auth/login')
    assert code == 200
    cookie = next(c.split(';', 1)[0] for c in headers.get_all('Set-Cookie') if c.startswith('smolofis_login='))
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', html).group(1)
    code, html, headers = request('/auth/login', cookie, fields={'username': name, 'password': password, 'csrf': csrf})
    if code != 303: return code, ''
    return code, next(c.split(';', 1)[0] for c in headers.get_all('Set-Cookie') if c.startswith('smolofis_session='))

def stop(process):
    if process and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)

def start_backend(management):
    environment = dict(clean_env, SMOLOFIS_AUDIT_FILE=str(audit))
    if management: environment['SMOLOFIS_MANAGEMENT_ENABLED'] = 'true'
    output = (evidence / ('backend-' + str(management) + '.log')).open('wb')
    process = subprocess.Popen([str(BINARY), '--private-console'], env=environment, user=uid, group=uid,
                               extra_groups=(), stdout=output, stderr=subprocess.STDOUT)
    output.close(); processes.append(process)
    for _ in range(50):
        assert process.poll() is None, 'Backend failed; inspect private fixture log'
        try:
            if request('/healthz')[0] == 200: return process
        except OSError: pass
        time.sleep(0.1)
    raise AssertionError('Backend did not become live')

try:
    for name in (admin, outsider):
        required(['samba-tool', 'user', 'create', name, password]); created_users.append(name)
    required(['samba-tool', 'group', 'add', group]); created_group = True
    required(['samba-tool', 'group', 'addmembers', group, admin])
    reader = Path('/etc/smolofis/reader-password')
    reader.write_text(json.loads((ROOT / 'passwords.json').read_text())['ldap-reader'])
    reader.chmod(0o640); os.chown(reader, 0, uid)
    ca = Path('/etc/smolofis/ca.pem'); shutil.copyfile('/var/lib/samba/private/tls/ca.pem', ca); ca.chmod(0o644)
    config.write_text(json.dumps({'origin': origin, 'directory': {
        'url': 'ldaps://127.0.0.1', 'realm': 'M0.SMOLOFIS.TEST', 'base_dn': 'DC=m0,DC=smolofis,DC=test',
        'admin_group_dn': f'CN={group},CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_dn': 'CN=ldap-reader,CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_password_file': str(reader), 'ca_file': str(ca)}}))
    config.chmod(0o644)
    required([str(BINARY), '--auth-enroll', str(config), str(state), admin], input=password.encode(), env=clean_env)
    expected_guid = json.loads(state.read_text())['first_admin_guid']
    required([str(BINARY), '--operation-init', str(journal)], env=clean_env)
    blocked = subprocess.run([str(BINARY)], env=dict(clean_env, SMOLOFIS_MANAGEMENT_ENABLED='true', SMOLOFIS_CONSOLE_ENABLED='true', SMOLOFIS_BIND='127.0.0.1:18099'),
                             capture_output=True, timeout=10, user=uid, group=uid, extra_groups=())
    assert blocked.returncode != 0 and b'management requires configured authentication and device enrollment' in blocked.stderr
    backend = start_backend(False)
    assert login(outsider)[0] == 401
    code, cookie = login(admin); assert code == 303
    assert request('/api/operations', cookie)[0] == 404
    stop(backend)
    passed('real directory enrollment/admin verification works and management routes remain absent by default')

    backend = start_backend(True)
    code, cookie = login(admin); assert code == 303
    assert request('/api/operations')[0] == 401
    assert request('/api/operations', extra={'Tailscale-User-Login': admin, 'X-Forwarded-User': admin})[0] == 401
    assert request('/api/operations', cookie)[0] == 503
    passed('anonymous/forged-header access is denied and missing worker yields bounded service unavailability')

    output = (evidence / 'worker.log').open('wb')
    worker = subprocess.Popen([str(BINARY), '--admin-worker', str(runtime), str(uid), str(journal)],
                              env=clean_env, user=0, group=uid, extra_groups=(), stdout=output, stderr=subprocess.STDOUT)
    output.close(); processes.append(worker)
    for _ in range(50):
        assert worker.poll() is None, 'Worker failed; inspect private fixture log'
        if runtime.exists(): break
        time.sleep(0.1)
    else: raise AssertionError('Worker socket missing')
    code, body, _ = request('/api/operations', cookie)
    assert code == 200 and json.loads(body)['operations'] == []
    _, html, _ = request('/auth/account', cookie)
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', html).group(1)
    endpoint = '/api/operations/identity-check'
    for data, headers, expected in [
        ({'csrf': csrf}, {'Origin': 'https://other.invalid'}, 403),
        ({'csrf': 'wrong'}, {}, 403),
        ({'csrf': csrf, 'actor_guid': 'ab' * 16}, {}, 422),
        ({'csrf': csrf, 'command': 'id'}, {}, 422),
    ]:
        assert request(endpoint, cookie, data=data, extra=headers)[0] == expected
    assert json.loads(request('/api/operations', cookie)[1])['operations'] == []
    passed('origin/CSRF checks and strict body decoding prevent forged actors or arbitrary commands before journal writes')

    code, body, headers = request(endpoint, cookie, data={'csrf': csrf}, extra={'X-Forwarded-User': outsider})
    assert code == 201 and 'no-store' in headers.get('Cache-Control', '')
    operation = json.loads(body)
    assert operation['actor_guid'] == expected_guid and operation['requester_uid'] == uid and operation['actor_uid'] == 0
    assert operation['outcome'] == 'identity_verified'
    assert [t['phase'] for t in operation['transitions']] == ['queued', 'running', 'succeeded']
    assert json.loads(request('/api/operations/' + operation['id'], cookie)[1]) == operation
    assert request('/api/operations/bad-id', cookie)[0] == 400
    assert request('/api/operations/' + 'fe' * 32, cookie)[0] == 404
    assert subprocess.run(['cat', str(journal / 'journal.json')], user=uid, group=uid, extra_groups=(), capture_output=True).returncode != 0
    passed('unprivileged authenticated API executes through root worker and preserves real GUID/peer UID with typed durable results')

    snapshot = (journal / 'journal.json').read_bytes()
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(3); peer.connect(str(runtime))
        try:
            peer.sendall(b'{"version":1,"operation":"protocol_status"}\n')
            assert peer.recv(4096) == b''
        except (ConnectionResetError, BrokenPipeError): pass
    assert (journal / 'journal.json').read_bytes() == snapshot
    passed('worker peer-UID validation rejects a root caller even when filesystem permissions permit connecting')

    probe = """import socket
import sys
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(5)
    connection.connect('/run/smolofis-admin/control.sock')
    connection.sendall(sys.argv[1].encode() + bytes([10]))
    print(connection.recv(4096).decode())
"""
    payload = json.dumps({'version': 1, 'operation': 'identity_check',
                          'actor_guid': expected_guid, 'path': '/etc/shadow'})
    reply = required([sys.executable, '-c', probe, payload], user=uid, group=uid, extra_groups=())
    assert json.loads(reply.stdout)['status'] == 'invalid_request'
    assert (journal / 'journal.json').read_bytes() == snapshot
    passed('allowed Unix caller still cannot supply arbitrary filesystem paths or unsupported worker fields')

    required(['samba-tool', 'group', 'removemembers', group, admin])
    assert request('/api/operations', cookie)[0] == 401
    assert request(endpoint, cookie, data={'csrf': csrf})[0] == 401
    assert (journal / 'journal.json').read_bytes() == snapshot
    passed('directory administrator removal revokes management access without creating further operations')

    stop(worker)
    required(['samba-tool', 'group', 'addmembers', group, admin])
    assert request('/api/operations', cookie)[0] == 401
    code, fresh = login(admin); assert code == 303
    assert request('/api/operations', fresh)[0] == 503
    assert (journal / 'journal.json').read_bytes() == snapshot
    assert password not in audit.read_text() and password.encode() not in snapshot
    passed('revoked sessions stay invalid after membership restoration and worker outage preserves durable history')

    older = Path('/opt/smolofis-operation-journal-lab/smolofis-panel')
    older_report = ROOT / 'operation-journal-56088655dc/report.json'
    assert hashlib.file_digest(older.open('rb'), 'sha256').hexdigest() == json.loads(older_report.read_text())['binary_sha256']
    for action in ('list', 'run', 'recover'):
        legacy = subprocess.run([str(older), '--operation-' + action, str(journal)], capture_output=True, timeout=15)
        assert legacy.returncode != 0 and b'invalid operation journal' in legacy.stderr
        assert (journal / 'journal.json').read_bytes() == snapshot
    passed('verified older journal reader/writer refuses attributed records while preserving their bytes')
finally:
    for process in reversed(processes): stop(process)
    failures = []
    for name in created_users:
        if subprocess.run(['samba-tool', 'user', 'delete', name], capture_output=True, timeout=60).returncode: failures.append('user')
    if created_group:
        if subprocess.run(['samba-tool', 'group', 'delete', group], capture_output=True, timeout=60).returncode: failures.append('group')
    assert not failures, 'Synthetic directory cleanup incomplete'

report = {'checks': checks, 'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
          'verified_ldaps': True, 'backend_uid': uid, 'worker_uid': 0,
          'http_transport': 'private loopback proxy-style fixture with HTTPS origin and manually supplied Secure cookies',
          'public_tls_tested': False, 'private_mount_namespace': True,
          'synthetic_accounts_removed': True, 'production_modified': False}
(evidence / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} management API checks; evidence retained at {evidence}', flush=True)
