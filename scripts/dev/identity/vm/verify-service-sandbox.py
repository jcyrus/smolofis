"""Run actual candidate units in the owned VM; never join/alter a tailnet."""
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
BINARY = Path('/opt/smolofis-panel-audit-lab/smolofis-panel')
SOURCES = Path('/opt/smolofis-identity-lab/vm')
BACKEND = 'smolofis-console-preview.service'
WORKER = 'smolofis-admin-worker.service'
AUDIT = Path('/var/lib/smolofis/panel-audit/auth.jsonl')
if (os.geteuid() != 0 or not (ROOT / 'domain-ready').exists()
        or not Path('/etc/smolofis-m0-fullvm').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Refusing service acceptance outside the owned full-kernel VM')
for unit in (BACKEND, WORKER):
    for base in ('/etc/systemd/system', '/run/systemd/system', '/usr/lib/systemd/system'):
        if (Path(base) / unit).exists() or (Path(base) / (unit + '.d')).exists():
            raise SystemExit('Refusing to replace an existing candidate unit or drop-in')
destinations = [Path('/etc/smolofis') / name for name in ('panel-auth.json', 'panel-enrollment.json', 'admin-worker.env')]
if any(path.exists() or path.is_symlink() for path in destinations) or AUDIT.exists() or Path('/run/smolofis-admin').exists():
    raise SystemExit('Refusing to replace pre-existing service configuration, audit history, or worker runtime')
reports = list(ROOT.glob('local-audit-*/report.json'))
if not reports:
    raise SystemExit('Run check-local-audit first')
source = max(reports, key=lambda path: path.stat().st_mtime)
local = json.loads(source.read_text())
assert local['binary_sha256'] == hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest()
fixture = Path(local['runtime'])
assert fixture.parent == Path('/run') and re.fullmatch('smolofis-local-audit-[0-9a-f]{10}', fixture.name)
if not (fixture / 'auth.json').is_file():
    raise SystemExit('Recreate the local-audit fixture after reboot')
admin = 'a-' + fixture.name.rsplit('-', 1)[1]
password = json.loads((source.parent / 'passwords.json').read_text())['admin']
configuration = json.loads((fixture / 'auth.json').read_text())
origin = 'https://sandbox-check.example.ts.net'
configuration['origin'] = origin
os.umask(0o077)
suffix = secrets.token_hex(5)
runtime = Path('/run') / ('smolofis-service-audit-' + suffix)
runtime.mkdir(mode=0o755)
runtime.chmod(0o755)
evidence = ROOT / ('service-audit-' + suffix)
evidence.mkdir(mode=0o700)
units = Path('/run/systemd/system')
home = Path('/home') / ('smolofis-sandbox-' + suffix)
created_directories, created_units = [], []
created_user = False
created_group = False
checks = []

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, timeout=90, **kwargs)

def required(args, **kwargs):
    result = run(args, **kwargs)
    if result.returncode:
        (evidence / 'last-error.log').write_bytes(result.stdout + result.stderr)
        raise SystemExit('FAIL: service acceptance command; private diagnostics retained')
    return result.stdout

def directory(path):
    if not path.exists():
        path.mkdir(mode=0o755)
        path.chmod(0o755)
        created_directories.append(path)

def stop(unit):
    required(['systemctl', 'stop', unit])

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

opener = urllib.request.build_opener(NoRedirect)
def http(path, fields=None, cookie=None):
    headers = {'Host': 'sandbox-check.example.ts.net', 'Origin': origin, 'Sec-Fetch-Site': 'same-origin'}
    if cookie:
        headers['Cookie'] = cookie
    request = urllib.request.Request('http://127.0.0.1:8091' + path, headers=headers,
        data=urllib.parse.urlencode(fields).encode() if fields is not None else None)
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.code, response.read().decode(), response.headers

def property(unit, name):
    return required(['systemctl', 'show', unit, '-p', name, '--value']).decode().strip()

def journal(unit):
    return required(['journalctl', '-u', unit, '--no-pager', '-n', '120'])

