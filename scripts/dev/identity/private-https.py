#!/usr/bin/env python3
"""Real TLS + enrolled LDAP console acceptance; Tailscale CLI is a read-only fixture.
No tailnet, public certificates, host ports, or external DNS are used.
"""
import http.client
import http.cookiejar
import http.server
import json
import os
from pathlib import Path
import runpy
import secrets
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request

lab = runpy.run_path('/usr/local/bin/lab-panel-auth')
command, Browser, NoRedirect = (lab[name] for name in ('command', 'Browser', 'NoRedirect'))
ROOT = Path('/run/smolofis-private-https')
ROOT.mkdir(mode=0o755)
ROOT.chmod(0o755)
CONF = Path('/etc/smolofis')
CONF.mkdir(mode=0o755, exist_ok=True)
CONF.chmod(0o755)
CONFIG = CONF / 'panel-auth.json'
STATE = CONF / 'panel-enrollment.json'
HOST = 'office.fixture.ts.net'
ORIGIN = 'https://' + HOST
BINARY = '/panel/smolofis-panel'
checks = []


def passed(message):
    checks.append(message)
    print('PASS: ' + message, flush=True)


Path('/etc/machine-id').write_text(secrets.token_hex(16) + '\n')
Path('/etc/machine-id').chmod(0o644)
config = json.loads(Path('/lab/panel-auth.json').read_text())
config['origin'] = ORIGIN
CONFIG.write_text(json.dumps(config))
CONFIG.chmod(0o644)
passwords = json.loads(Path('/lab/panel-passwords.json').read_text())
command(BINARY, '--auth-enroll', str(CONFIG), str(STATE), 'panel-admin', data=passwords['panel-admin'])

# The production check runs only these local read-only CLI status commands.
# Fixture JSON is deliberately mutable so refusal cases exercise the real binary.
node_file = ROOT / 'node.json'
serve_file = ROOT / 'serve.json'
node = {'BackendState': 'Running', 'Self': {'Online': True, 'DNSName': HOST + '.'}}
node_file.write_text(json.dumps(node))
serve_file.write_text('null')
shim = Path('/usr/bin/tailscale')
shim.write_text('''#!/usr/bin/python3
import json, sys
from pathlib import Path
root = Path('/run/smolofis-private-https')
args = sys.argv[1:]
with (root / 'cli-calls.jsonl').open('a') as out:
    out.write(json.dumps(args) + '\\n')
if args == ['status', '--json']:
    print((root / 'node.json').read_text())
elif args == ['serve', 'status', '--json']:
    print((root / 'serve.json').read_text())
else:
    raise SystemExit('refusing non-read-only Tailscale fixture command')
''')
shim.chmod(0o755)

