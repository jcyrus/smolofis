"""Exercise supported Gitea APIs with synthetic accounts; never print secrets."""
import base64
import http.cookiejar
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


BASE = 'http://git:3000'
run_suffix = secrets.token_hex(6)
USER = 'legacy-' + run_suffix
DIRECTORY_USER = 'm-' + run_suffix
if '--check-restored' in sys.argv:
    previous = json.loads(Path('/evidence/migration-report.json').read_text())
    USER, DIRECTORY_USER = previous['username'], previous['directory_username']
elif '--check-session-revocation' in sys.argv:
    previous = json.loads(Path('/evidence/session-fixture.json').read_text())['report']
    USER, DIRECTORY_USER = previous['username'], previous['directory_username']
REPO = 'continuity'
passwords = json.loads(Path('/lab/passwords.json').read_text())
admin = ('lab-admin', passwords['lab-admin'])
local = (USER, passwords['legacy-local'])
directory = (USER, passwords['legacy-dir'])


def api(path, method='GET', data=None, credentials=admin, token=None, expected=(200,)):
    headers = {}
    if token:
        headers['Authorization'] = 'token ' + token
    elif credentials:
        encoded = base64.b64encode(':'.join(credentials).encode()).decode()
        headers['Authorization'] = 'Basic ' + encoded
    payload = None
    if data is not None:
        payload = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(BASE + '/api/v1' + path, data=payload,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    if status not in expected:
        raise SystemExit(f'FAIL: Gitea {method} {path} returned HTTP {status}')
    return status, json.loads(body) if body else None


def require(condition, message):
    if not condition:
        raise SystemExit('FAIL: ' + message)
    print('PASS: ' + message, flush=True)


def command(args, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=30, **kwargs)
    return result.returncode, result.stdout


def directory_disable(disabled):
    # This fixed synthetic identity has no other UAC flags to preserve.
    with tempfile.NamedTemporaryFile(mode='w') as password:
        password.write(passwords['Administrator'])
        password.flush()
        change = (f'dn: CN={DIRECTORY_USER},CN=Users,DC=lab,DC=smolofis,DC=test\n'
                  'changetype: modify\nreplace: userAccountControl\n'
                  f'userAccountControl: {514 if disabled else 512}\n')
        code, _ = command([
            'ldapmodify', '-x', '-H', 'ldaps://dc',
            '-D', 'Administrator@lab.smolofis.test', '-y', password.name,
        ], input=change.encode(), env={**os.environ, 'LDAPTLS_CACERT': '/lab/ca.crt'})
        require(code == 0, 'synthetic directory account ' + ('disabled' if disabled else 'enabled'))


class LoginForm(HTMLParser):
    csrf = None
    has_login = False

    def __init__(self):
        super().__init__()
        self.inputs = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input':
            self.inputs.add(attrs.get('name'))
        if tag == 'input' and attrs.get('name') == '_csrf':
            self.csrf = attrs.get('value')
        if tag == 'form' and attrs.get('action') == '/user/login':
            self.has_login = True


def browser_login(credentials=local, remember=False):
    jar = http.cookiejar.CookieJar()
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    browser.lab_cookies = jar
    with browser.open(BASE + '/user/login', timeout=20) as response:
        form = LoginForm()
        form.feed(response.read().decode())
    require(form.has_login, 'browser login form found')
    fields = {'user_name': credentials[0], 'password': credentials[1]}
    if remember:
        fields['remember'] = 'on'
    if form.csrf:
        fields['_csrf'] = form.csrf
    data = urllib.parse.urlencode(fields).encode()
    # Current Gitea uses cross-origin protection rather than a hidden token.
    request = urllib.request.Request(BASE + '/user/login', data=data, headers={
        'Origin': BASE, 'Sec-Fetch-Site': 'same-origin',
    })
    with browser.open(request, timeout=20) as response:
        response.read()
    require(browser_allowed(browser), 'authenticated browser session established')
    return browser


def browser_allowed(browser):
    try:
        with browser.open(BASE + '/user/settings', timeout=20) as response:
            page = LoginForm()
            page.feed(response.read().decode())
            # Gitea may render an account-prohibited page with HTTP 200.
            return (urllib.parse.urlparse(response.url).path == '/user/settings'
                    and 'full_name' in page.inputs)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return False
        raise


def freeze_cookies(browser):
    records = []
    for cookie in browser.lab_cookies:
        record = vars(cookie).copy()
        record['rest'] = record.pop('_rest')
        records.append(record)
    return records


def replay_cookies(records):
    jar = http.cookiejar.CookieJar()
    for record in records:
        jar.set_cookie(http.cookiejar.Cookie(**record))
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    browser.lab_cookies = jar
    return browser


def check_session_revocation():
    fixture_path = Path('/evidence/session-fixture.json')
    fixture = json.loads(fixture_path.read_text())
    report = fixture['report']
    maintenance = json.loads(Path('/evidence/session-maintenance.json').read_text())
    require(maintenance['target_account_id'] == report['account_id']
            and maintenance['remember_tokens_removed'] > 0,
            'offline maintenance completed for the staged account')
    directory_disable(False)
    api('/admin/users/' + USER, 'PATCH', {
        'source_id': report['source_id'], 'login_name': DIRECTORY_USER,
        'prohibit_login': False,
    })
    _, account = api('/user', credentials=directory)
    require(account['id'] == report['account_id'], 're-enabled directory login retains the same account')
    for label, cookies in fixture['cookies'].items():
        require(not browser_allowed(replay_cookies(cookies)),
                f'{label} cannot resume after restart and account re-enabling')
    _, unaffected = api('/user', token=fixture['unaffected_api_token'])
    require(unaffected['login'] == 'lab-admin', 'office-wide browser sign-out preserves another user API token')
    api('/user', token=fixture['revoked_api_token'], expected=(401, 403))
    key_path = Path(fixture['key_path'])
    require(key_path.parent == Path('/evidence'), 'SSH fixture path stays within lab artifacts')
    ssh_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_SSH_COMMAND':
               'ssh -i ' + shlex.quote(str(key_path)) +
               ' -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10'
               ' -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/evidence/known_hosts'}
    code, _ = command(['git', 'ls-remote', f'ssh://git@git/{USER}/{REPO}.git'], env=ssh_env)
    require(code != 0, 'revoked SSH key remains unusable after the browser-session restart')
    browser_login(credentials=directory)
    fresh_remembered = browser_login(credentials=directory, remember=True)
    remember_only = [item for item in freeze_cookies(fresh_remembered) if item['name'] != 'i_like_gitea']
    # Zero-day remember tokens can be valid until the current second ends.
    time.sleep(1.1)
    require(not browser_allowed(replay_cookies(remember_only)),
            'zero-day remember setting cannot recreate a missing browser session')
    api('/users/lab-admin/tokens/' + str(fixture['unaffected_token_id']), 'DELETE', expected=(204,))
    report.update({
        'permanent_browser_session_revocation_passed': True,
        'revocation_scope': 'all-gitea-browser-users',
        'legacy_remember_cookie_replay_denied': True,
        'unaffected_api_token_preserved': True,
        'session_provider': 'memory', 'remember_days': 0,
    })
    Path('/evidence/session-report.json').write_text(json.dumps(report, indent=2) + '\n')
    key_path.unlink(missing_ok=True)
    key_path.with_suffix('.pub').unlink(missing_ok=True)
    fixture_path.unlink()
    print('PASS: permanent browser-session revocation with office-wide sign-out', flush=True)