try:
    try:
        account = pwd.getpwnam('smolofis-panel')
    except KeyError:
        try:
            grp.getgrnam('smolofis-panel')
            args = ['--gid', 'smolofis-panel']
        except KeyError:
            args = ['--user-group']
            created_group = True
        required(['useradd', '--system', '--no-create-home', '--home-dir', '/nonexistent',
                  '--shell', '/usr/sbin/nologin', *args, 'smolofis-panel'])
        created_user = True
        account = pwd.getpwnam('smolofis-panel')
    uid, gid = account.pw_uid, account.pw_gid
    assert uid != 0 and gid != 0
    for path in (Path('/etc/smolofis'), Path('/var/lib/smolofis'), AUDIT.parent):
        directory(path)
    destinations[2].write_text(f'PANEL_UID={uid}\n')
    reader = runtime / 'reader-password'
    shutil.copyfile(configuration['directory']['reader_password_file'], reader)
    reader.chmod(0o640)
    os.chown(reader, 0, gid)
    configuration['directory']['reader_password_file'] = str(reader)
    config = runtime / 'panel-auth.json'
    config.write_text(json.dumps(configuration))
    config.chmod(0o644)
    state = runtime / 'panel-enrollment.json'
    local_log = runtime / 'local.jsonl'
    local_log.touch(mode=0o600)
    environment = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}
    environment['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(local_log)
    required([str(BINARY), '--auth-enroll', str(config), str(state), admin],
             input=password.encode(), env=environment)
    AUDIT.touch(mode=0o600)
    os.chown(AUDIT, uid, gid)
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    sentinel = home / 'sentinel'
    sentinel.write_text('synthetic sandbox visibility check\n')
    sentinel.chmod(0o644)
    required(['python3', '-c', 'from pathlib import Path; import sys; assert Path(sys.argv[1]).read_text()', str(sentinel)],
             user=uid, group=gid, extra_groups=())
    backend_outside, worker_outside = runtime / 'outside-panel', runtime / 'outside-root'
    for path, owner, group in ((backend_outside, uid, gid), (worker_outside, 0, gid)):
        path.write_text('no service writes here\n')
        path.chmod(0o600)
        os.chown(path, owner, group)
    probe = runtime / 'sandbox-probe.py'
    shutil.copyfile(SOURCES / 'sandbox-probe.py', probe)
    probe.chmod(0o644)
    for unit in (BACKEND, WORKER):
        target = units / unit
        shutil.copyfile(SOURCES / unit, target)
        target.chmod(0o644)
        created_units.append(target)
        dropins = units / (unit + '.d')
        dropins.mkdir(mode=0o755)
        executable = f'{BINARY} --private-console' if unit == BACKEND else f'{BINARY} --admin-worker /run/smolofis-admin/control.sock ${{PANEL_UID}}'
        text = f'[Service]\nExecStart=\nExecStart={executable}\nRestart=no\n'
        if unit == BACKEND:
            text += (f'BindReadOnlyPaths={runtime}:/etc/smolofis\n'
                     'Environment=SMOLOFIS_BIND=0.0.0.0:28091 SMOLOFIS_CONSOLE_ENABLED=false SMOLOFIS_AUTH_CONFIG=/nonexistent SMOLOFIS_AUTH_STATE=/nonexistent\n'
                     f'Environment=SMOLOFIS_AUDIT_FILE={AUDIT}\n')
        else:
            text += f'ExecStartPost=/usr/bin/python3 {probe} worker {worker_outside} {sentinel} /run/smolofis-admin\n'
        (dropins / '10-lab.conf').write_text(text)
    required(['systemctl', 'daemon-reload'])
    required(['systemd-analyze', 'verify', str(units / BACKEND), str(units / WORKER)])
    # The file is caller-owned and writable by DAC; only the service mount
    # namespace denies it without an explicit ReadWritePaths exception.
    required(['python3', '-c', 'import os,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_APPEND); os.close(fd)', str(AUDIT)],
             user=uid, group=gid, extra_groups=())
    run(['systemctl', 'start', BACKEND])
    for _ in range(40):
        if property(BACKEND, 'ActiveState') == 'failed':
            break
        time.sleep(.25)
    assert property(BACKEND, 'ActiveState') == 'failed'
    denied = journal(BACKEND)
    assert b'Read-only file system' in denied
    (evidence / 'readonly-startup.log').write_bytes(denied)
    stop(BACKEND)
    required(['systemctl', 'reset-failed', BACKEND])
    passed('actual backend unit fails closed when audit path has no write allowlist')
    backend_dropins = units / (BACKEND + '.d')
    shutil.copyfile(SOURCES / 'audit.conf', backend_dropins / '50-audit.conf')
    (backend_dropins / '60-probe.conf').write_text(f'[Service]\nExecStartPost=/usr/bin/python3 {probe} backend {backend_outside} {sentinel} {AUDIT}\n')
    required(['systemctl', 'daemon-reload'])
    required(['systemctl', 'start', BACKEND])
    for _ in range(80):
        try:
            if http('/healthz')[0] == 200:
                break
        except OSError:
            pass
        time.sleep(.25)
    else:
        raise SystemExit('FAIL: private backend readiness')
    pid = int(property(BACKEND, 'MainPID'))
    status = Path(f'/proc/{pid}/status').read_text()
    assert re.search(rf'^Uid:\s+{uid}\s+{uid}\s+{uid}\s+{uid}$', status, re.M)
    assert int(re.search(r'^CapEff:\s*(\w+)$', status, re.M).group(1), 16) == 0
    assert re.search(r'^NoNewPrivs:\s+1$', status, re.M)
    assert re.search(r'^Seccomp:\s+2$', status, re.M)
    assert b'"sandbox_probe": "backend"' in journal(BACKEND)
    for key, expected in (('ProtectSystem', 'strict'), ('ProtectHome', 'yes'), ('PrivateTmp', 'yes'), ('NoNewPrivileges', 'yes')):
        assert property(BACKEND, key) == expected
    assert '/run' in property(BACKEND, 'ReadOnlyPaths').split()
    listeners = required(['ss', '-H', '-ltnp']).decode()
    own = [line for line in listeners.splitlines() if f'pid={pid},' in line]
    assert len(own) == 1 and '127.0.0.1:8091' in own[0]
    assert http('/api/state')[0] == 401
    code, page, headers = http('/auth/login')
    assert code == 200
    cookie = next(value for value in headers.get_all('Set-Cookie', []) if value.startswith('smolofis_login='))
    assert all(flag in cookie for flag in ('Secure', 'HttpOnly', 'SameSite=Strict'))
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page).group(1)
    code, _, headers = http('/auth/login', {'username': admin, 'password': password, 'csrf': csrf}, cookie.split(';')[0])
    assert code == 303
    session = next(value for value in headers.get_all('Set-Cookie', []) if value.startswith('smolofis_session='))
    assert 'Secure' in session and http('/api/state', cookie=session.split(';')[0])[0] == 200
    events = [json.loads(line) for line in AUDIT.read_text().splitlines()]
    assert events[-1]['event'] == 'login_allowed' and 'actor_uid' not in events[-1]
    passed('actual hardened private backend authenticates LDAPS and audits through only the single-file write exception')
    passed('sandbox probe confirms read-only outside file, hidden home, zero capabilities and no-new-privileges')
    passed('private mode ignores widening/disable environment overrides and serves authenticated resources only on fixed loopback')
    required([str(BINARY), '--auth-lock', str(state)], env=environment)
    code, body, _ = http('/api/state', cookie=session.split(';')[0])
    assert code == 503 and 'locked locally' in body
    required([str(BINARY), '--auth-enroll', str(config), str(state), admin], input=password.encode(), env=environment)
    assert http('/api/state', cookie=session.split(';')[0])[0] == 401
    passed('directory-bound enrollment observes atomic lockdown/re-enrollment and permanently rejects the old service session')

    required(['systemctl', 'start', WORKER])
    sock = Path('/run/smolofis-admin/control.sock')
    assert sock.is_socket() and sock.stat().st_uid == 0 and sock.stat().st_gid == gid
    assert sock.stat().st_mode & 0o777 == 0o660
    assert b'"sandbox_probe": "worker"' in journal(WORKER)
    client = '''import socket,sys
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.settimeout(8)
try:
 s.connect(sys.argv[1]); s.sendall(sys.stdin.buffer.read()); data=s.recv(2048)
except (ConnectionResetError,BrokenPipeError,PermissionError): data=b""
finally: s.close()
sys.stdout.buffer.write(data)
'''
    def request(payload, caller=uid):
        return required(['python3', '-c', client, str(sock)], input=payload,
                        user=caller, group=gid, extra_groups=())
    valid = b'{"version":1,"operation":"protocol_status"}\n'
    reply = json.loads(request(valid))
    assert reply == {'version': 1, 'status': 'ok', 'mutations_enabled': False}
    assert request(valid, caller=0) == b''
    assert request(valid, caller=65534) == b''
    passed('actual worker unit permits the configured UID and rejects root/other UID peers despite socket access')
    for payload, expected in ((b'{"version":2,"operation":"protocol_status"}\n', 'unsupported_version'),
                              (b'{"version":1,"operation":"restart","unit":"sshd"}\n', 'invalid_request'),
                              (b'{"version":1,"operation":"protocol_status","command":"id"}\n', 'invalid_request')):
        assert json.loads(request(payload))['status'] == expected
    assert request(b'x' * 2048 + b'\n') == b''
    assert request(b'{') == b''
    assert json.loads(request(valid))['mutations_enabled'] is False
    passed('worker rejects unsupported/unknown/arbitrary/oversized/incomplete requests; mutations remain disabled')
    passed('worker sandbox allows only Unix sockets and its runtime writes; ordinary IPv4 socket creation is denied')
    for unit in (BACKEND, WORKER):
        (evidence / (unit + '.log')).write_bytes(journal(unit))
    stop(WORKER)
    assert not Path('/run/smolofis-admin').exists()
    stop(BACKEND)
    shutil.copyfile(AUDIT, evidence / 'auth-events.jsonl')
    passed('stopping candidate units closes backend and removes worker runtime/socket')
    report = {'checks': checks, 'binary_sha256': local['binary_sha256'], 'uid': uid, 'gid': gid,
              'systemd_version': required(['systemctl', '--version']).decode().splitlines()[0],
              'actual_candidate_units': True, 'https_transport_tested': False,
              'tailnet_modified': False, 'production_modified': False,
              'lab_overrides': ['binary path', 'Restart=no', 'readonly config/state fixture bindings',
                                'environment override negative tests', 'ExecStartPost sandbox probes']}
    (evidence / 'report.json').write_text(json.dumps(report, indent=2))
    print(f'PASS: {len(checks)} actual-unit sandbox checks; private evidence retained at {evidence}', flush=True)