process = subprocess.Popen([BINARY, '--private-console'], user=65534, group=65534, extra_groups=[],
    env={**os.environ, 'RUST_LOG': 'error',
         # Private mode must ignore environment attempts to widen/disable it.
         'SMOLOFIS_BIND': '0.0.0.0:18099', 'SMOLOFIS_CONSOLE_ENABLED': 'false',
         'SMOLOFIS_AUTH_CONFIG': '/missing/config', 'SMOLOFIS_AUTH_STATE': '/missing/state'},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
proxy = None
try:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError('private panel exited during startup')
        try:
            with urllib.request.urlopen('http://127.0.0.1:8091/healthz', timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        time.sleep(.1)
    else:
        raise RuntimeError('private console did not become live')
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1', 18099)) != 0
    # A separate network namespace peer cannot hit this loopback listener;
    # inspect the kernel listener to ensure it did not bind any wildcard/IP.
    listeners = Path('/proc/net/tcp').read_text().splitlines()[1:]
    bindings = [line.split()[1] for line in listeners if line.split()[1].endswith(':1F9B') and line.split()[3] == '0A']
    assert bindings == ['0100007F:1F9B'], bindings  # 8091 in hex
    passed('private mode fixes loopback port 8091 and requires fixed enrolled config despite environment overrides')

    def preflight(expected=0):
        result = subprocess.run([BINARY, '--console-access-check', str(CONFIG), str(STATE)],
                                capture_output=True, text=True, timeout=12)
        assert result.returncode == expected, 'unexpected transport preflight result; output suppressed'
        return result
    ready = json.loads(preflight().stdout)
    assert ready['origin'] == ORIGIN and ready['existing_serve'] == 'empty'
    passed('real backend authentication, Secure cookie, enrollment and matching online node pass preflight')
    for fixture in [
        {'TCP': {'443': {'HTTPS': True}}},
        {'AllowFunnel': {HOST + ':443': True}},
        {'Foreground': {'unrelated': {}}},
        {'Services': {'svc:unrelated': {}}},
        {'Unknown': {}},
    ]:
        serve_file.write_text(json.dumps(fixture))
        preflight(expected=1)
    serve_file.write_text('null')
    passed('existing Serve/Funnel/services and unknown configuration are rejected without mutation')
    for changed in [dict(node, BackendState='Stopped'),
                    {'BackendState': 'Running', 'Self': {'Online': False, 'DNSName': HOST}},
                    {'BackendState': 'Running', 'Self': {'Online': True, 'DNSName': 'other.fixture.ts.net'}}]:
        node_file.write_text(json.dumps(changed))
        preflight(expected=1)
    node_file.write_text(json.dumps(node))
    passed('offline node and a different MagicDNS identity fail closed')

    # Locally generated disposable CA and server certificate; certificate chain
    # and hostname verification remain enabled in the HTTPS client below.
    command('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=SmolOfis disposable transport CA', '-keyout', str(ROOT / 'ca.key'),
            '-out', str(ROOT / 'ca.crt'), '-addext', 'basicConstraints=critical,CA:TRUE',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-addext', 'subjectKeyIdentifier=hash')
    command('openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=' + HOST,
            '-keyout', str(ROOT / 'server.key'), '-out', str(ROOT / 'server.csr'))
    (ROOT / 'server.ext').write_text('subjectAltName=DNS:' + HOST + '\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n')
    command('openssl', 'x509', '-req', '-in', str(ROOT / 'server.csr'),
            '-CA', str(ROOT / 'ca.crt'), '-CAkey', str(ROOT / 'ca.key'), '-CAcreateserial',
            '-days', '1', '-extfile', str(ROOT / 'server.ext'), '-out', str(ROOT / 'server.crt'))

    class Proxy(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log POST bodies, passwords, or cookies.
        def do_GET(self):
            self.forward()
        def do_POST(self):
            self.forward()
        def forward(self):
            backend = http.client.HTTPConnection('127.0.0.1', 8091, timeout=5)
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            headers = dict(self.headers)
            backend.request(self.command, self.path, body=body, headers=headers)
            response = backend.getresponse()
            data = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in ('connection', 'transfer-encoding', 'content-length'):
                    self.send_header(name, value)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            backend.close()
    proxy = http.server.ThreadingHTTPServer(('127.0.0.1', 443), Proxy)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(ROOT / 'server.crt', ROOT / 'server.key')
    proxy.socket = server_context.wrap_socket(proxy.socket, server_side=True)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()

    class LocalHTTPS(http.client.HTTPSConnection):
        def connect(self):
            self.sock = self._context.wrap_socket(socket.create_connection(('127.0.0.1', 443), timeout=5), server_hostname=self.host)
    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return self.do_open(LocalHTTPS, request, context=self._context)
    class TLSBrowser(Browser):
        def __init__(self, origin=ORIGIN, context=None):
            super().__init__(origin)
            context = context or ssl.create_default_context(cafile=ROOT / 'ca.crt')
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                urllib.request.HTTPCookieProcessor(self.jar), HTTPSHandler(context=context))
    browser = TLSBrowser()
    status, _, headers = browser.request('/auth/login')
    assert status == 200 and headers.get('Strict-Transport-Security') is None
    assert all(cookie.secure for cookie in browser.jar)
    assert browser.login('panel-admin', passwords['panel-admin']) == 303
    assert browser.request('/api/state')[0] == 200
    assert browser.request('/console')[0] == 200
    passed('verified TLS proxy preserves Host/Origin and supports real LDAPS sign-in, protected telemetry and console')
    insecure = urllib.request.Request('http://' + HOST + '/api/state')
    browser.jar.add_cookie_header(insecure)
    assert not insecure.has_header('Cookie')
    backend = http.client.HTTPConnection('127.0.0.1', 8091, timeout=3)
    backend.request('GET', '/api/state', headers={'Host': HOST})
    assert backend.getresponse().status == 401
    backend.close()
    backend = http.client.HTTPConnection('127.0.0.1', 8091, timeout=3)
    backend.request('GET', '/auth/login', headers={'Host': 'attacker.example', 'X-Forwarded-Host': HOST, 'X-Forwarded-Proto': 'https'})
    assert backend.getresponse().status == 421
    backend.close()
    passed('Secure cookies stay off HTTP; forwarding headers cannot bypass the configured Host')
    for candidate in [TLSBrowser(context=ssl.create_default_context()), TLSBrowser(origin='https://wrong.fixture.ts.net')]:
        try:
            candidate.request('/healthz')
        except urllib.error.URLError:
            pass
        else:
            raise AssertionError('invalid TLS certificate unexpectedly accepted')
    passed('untrusted TLS CA and certificate hostname mismatch are rejected by the client')
    command(BINARY, '--auth-lock', str(STATE))
    preflight(expected=1)
    assert browser.request('/api/state')[0] == 503
    assert browser.request('/healthz')[0] == 200
    passed('local lockdown blocks HTTPS access and transport preflight while liveness remains public')
    calls = [json.loads(line) for line in (ROOT / 'cli-calls.jsonl').read_text().splitlines()]
    assert calls and all(call in (['status', '--json'], ['serve', 'status', '--json']) for call in calls)
    passed('preflight invokes only read-only local Tailscale status commands')
    print(json.dumps({'checks': checks, 'scope': 'real TLS, loopback, enrollment and Samba; Tailscale status mocked, no live tailnet or public issuance tested'}), flush=True)
finally:
    if proxy is not None:
        proxy.shutdown()
        proxy.server_close()
    process.terminate()
    process.wait(timeout=5)