def main():
    # Each run gets a fresh pair: the pinned API cannot reset source_id to 0.
    with tempfile.NamedTemporaryFile(mode='w') as password:
        password.write(passwords['Administrator'])
        password.flush()
        encoded = base64.b64encode(('"' + directory[1] + '"').encode('utf-16-le')).decode()
        change = (f'dn: CN={DIRECTORY_USER},CN=Users,DC=lab,DC=smolofis,DC=test\n'
                  f'objectClass: user\nsAMAccountName: {DIRECTORY_USER}\n'
                  f'userPrincipalName: {DIRECTORY_USER}@lab.smolofis.test\n'
                  f'mail: {DIRECTORY_USER}@lab.smolofis.test\n'
                  f'unicodePwd:: {encoded}\nuserAccountControl: 512\n')
        code, _ = command(['ldapadd', '-x', '-H', 'ldaps://dc',
                           '-D', 'Administrator@lab.smolofis.test', '-y', password.name],
                          input=change.encode(), env={**os.environ, 'LDAPTLS_CACERT': '/lab/ca.crt'})
        require(code == 0, 'new synthetic directory identity created for this migration run')
    _, existing = api('/admin/users', 'POST', {
        'username': USER, 'email': USER + '@lab.smolofis.test',
        'password': local[1], 'must_change_password': False,
    }, expected=(201,))

    _, identity = api('/user', credentials=local)
    status, repo = api(f'/repos/{USER}/{REPO}', credentials=local, expected=(200, 404))
    if status == 404:
        _, repo = api('/user/repos', 'POST', {
            'name': REPO, 'private': True, 'auto_init': True,
            'default_branch': 'main',
        }, credentials=local, expected=(201,))
    _, commits = api(f'/repos/{USER}/{REPO}/commits', credentials=local)
    original_commits = [commit['sha'] for commit in commits]
    require(bool(original_commits), 'local account owns a private repository with history')

    evidence = Path('/evidence')
    evidence.mkdir(mode=0o700, exist_ok=True)
    os.chmod(evidence, 0o700)
    suffix = secrets.token_hex(6)
    key_path = evidence / ('migration-key-' + suffix)
    code, _ = command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key_path)])
    require(code == 0, 'synthetic SSH key generated')
    _, key = api('/user/keys', 'POST', {
        'title': 'migration-' + suffix, 'key': key_path.with_suffix('.pub').read_text(),
    }, credentials=local, expected=(201,))
    _, token = api(f'/users/{USER}/tokens', 'POST', {
        'name': 'migration-' + suffix, 'scopes': ['read:user', 'read:repository'],
    }, credentials=local, expected=(201,))
    browser = browser_login()
    session_mode = '--prepare-session-revocation' in sys.argv
    cookie_sets = {}
    if session_mode:
        remembered = browser_login(remember=True)
        remember_only_before = [item for item in freeze_cookies(remembered) if item['name'] != 'i_like_gitea']
        remembered = replay_cookies(remember_only_before)
        require(browser_allowed(remembered),
                'remember cookie can recreate a session before revocation')
        # Auto-login rotates the remember token; freeze the replacement, not a stale token.
        remembered_cookies = freeze_cookies(remembered)
        require(any(item['name'] == 'gitea_incredible' and item['expires']
                    and item['expires'] > time.time() + 60 for item in remembered_cookies),
                'legacy persistent remember cookie exists before the policy change')
        other_browser = browser_login(credentials=admin)
        cookie_sets = {
            'dormant ordinary browser session': freeze_cookies(browser),
            'dormant remembered browser session': remembered_cookies,
            'remember-cookie-only replay': [item for item in remembered_cookies if item['name'] != 'i_like_gitea'],
            'another user browser session': freeze_cookies(other_browser),
        }
        _, unaffected_token = api('/users/lab-admin/tokens', 'POST', {
            'name': 'session-probe-' + suffix, 'scopes': ['read:user'],
        }, expected=(201,))
    code, host_key = command(['ssh-keyscan', '-T', '5', 'git'])
    require(code == 0 and bool(host_key), 'lab SSH host key captured')
    known_hosts = evidence / 'known_hosts'
    known_hosts.write_bytes(host_key)
    ssh_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_SSH_COMMAND':
               'ssh -i ' + shlex.quote(str(key_path)) +
               ' -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10'
               ' -o StrictHostKeyChecking=yes -o UserKnownHostsFile=' + shlex.quote(str(known_hosts))}

    def ssh_access():
        code, refs = command(['git', 'ls-remote', f'ssh://git@git/{USER}/{REPO}.git'], env=ssh_env)
        return code == 0 and bool(refs.strip())

    require(ssh_access(), 'SSH key reads the private repository before migration')
    api('/user', token=token['sha1'])
    # Resolve the source ID from the existing Alice account, not an assumed ID.
    _, alice = api('/admin/users', expected=(200,))
    source_id = next(user['source_id'] for user in alice if user['login'] == 'alice')
    require(source_id > 0, 'directory source ID resolved from a verified account')
    api('/admin/users/' + USER, 'PATCH', {
        'source_id': source_id, 'login_name': DIRECTORY_USER, 'email': existing['email'],
        'must_change_password': False,
    })
    _, migrated = api('/user', credentials=directory)
    require(migrated['id'] == identity['id'] and migrated['login'] == USER,
            'migration preserves Gitea account ID and username with a different directory login')
    _, migrated_repo = api(f'/repos/{USER}/{REPO}', credentials=directory)
    _, migrated_commits = api(f'/repos/{USER}/{REPO}/commits', credentials=directory)
    require(migrated_repo['id'] == repo['id'] and migrated_repo['owner']['id'] == identity['id']
            and [commit['sha'] for commit in migrated_commits] == original_commits,
            'migration preserves repository ownership and commit history')
    _, keys = api('/user/keys', credentials=directory)
    require(any(item['id'] == key['id'] for item in keys) and ssh_access(),
            'migration preserves the existing working SSH key')
    api('/user', token=token['sha1'])
    require(True, 'migration preserves the existing API token')
    api('/user', credentials=local, expected=(401, 403))
    require(True, 'old local password cannot authenticate after directory association')

    paused = False
    try:
        directory_disable(True)
        api('/user', credentials=directory, expected=(401, 403))
        status, _ = api('/user', token=token['sha1'], expected=(200, 401, 403))
        observations = {
            'token_survives_directory_disable': status == 200,
            'ssh_key_survives_directory_disable': ssh_access(),
            'browser_session_survives_directory_disable': browser_allowed(browser),
        }
        for name, value in observations.items():
            print(f'OBSERVED: {name}={value}', flush=True)

        # Gitea must be fenced independently while directory authentication is down.
        api('/admin/users/' + USER, 'PATCH', {
            'source_id': source_id, 'login_name': DIRECTORY_USER,
            'email': existing['email'], 'prohibit_login': True,
        })
        api('/user', token=token['sha1'], expected=(401, 403))
        require(not ssh_access(), 'Gitea login prohibition blocks existing SSH key')
        require(not browser_allowed(browser), 'Gitea login prohibition blocks existing browser session')
        require(True, 'Gitea login prohibition blocks existing API token')
        _, all_tokens = api(f'/users/{USER}/tokens')
        for credential in all_tokens:
            api(f'/users/{USER}/tokens/{credential["id"]}', 'DELETE', expected=(204,))
        for credential in keys:
            api(f'/admin/users/{USER}/keys/{credential["id"]}', 'DELETE', expected=(204,))
        if session_mode:
            require(browser_allowed(other_browser), 'other user browser is still signed in before maintenance')
            report = {**observations, 'username': USER, 'directory_username': DIRECTORY_USER,
                      'account_id': identity['id'], 'repo_id': repo['id'],
                      'commits': original_commits, 'source_id': source_id}
            # These fixture cookies/tokens are secrets, unlike the final metadata report.
            fixture = evidence / 'session-fixture.json'
            fixture.write_text(json.dumps({
                'report': report, 'cookies': cookie_sets, 'key_path': str(key_path),
                'revoked_api_token': token['sha1'],
                'unaffected_api_token': unaffected_token['sha1'],
                'unaffected_token_id': unaffected_token['id'],
            }))
            fixture.chmod(0o600)
            paused = True
            print('PASS: target prohibited and credential revocation staged for offline session maintenance', flush=True)
            return
        directory_disable(False)
        api('/admin/users/' + USER, 'PATCH', {
            'source_id': source_id, 'login_name': DIRECTORY_USER,
            'email': existing['email'], 'prohibit_login': False,
        })
        api('/user', credentials=directory)
        observations['browser_session_survives_reenable'] = browser_allowed(browser)
        print(f'OBSERVED: browser_session_survives_reenable={observations["browser_session_survives_reenable"]}', flush=True)
        api('/user', token=token['sha1'], expected=(401, 403))
        require(not ssh_access(), 'deleted SSH credential stays revoked after re-enabling the account')
        require(True, 'deleted API token stays revoked after re-enabling the account')
        report = {**observations, 'username': USER, 'directory_username': DIRECTORY_USER,
                  'account_id': identity['id'], 'repo_id': repo['id'],
                  'commits': original_commits, 'source_id': source_id,
                  'migration_passed': True, 'token_and_ssh_revocation_passed': True,
                  'permanent_browser_session_revocation_passed': False}
        (evidence / 'migration-report.json').write_text(json.dumps(report, indent=2) + '\n')
    finally:
        if not paused:
            directory_disable(False)
            # Leave the synthetic account usable, even after a failed assertion.
            api('/admin/users/' + USER, 'PATCH', {
                'source_id': source_id, 'login_name': DIRECTORY_USER,
                'email': existing['email'], 'prohibit_login': False,
            })
            key_path.unlink(missing_ok=True)
            key_path.with_suffix('.pub').unlink(missing_ok=True)


if __name__ == '__main__':
    if '--check-session-revocation' in sys.argv:
        check_session_revocation()
    elif '--check-restored' in sys.argv:
        report = json.loads(Path('/evidence/migration-report.json').read_text())
        _, account = api('/user', credentials=directory)
        _, repo = api(f'/repos/{USER}/{REPO}', credentials=directory)
        _, commits = api(f'/repos/{USER}/{REPO}/commits', credentials=directory)
        require(account['id'] == report['account_id'] and account['source_id'] == report['source_id']
                and repo['id'] == report['repo_id'] and repo['owner']['id'] == report['account_id']
                and [commit['sha'] for commit in commits] == report['commits'],
                'cold restore preserves directory login, Gitea mapping, repository, and history')
    else:
        main()
