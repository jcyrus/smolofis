#!/usr/bin/env python3
"""Real peer HTTPS acceptance with normal CA verification; no saved sessions."""
import http.cookiejar
import json
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

import os
ORIGIN = os.environ['SMOLOFIS_LAB_HTTPS_ORIGIN']
assert ORIGIN.startswith('https://smolofis-m0-private.') and ORIGIN.endswith('.ts.net')
password = (Path(__file__).resolve().parents[4] / 'work/identity-m0-vm/private-https-password').read_text()
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

context = ssl.create_default_context()
assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED

def client():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context),
        urllib.request.HTTPCookieProcessor(jar), NoRedirect())
    return opener

def request(opener, path, fields=None, headers=None, json_body=None):
    h = {'Origin': ORIGIN}
    if headers: h.update(headers)
    data = urllib.parse.urlencode(fields).encode() if fields is not None else None
    if json_body is not None:
        data = json.dumps(json_body).encode(); h['Content-Type'] = 'application/json'
    req = urllib.request.Request(ORIGIN + path, headers=h, data=data)
    try:
        response = opener.open(req, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.code, response.read().decode(), response.headers

checks = []
def passed(label):
    checks.append(label); print('PASS: ' + label, flush=True)

def challenge(opener):
    code, html, headers = request(opener, '/auth/login')
    assert code == 200 and 'no-store' in headers.get('Cache-Control', '')
    cookie = next(value for value in headers.get_all('Set-Cookie', []) if value.startswith('smolofis_login='))
    assert all(flag in cookie for flag in ('Secure', 'HttpOnly', 'SameSite=Strict'))
    return re.search(r'name="csrf" value="([a-f0-9]+)"', html).group(1)

browser = client()
assert request(browser, '/healthz')[0] == 200
assert request(browser, '/api/state')[0] == 401
passed('trusted public certificate and hostname work from a real tailnet peer; telemetry requires authentication')
code, _, _ = request(client(), '/api/state', headers={'Tailscale-User-Login': 'forged@example.invalid', 'X-Forwarded-User': 'mobile-admin'})
assert code in (400, 401, 403)
passed('forged proxy identity headers cannot authenticate an anonymous request')
code, _, _ = request(client(), '/auth/login', headers={'Host': 'untrusted.example.invalid'})
assert code in (400, 403, 404, 421)
passed('a spoofed HTTP Host is rejected on the real HTTPS connection')

csrf = challenge(browser)
passed('real Serve preserves login Host/origin and Secure HttpOnly Strict no-store cookies')
code, _, headers = request(browser, '/auth/login', {'username': 'mobile-admin', 'password': password, 'csrf': csrf},
                          {'Origin': 'https://untrusted.example'})
assert code == 403 and not any(value.startswith('smolofis_session=') for value in headers.get_all('Set-Cookie', []))
passed('cross-origin login is rejected before a session is issued')
code, _, _ = request(browser, '/auth/login', {'username': 'mobile-admin', 'password': password, 'csrf': '0'*64})
assert code == 403
passed('invalid login CSRF is rejected over the actual HTTPS path')
ordinary = client(); token = challenge(ordinary)
code, _, headers = request(ordinary, '/auth/login', {'username': 'mobile-user', 'password': password, 'csrf': token})
assert code == 401 and not any(value.startswith('smolofis_session=') for value in headers.get_all('Set-Cookie', []))
passed('valid directory user outside the console administrator group receives no session')
csrf = challenge(browser)
code, _, headers = request(browser, '/auth/login', {'username': 'mobile-admin', 'password': password, 'csrf': csrf})
assert code == 303
cookie = next(value for value in headers.get_all('Set-Cookie', []) if value.startswith('smolofis_session='))
assert all(flag in cookie for flag in ('Secure', 'HttpOnly', 'SameSite=Strict'))
assert request(browser, '/api/state')[0] == 200
assert request(browser, '/console')[0] == 200
passed('enrolled directory administrator signs in and reads console/telemetry through real Tailscale HTTPS')
operation_report = None
if os.environ.get('SMOLOFIS_LAB_MANAGEMENT_TEST') == 'true':
    code, services, _ = request(browser, '/console/services')
    assert code == 200 and 'data-run-identity' in services and '/assets/operations.js' in services
    operation_csrf = re.search(r'data-csrf="([a-f0-9]+)"', services).group(1)
    assert request(browser, '/assets/operations.js')[0] == 200
    passed('CI-built console exposes the opt-in diagnostic controls and JavaScript through trusted HTTPS')
    code, body, _ = request(browser, '/api/operations')
    assert code == 200
    before = json.loads(body)['operations']
    endpoint = '/api/operations/identity-check'
    for payload, headers, expected in [
        ({'csrf': operation_csrf}, {'Origin': 'https://untrusted.example'}, 403),
        ({'csrf': '0'*64}, {}, 403),
        ({'csrf': operation_csrf, 'actor_guid': 'ab'*16}, {}, 422),
        ({'csrf': operation_csrf, 'command': 'id'}, {}, 422),
    ]:
        assert request(browser, endpoint, headers=headers, json_body=payload)[0] == expected
    assert json.loads(request(browser, '/api/operations')[1])['operations'] == before
    passed('actual HTTPS operation requests enforce Origin, CSRF and strict body fields without adding rejected jobs')
    code, body, headers = request(browser, endpoint, json_body={'csrf': operation_csrf})
    assert code == 201 and 'no-store' in headers.get('Cache-Control', '')
    operation = json.loads(body)
    assert operation['outcome'] == 'identity_verified' and operation['actor_uid'] == 0
    assert re.fullmatch('[a-f0-9]{32}', operation['actor_guid']) and operation['requester_uid'] > 0
    assert [t['phase'] for t in operation['transitions']] == ['queued', 'running', 'succeeded']
    passed('real HTTPS submission executes through the root worker with administrator and peer attribution')
    assert json.loads(request(browser, '/api/operations/' + operation['id'])[1]) == operation
    after = json.loads(request(browser, '/api/operations')[1])['operations']
    assert len(after) == len(before)+1 and after[-1] == operation
    assert request(browser, '/api/operations/bad-id')[0] == 400
    assert request(browser, '/api/operations/' + 'fe'*32)[0] == 404
    passed('HTTPS status/history reads preserve the confirmed result and reject invalid or unknown references')
    assert request(client(), '/api/operations')[0] == 401
    assert request(client(), endpoint, json_body={'csrf': operation_csrf})[0] == 401
    passed('anonymous HTTPS clients cannot read operation history or submit checks')
    operation_report = {'operation_id': operation['id'], 'actor_guid': operation['actor_guid'],
                        'requester_uid': operation['requester_uid'], 'outcome': operation['outcome']}

code, account, _ = request(browser, '/auth/account')
assert code == 200
csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', account).group(1)
code, _, _ = request(browser, '/auth/logout', {'csrf': csrf})
assert code == 303 and request(browser, '/api/state')[0] == 401
passed('CSRF-protected sign-out removes access through the actual HTTPS frontend')
if operation_report:
    assert request(browser, '/api/operations')[0] == 401
    passed('signed-out HTTPS session loses access to the management API')
report = {'checks': checks, 'origin': ORIGIN, 'certificate_verification': 'required with hostname checking',
          'operation': operation_report, 'production_admin_enabled': False, 'network_employee_denial_tested': False}
output = Path('/private/tmp/smolofis-private-https-peer-report.json')
output.write_text(json.dumps(report, indent=2)); output.chmod(0o600)
print(f'PASS: {len(checks)} real HTTPS peer checks; credential-free report: {output}', flush=True)
