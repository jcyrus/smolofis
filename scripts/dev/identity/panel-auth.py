#!/usr/bin/env python3
"""Synthetic panel authentication acceptance inside the isolated directory lab."""
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LAB = Path('/lab')
BASE = 'DC=lab,DC=smolofis,DC=test'
GROUP = f'CN=Console-Administrators,CN=Users,{BASE}'
NESTED = f'CN=Console-Operators,CN=Users,{BASE}'
ADMIN = f'CN=panel-admin,CN=Users,{BASE}'


def command(*args, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True,
                            env={**os.environ, 'LDAPTLS_CACERT': '/lab/ca.crt'}, timeout=15)
    if result.returncode:
        raise RuntimeError(f'lab command failed: {args[0]} (output suppressed)')
    return result.stdout


def prepare():
    credentials = LAB / 'panel-passwords.json'
    if not credentials.exists():
        credentials.write_text(json.dumps({name: 'Lab9!' + secrets.token_hex(18)
                                          for name in ('panel-admin', 'panel-nonadmin')}))
        credentials.chmod(0o600)
    passwords = json.loads(credentials.read_text())
    for name, password in passwords.items():
        exists = subprocess.run(['samba-tool', 'user', 'show', name], capture_output=True).returncode == 0
        if not exists:
            command('samba-tool', 'user', 'create', name, password)
    if subprocess.run(['samba-tool', 'group', 'show', 'Console-Administrators'], capture_output=True).returncode:
        command('samba-tool', 'group', 'add', 'Console-Administrators')
    if subprocess.run(['samba-tool', 'group', 'show', 'Console-Operators'], capture_output=True).returncode:
        command('samba-tool', 'group', 'add', 'Console-Operators')
    if 'panel-admin' not in command('samba-tool', 'group', 'listmembers', 'Console-Operators').splitlines():
        command('samba-tool', 'group', 'addmembers', 'Console-Operators', 'panel-admin')
    members = command('samba-tool', 'group', 'listmembers', 'Console-Administrators').splitlines()
    if 'panel-admin' not in members:
        command('samba-tool', 'group', 'addmembers', 'Console-Administrators', 'panel-admin')
    command('samba-tool', 'user', 'enable', 'panel-admin')
    root_password = json.loads((LAB / 'passwords.json').read_text())['Administrator']
    (LAB / 'panel-root-password').write_text(root_password)
    (LAB / 'panel-root-password').chmod(0o600)
    config = {'origin': 'http://127.0.0.1:18091', 'directory': {
        'url': 'ldaps://dc', 'realm': 'LAB.SMOLOFIS.TEST', 'base_dn': BASE,
        'admin_group_dn': GROUP, 'ca_file': '/lab/ca.crt',
        'reader_dn': f'CN=ldap-reader,CN=Users,{BASE}',
        'reader_password_file': '/lab/ldap-bind-password'}}
    (LAB / 'panel-auth.json').write_text(json.dumps(config))
    print('PASS: synthetic console identities and dedicated administrator group prepared', flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Browser:
    def __init__(self, origin):
        self.origin = origin
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, path, data=None, origin=True):
        headers = {'Origin': self.origin} if origin else {}
        request = urllib.request.Request(self.origin + path,
                                         data=None if data is None else urllib.parse.urlencode(data).encode(),
                                         headers=headers)
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        return response.code, response.read().decode(), response.headers

    def login(self, name, password):
        status, page, _ = self.request('/auth/login')
        assert status == 200
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page).group(1)
        return self.request('/auth/login', {'username': name, 'password': password, 'csrf': csrf})[0]


