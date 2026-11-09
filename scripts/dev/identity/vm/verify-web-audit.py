"""Unprivileged web audit acceptance against real LDAPS in the owned full VM."""
from contextlib import contextmanager
import errno
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
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
if (os.geteuid() != 0 or not (ROOT / 'domain-ready').exists()
        or not Path('/etc/smolofis-m0-fullvm').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Refusing web audit acceptance outside the owned full-kernel VM')
reports = list(ROOT.glob('local-audit-*/report.json'))
if not reports:
    raise SystemExit('Run check-local-audit first to prepare the verified Linux fixture')
source = max(reports, key=lambda path: path.stat().st_mtime)
local_report = json.loads(source.read_text())
assert local_report['binary_sha256'] == hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest()
source_runtime = Path(local_report['runtime'])
assert source_runtime.parent == Path('/run') and re.fullmatch('smolofis-local-audit-[0-9a-f]{10}', source_runtime.name)
if not (source_runtime / 'auth.json').is_file():
    raise SystemExit('Run check-local-audit after the guest reboot to recreate its private runtime fixture')
source_suffix = source_runtime.name.rsplit('-', 1)[1]
admin = 'a-' + source_suffix
password = json.loads((source.parent / 'passwords.json').read_text())['admin']
configuration = json.loads((source_runtime / 'auth.json').read_text())
group = configuration['directory']['admin_group_dn'].split(',', 1)[0].removeprefix('CN=')
origin = 'http://127.0.0.1:18094'
configuration['origin'] = origin
os.umask(0o077)
suffix = secrets.token_hex(5)
runtime = Path('/run') / ('smolofis-web-audit-' + suffix)
runtime.mkdir(mode=0o755)
runtime.chmod(0o755)
evidence = ROOT / ('web-audit-' + suffix)
evidence.mkdir(mode=0o700)
config = runtime / 'auth.json'
config.write_text(json.dumps(configuration))
config.chmod(0o644)
state = runtime / 'state.json'
local_log = runtime / 'local.jsonl'
local_log.touch(mode=0o600)
checks = []

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def required(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=60, **kwargs)
    if result.returncode:
        raise SystemExit('FAIL: web audit prerequisite; output suppressed')
    return result.stdout

def control(operation, log=local_log, allowed=True):
    args = [str(BINARY), operation]
    args += [str(config), str(state), admin] if operation == '--auth-enroll' else [str(state)]
    environment = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}
    environment['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(log)
    result = subprocess.run(args, input=(password if operation == '--auth-enroll' else '').encode(),
                            capture_output=True, timeout=60, env=environment)
    assert (result.returncode == 0) == allowed
    if not allowed:
        assert b'Console is locked' in result.stderr

control('--auth-enroll')
identity = json.loads(state.read_text())['first_admin_guid']

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class Browser:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, path, fields=None, cookie=None):
        headers = {'Origin': origin, 'Sec-Fetch-Site': 'same-origin'}
        if cookie is not None:
            headers['Cookie'] = cookie
        request = urllib.request.Request(origin + path, headers=headers,
            data=urllib.parse.urlencode(fields).encode() if fields is not None else None)
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, response.read().decode(), response.headers

    def login(self):
        status, page, _ = self.request('/auth/login')
        assert status == 200
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page).group(1)
        return self.request('/auth/login', {'username': admin, 'password': password, 'csrf': csrf})

    def session(self):
        return next('smolofis_session=' + cookie.value for cookie in self.jar if cookie.name == 'smolofis_session')

def audit_file(path, uid=65534, mode=0o600):
    path.touch(mode=mode)
    path.chmod(mode)
    os.chown(path, uid, uid)
    return path

def environment(log):
    env = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}
    env.update(SMOLOFIS_BIND='127.0.0.1:18094', SMOLOFIS_CONSOLE_ENABLED='true',
               SMOLOFIS_AUTH_CONFIG=str(config), SMOLOFIS_AUTH_STATE=str(state),
               SMOLOFIS_AUDIT_FILE=str(log), RUST_LOG='error')
    return env

