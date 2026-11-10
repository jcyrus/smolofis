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

def request(opener, path, fields=None, headers=None):
    h = {'Origin': ORIGIN}
    if headers: h.update(headers)
    req = urllib.request.Request(ORIGIN + path, headers=h,
        data=urllib.parse.urlencode(fields).encode() if fields is not None else None)
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
code, account, _ = request(browser, '/auth/account')
assert code == 200
csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', account).group(1)
code, _, _ = request(browser, '/auth/logout', {'csrf': csrf})
assert code == 303 and request(browser, '/api/state')[0] == 401
passed('CSRF-protected sign-out removes access through the actual HTTPS frontend')
report = {'checks': checks, 'origin': ORIGIN, 'certificate_verification': 'required with hostname checking',
          'production_admin_enabled': False, 'network_employee_denial_tested': False}
output = Path('/private/tmp/smolofis-private-https-peer-report.json')
output.write_text(json.dumps(report, indent=2)); output.chmod(0o600)
print(f'PASS: {len(checks)} real HTTPS peer checks; credential-free report: {output}', flush=True)