finally:
    for unit in (WORKER, BACKEND):
        if (units / unit).exists():
            result = run(['systemctl', 'stop', unit])
            if result.returncode:
                raise SystemExit('Service stop failed; retain unit/configuration and inspect the owned lab')
    for unit in (BACKEND, WORKER):
        if (units / unit).exists():
            (evidence / (unit + '.cleanup.log')).write_bytes(journal(unit))
        dropins = units / (unit + '.d')
        if dropins.exists():
            for path in dropins.iterdir():
                path.unlink()
            dropins.rmdir()
    for path in created_units:
        path.unlink()
    for path in destinations:
        if path.exists():
            path.unlink()
    (runtime / 'reader-password').unlink(missing_ok=True)
    if AUDIT.exists():
        shutil.copyfile(AUDIT, evidence / 'archived-auth-events.jsonl')
        AUDIT.unlink()
    if home.exists():
        (home / 'sentinel').unlink(missing_ok=True)
        home.rmdir()
    for path in reversed(created_directories):
        try:
            path.rmdir()
        except OSError:
            pass
    required(['systemctl', 'daemon-reload'])
    run(['systemctl', 'reset-failed', BACKEND, WORKER])
    if created_user:
        required(['userdel', 'smolofis-panel'])
    if created_group:
        try:
            grp.getgrnam('smolofis-panel')
        except KeyError:
            pass
        else:
            required(['groupdel', 'smolofis-panel'])