def spawn(log):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', 18094))
    return subprocess.Popen([str(BINARY)], env=environment(log), user=65534, group=65534,
        extra_groups=(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=10)

@contextmanager
def server(log):
    process = spawn(log)
    try:
        for _ in range(120):
            if process.poll() is not None:
                raise SystemExit('FAIL: unprivileged panel startup; diagnostics suppressed')
            try:
                if Browser().request('/healthz')[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(.25)
        else:
            raise SystemExit('FAIL: panel readiness deadline')
        status = Path(f'/proc/{process.pid}/status').read_text()
        assert re.search(r'^Uid:\s+65534\s+65534\s+65534\s+65534$', status, re.M)
        assert re.search(r'^Gid:\s+65534\s+65534\s+65534\s+65534$', status, re.M)
        groups = re.search(r'^Groups:\s*(.*)$', status, re.M).group(1).split()
        assert '0' not in groups
        assert int(re.search(r'^CapEff:\s*(\w+)$', status, re.M).group(1), 16) == 0
        yield process
    finally:
        stop(process)

healthy = audit_file(runtime / 'healthy.jsonl')
with server(healthy):
    browser = Browser()
    assert browser.login()[0] == 303
    assert browser.request('/api/state')[0] == 200
    event = json.loads(healthy.read_text().splitlines()[-1])
    assert event['event'] == 'login_allowed' and event['actor_guid'] == identity
    assert 'actor_uid' not in event and password not in healthy.read_text() and admin not in healthy.read_text()
passed('UID/GID 65534 with no root group/capabilities writes actor-attributed events and authenticates real LDAPS')
shutil.copyfile(healthy, evidence / 'healthy-events.jsonl')

cases = [(runtime / 'missing.jsonl', 'No such file'),
         (audit_file(runtime / 'root-owned.jsonl', uid=0), 'caller-owned'),
         (audit_file(runtime / 'public.jsonl', mode=0o644), 'one link')]
link = runtime / 'symlink.jsonl'
link.symlink_to(healthy)
cases.append((link, 'caller-owned'))
hard = runtime / 'hardlink.jsonl'
os.link(healthy, hard)
cases.append((hard, 'one link'))
unsafe_parent = runtime / 'unsafe'
unsafe_parent.mkdir(mode=0o755)
unsafe_parent.chmod(0o777)
cases.append((audit_file(unsafe_parent / 'events.jsonl'), 'not be group/world writable'))
for path, reason in cases:
    process = spawn(path)
    try:
        _, stderr = process.communicate(timeout=30)
        assert process.returncode != 0 and reason.encode() in stderr
    finally:
        stop(process)
hard.unlink()
passed('Linux web startup rejects missing/root-owned/public/symlink/hard-link logs and writable parent directories')

partial = audit_file(runtime / 'partial.jsonl')
with server(partial):
    browser = Browser()
    assert browser.login()[0] == 303
    old_cookie = browser.session()
    status, account, _ = browser.request('/auth/account')
    assert status == 200
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', account).group(1)
    with partial.open('ab') as stream:
        stream.write(b'partial')
    before = partial.read_bytes()
    fresh = Browser()
    status, _, headers = fresh.login()
    assert status == 503 and not any(cookie.name == 'smolofis_session' for cookie in fresh.jar)
    assert not any('smolofis_session=' in value for value in headers.get_all('Set-Cookie', []))
    assert partial.read_bytes() == before
    status, _, headers = browser.request('/auth/logout', {'csrf': csrf})
    assert status == 503 and any('Max-Age=0' in value for value in headers.get_all('Set-Cookie', []))
    assert Browser().request('/api/state', cookie=old_cookie)[0] == 401
    assert partial.read_bytes() == before
passed('incomplete audit tail denies new sessions without appending; logout still deletes and invalidates the old token')
shutil.copyfile(partial, evidence / 'injected-partial-events')

full = runtime / 'full'
full.mkdir(mode=0o755)
required(['mount', '-t', 'tmpfs', '-o', 'size=64k,mode=0755', 'tmpfs', str(full)])
try:
    log = audit_file(full / 'events.jsonl')
    with server(log):
        revoked, lockdown = Browser(), Browser()
        assert revoked.login()[0] == 303 and lockdown.login()[0] == 303
        revoked_cookie, lockdown_cookie = revoked.session(), lockdown.session()
        # Align the test log to a page boundary so the next append needs a new
        # tmpfs page. Padding is valid JSON plus whitespace, explicitly synthetic.
        padding = json.dumps({'version': 1, 'timestamp_ms': 0, 'event': 'login_denied', 'actor_guid': None}).encode()
        length = log.stat().st_size
        target = ((length + len(padding) + 1 + 4095) // 4096) * 4096
        with log.open('ab', buffering=0) as stream:
            stream.write(padding + b' ' * (target - length - len(padding) - 1) + b'\n')
        with (full / 'filler').open('wb', buffering=0) as stream:
            while True:
                try:
                    stream.write(b'x' * 4096)
                except OSError as error:
                    if error.errno != errno.ENOSPC:
                        raise
                    break
        before = log.read_bytes()
        fresh = Browser()
        status, _, headers = fresh.login()
        assert status == 503 and not any(cookie.name == 'smolofis_session' for cookie in fresh.jar)
        assert not any('smolofis_session=' in value for value in headers.get_all('Set-Cookie', []))
        assert log.read_bytes() == before
        passed('real ENOSPC on web-owned audit log denies login without issuing a session')
        try:
            required(['samba-tool', 'group', 'removemembers', group, admin])
            assert Browser().request('/api/state', cookie=revoked_cookie)[0] == 503
            assert Browser().request('/api/state', cookie=revoked_cookie)[0] == 401
        finally:
            required(['samba-tool', 'group', 'addmembers', group, admin])
        assert Browser().request('/api/state', cookie=revoked_cookie)[0] == 401
        assert Browser().request('/api/state', cookie=lockdown_cookie)[0] == 200
        assert log.read_bytes() == before
        passed('directory group removal revokes observed session despite ENOSPC; re-adding membership cannot revive its token')
        broken_local = runtime / 'broken-local.jsonl'
        broken_local.write_bytes(b'partial')
        control('--auth-lock', log=broken_local, allowed=False)
        status, body, _ = Browser().request('/api/state', cookie=lockdown_cookie)
        assert status == 503 and 'locked locally' in body
        control('--auth-enroll')
        assert Browser().request('/api/state', cookie=lockdown_cookie)[0] == 401
        assert log.read_bytes() == before
        passed('local lockdown still invalidates live browser sessions with both audit channels failing; re-enrollment does not revive tokens')
        shutil.copyfile(log, evidence / 'disk-full-events.jsonl')
finally:
    required(['umount', str(full)])

recovered = audit_file(runtime / 'recovered.jsonl')
with server(recovered):
    browser = Browser()
    assert browser.login()[0] == 303 and browser.request('/api/state')[0] == 200
    assert json.loads(recovered.read_text().splitlines()[-1])['event'] == 'login_allowed'
passed('new provisioned log and restarted writer restore sign-in after fault archival')
shutil.copyfile(recovered, evidence / 'recovered-events.jsonl')
shutil.copyfile(local_log, evidence / 'local-events.jsonl')
(evidence / 'report.json').write_text(json.dumps({'version': 1,
    'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
    'checks': checks, 'runtime': str(runtime), 'source_local_report': str(source),
    'uid': 65534, 'effective_capabilities': 0, 'tmpfs_unmounted': True,
    'kernel': os.uname().release, 'architecture': os.uname().machine,
    'systemd_sandbox_tested': False, 'production_modified': False}, indent=2))
print(f'PASS: {len(checks)} Linux web-audit checks; private evidence retained at {evidence}', flush=True)