def start(config_path, state=None, user=None):
    config = json.loads(Path(config_path).read_text())
    address = urllib.parse.urlparse(config['origin']).netloc
    environment = {**os.environ,
        'SMOLOFIS_CONSOLE_ENABLED': 'true', 'SMOLOFIS_BIND': address,
        'SMOLOFIS_AUTH_CONFIG': str(config_path), 'RUST_LOG': 'error'}
    if state is not None:
        environment['SMOLOFIS_AUTH_STATE'] = str(state)
    child = subprocess.Popen(['/panel/smolofis-panel'], env=environment, user=user,
        group=user, extra_groups=[] if user is not None else None,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if child.poll() is not None:
            raise RuntimeError('panel exited during authentication startup')
        try:
            if Browser(config['origin']).request('/healthz')[0] == 200:
                return child
        except OSError:
            pass
        time.sleep(.1)
    child.terminate()
    child.wait(timeout=5)
    raise RuntimeError('panel did not become live')


def modify(dn, attribute, value, operation='replace'):
    ldif = f'dn: {dn}\nchangetype: modify\n{operation}: {attribute}\n{attribute}: {value}\n\n'
    command('ldapmodify', '-x', '-H', 'ldaps://dc', '-D', 'Administrator@LAB.SMOLOFIS.TEST',
            '-y', '/lab/panel-root-password', data=ldif)


def verify():
    passwords = json.loads((LAB / 'panel-passwords.json').read_text())
    process = start('/lab/panel-auth.json')
    checks = []
    def passed(message):
        checks.append(message)
        print('PASS: ' + message, flush=True)
    browser = Browser('http://127.0.0.1:18091')
    nested_added = False
    try:
        assert browser.request('/api/state')[0] == 401
        passed('telemetry denied before sign-in; liveness remains public')
        assert browser.login('panel-nonadmin', passwords['panel-nonadmin']) == 401
        passed('valid directory account without console administrator membership denied')
        assert browser.login('panel-admin', 'wrong-password') == 401
        passed('incorrect directory password denied')
        assert browser.login('panel-admin', passwords['panel-admin']) == 303
        assert browser.request('/console')[0] == 200
        assert browser.request('/api/state')[0] == 200
        passed('certificate-verified LDAPS administrator sign-in opens console and telemetry')
        modify(GROUP, 'member', ADMIN, 'delete')
        assert browser.request('/api/state')[0] == 401
        modify(GROUP, 'member', ADMIN, 'add')
        assert browser.request('/api/state')[0] == 401
        passed('administrator group removal permanently revokes the observed session')
        assert browser.login('panel-admin', passwords['panel-admin']) == 303
        modify(ADMIN, 'userAccountControl', '514')
        assert browser.request('/api/state')[0] == 401
        modify(ADMIN, 'userAccountControl', '512')
        assert browser.request('/api/state')[0] == 401
        passed('directory account disabling permanently revokes the observed session')
        assert browser.login('panel-admin', passwords['panel-admin']) == 303
        status, account, _ = browser.request('/auth/account')
        assert status == 200
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', account).group(1)
        assert browser.request('/auth/logout', {'csrf': 'wrong'})[0] == 403
        assert browser.request('/auth/logout', {'csrf': csrf}, origin=False)[0] == 403
        assert browser.request('/auth/logout', {'csrf': csrf})[0] == 303
        assert browser.request('/api/state')[0] == 401
        passed('sign-out requires same-origin CSRF proof and invalidates session')
        modify(GROUP, 'member', NESTED, 'add')
        nested_added = True
        modify(GROUP, 'member', ADMIN, 'delete')
        assert browser.login('panel-admin', passwords['panel-admin']) == 303
        assert browser.request('/api/state')[0] == 200
        modify(GROUP, 'member', NESTED, 'delete')
        nested_added = False
        assert browser.request('/api/state')[0] == 401
        passed('nested administrator group membership allows access and removal revokes it')
    finally:
        process.terminate()
        process.wait(timeout=5)
        # Restore only synthetic fixtures, even when assertions fail.
        modify(ADMIN, 'userAccountControl', '512')
        if nested_added:
            modify(GROUP, 'member', NESTED, 'delete')
        members = command('ldapsearch', '-LLL', '-o', 'ldif-wrap=no', '-x', '-H', 'ldaps://dc', '-D',
                          'Administrator@LAB.SMOLOFIS.TEST', '-y', '/lab/panel-root-password',
                          '-b', GROUP, '-s', 'base', 'member')
        if ADMIN not in members:
            modify(GROUP, 'member', ADMIN, 'add')
    for name, change in [('untrusted CA', {'ca_file': '/etc/ssl/certs/ca-certificates.crt'}),
                         ('certificate hostname mismatch', {'url': 'ldaps://10.83.0.10'})]:
        config = json.loads((LAB / 'panel-auth.json').read_text())
        config['directory'].update(change)
        path = Path('/tmp/panel-negative.json')
        path.write_text(json.dumps(config))
        process = start(path)
        try:
            assert Browser(config['origin']).login('panel-admin', passwords['panel-admin']) == 401
            passed(name + ' denies sign-in')
        finally:
            process.terminate()
            process.wait(timeout=5)
    print(json.dumps({'checks': checks, 'scope': 'isolated Samba container; not VM or production acceptance'}), flush=True)


def enrollment():
    # Unique machine identity for this disposable runner only; never a host mount.
    machine = Path('/etc/machine-id')
    machine.write_text(secrets.token_hex(16) + '\n')
    machine.chmod(0o644)
    runtime = Path('/run/smolofis-panel-enrollment')
    runtime.mkdir(mode=0o755)
    config = runtime / 'auth.json'
    original = (LAB / 'panel-auth.json').read_text()
    config.write_text(original)
    config.chmod(0o644)
    state = runtime / 'state.json'
    passwords = json.loads((LAB / 'panel-passwords.json').read_text())
    checks = []
    def passed(message):
        checks.append(message)
        print('PASS: ' + message, flush=True)
    def control(operation, username=None, password=None, expected=0, user=None):
        args = ['/panel/smolofis-panel', operation]
        args += [str(config), str(state), username] if username else [str(state)]
        result = subprocess.run(args, input=password, text=True, capture_output=True,
                                timeout=10, user=user)
        assert result.returncode == expected, f'{operation} returned unexpected status; output suppressed'
        return result.stdout
    def enroll():
        control('--auth-enroll', 'panel-admin', passwords['panel-admin'])
    control('--auth-enroll', 'panel-nonadmin', passwords['panel-nonadmin'], expected=1)
    assert not state.exists()
    control('--auth-enroll', 'panel-admin', 'incorrect', expected=1)
    assert not state.exists()
    passed('non-admin and incorrect-password enrollment leave no device state')
    enroll()
    first = json.loads(state.read_text())
    assert state.stat().st_mode & 0o777 == 0o644
    status = json.loads(control('--auth-status'))
    assert status['locked'] is False and status['directory_checked'] is False
    assert len(status['first_admin_guid']) == 32
    control('--auth-status', expected=1, user=65534)
    passed('root-only enrollment records the real directory GUID and device identity')
    process = start(config, state=state, user=65534)
    browser = Browser('http://127.0.0.1:18091')
    try:
        assert browser.login('panel-admin', passwords['panel-admin']) == 303
        assert browser.request('/api/state')[0] == 200
        passed('unprivileged panel reads root-owned enrollment and verifies administrator access')
        control('--auth-lock')
        assert browser.request('/api/state')[0] == 503
        assert browser.login('panel-admin', passwords['panel-admin']) == 503
        assert browser.request('/healthz')[0] == 200
        enroll()
        assert browser.request('/api/state')[0] == 401
        assert json.loads(state.read_text())['device_id'] == first['device_id']
        assert json.loads(state.read_text())['generation'] != first['generation']
        assert browser.login('panel-admin', passwords['panel-admin']) == 303
        passed('local lock blocks access; re-enrollment preserves device ID and rejects old sessions')
        control('--auth-lock')
        enroll()
        assert browser.request('/api/state')[0] == 401
        assert browser.login('panel-admin', passwords['panel-admin']) == 303
        passed('lock and re-enrollment invalidate sessions even without an intervening browser request')
        offline = json.loads(original)
        offline['directory']['url'] = 'ldaps://127.0.0.1:9'
        config.write_text(json.dumps(offline))
        assert browser.request('/api/state')[0] == 503
        assert json.loads(control('--auth-status'))['directory_checked'] is False
        control('--auth-lock')
        locked = state.read_bytes()
        control('--auth-enroll', 'panel-admin', passwords['panel-admin'], expected=1)
        assert state.read_bytes() == locked
        config.write_text(original)
        enroll()
        assert browser.request('/api/state')[0] == 401
        assert browser.login('panel-admin', passwords['panel-admin']) == 303
        passed('offline status and lockdown work; failed directory re-enrollment preserves locked state')
        good = state.read_bytes()
        state.write_text('{broken')
        assert browser.request('/api/state')[0] == 503
        assert browser.request('/healthz')[0] == 200
        state.write_bytes(good)
        state.chmod(0o666)
        assert browser.request('/api/state')[0] == 503
        control('--auth-lock', expected=1)
        state.chmod(0o644)
        saved = runtime / 'saved.json'
        state.rename(saved)
        state.symlink_to(saved)
        assert browser.request('/api/state')[0] == 503
        control('--auth-lock', expected=1)
        state.unlink()
        saved.rename(state)
        assert browser.request('/api/state')[0] == 200
        passed('malformed, writable, and symlink enrollment files fail closed')
        machine_original = machine.read_text()
        machine.write_text(secrets.token_hex(16) + '\n')
        assert browser.request('/api/state')[0] == 503
        control('--auth-status', expected=1)
        machine.write_text(machine_original)
        assert browser.request('/api/state')[0] == 200
        passed('copied enrollment is rejected on a different machine identity')
        import fcntl
        with (runtime / '.panel-enrollment.lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            control('--auth-lock', expected=1)
            assert state.read_bytes() == good
        passed('concurrent local commands cannot overwrite an active enrollment operation')
        control('--auth-lock')
        process.terminate()
        process.wait(timeout=5)
        process = start(config, state=state, user=65534)
        assert browser.request('/api/state')[0] == 503
        assert browser.request('/healthz')[0] == 200
        passed('local lockdown survives a panel process restart')
    finally:
        process.terminate()
        process.wait(timeout=5)
    print(json.dumps({'checks': checks, 'scope': 'root local commands and unprivileged panel in isolated Samba container'}), flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['prepare']:
        prepare()
    elif sys.argv[1:] == ['verify']:
        verify()
    elif sys.argv[1:] == ['enrollment']:
        enrollment()
    elif sys.argv[1:] == ['private-https']:
        import runpy
        runpy.run_path('/usr/local/bin/lab-private-https', run_name='__main__')
    else:
        raise SystemExit('usage: panel-auth.py prepare|verify|enrollment|private-https')
