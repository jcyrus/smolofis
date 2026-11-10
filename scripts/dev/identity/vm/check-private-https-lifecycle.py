#!/usr/bin/env python3
"""Real peer + owned full VM HTTPS lifecycle acceptance; no production writes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location('lab', Path(__file__).with_name('lab.py'))
lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab); lab.PORT = '12223'
ROOT = '/var/lib/smolofis-identity-machine-lab'
BACKEND, HTTPS = 'smolofis-console-preview.service', 'smolofis-console-https-preview.service'

def remote(args, check=True, timeout=30):
    result = subprocess.run(lab.ssh_args() + [shlex.join(['sudo', '-n', *args])],
                            capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('Owned lab command failed; credential-free diagnostics retained separately')
    return result

record = json.loads(remote(['cat', ROOT + '/private-https-trial.json']).stdout)
origin = record['origin']; host = origin.split('://', 1)[1]
assert origin.startswith('https://smolofis-m0-private.') and origin.endswith('.ts.net')
runtime, evidence = record['runtime'], record['evidence']
assert runtime.startswith('/run/smolofis-private-https-') and evidence.startswith(ROOT + '/private-https-')
context = ssl.create_default_context()
checks = []

def passed(label):
    checks.append(label); print('PASS: ' + label, flush=True)

def status(unit):
    return remote(['systemctl', 'show', unit, '-p', 'ActiveState', '--value']).stdout.decode().strip()

def serve():
    return json.loads(remote(['tailscale', 'serve', 'status', '--json']).stdout)

def empty_serve():
    for _ in range(20):
        value = serve()
        if not value or all(v is None or v == {} for v in value.values()): return
        time.sleep(.5)
    raise AssertionError('Foreground Serve session remains after stop/crash')

def https_code():
    try:
        with urllib.request.urlopen(origin + '/healthz', context=context, timeout=5) as r: return r.code
    except urllib.error.HTTPError as error:
        return error.code
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError): raise
        return None

def unavailable():
    assert https_code() is None, 'HTTPS still responds after foreground owner stopped'

def available():
    for _ in range(20):
        if https_code() == 200: return
        time.sleep(.5)
    raise AssertionError('HTTPS did not return')

def write_report(name, value):
    data = json.dumps(value, indent=2).encode()
    command = 'umask 077; cat > ' + shlex.quote(evidence + '/' + name)
    subprocess.run(lab.ssh_args() + ['sudo -n sh -c ' + shlex.quote(command)], input=data,
                   capture_output=True, check=True, timeout=20)

renewal = json.loads(remote(['cat', evidence + '/renewal.json']).stdout)
with socket.create_connection((host, 443), timeout=10) as raw:
    with context.wrap_socket(raw, server_hostname=host) as tls:
        certificate = tls.getpeercert()
assert certificate['serialNumber'] == renewal['after_serial']
passed('Serve presents the renewed publicly trusted certificate without restarting the backend')
listeners = remote(['ss', '-lntH', '( sport = :8091 )']).stdout.decode().splitlines()
assert len(listeners) == 1 and listeners[0].split()[3] == '127.0.0.1:8091'
node = json.loads(remote(['tailscale', 'status', '--json']).stdout)
node_ip = next(ip for ip in node['TailscaleIPs'] if ':' not in ip)
try:
    socket.create_connection((node_ip, 8091), timeout=3).close()
except OSError:
    pass
else:
    raise AssertionError('Private HTTP backend is reachable directly from a peer')
passed('backend is kernel-bound to loopback and cannot be reached directly from a tailnet peer')
remote(['systemctl', 'stop', HTTPS]); empty_serve(); unavailable()
assert status(BACKEND) == 'active'
passed('normal HTTPS stop removes its own foreground session and remote listener while backend stays local')
remote(['systemctl', 'start', HTTPS]); available()
remote(['systemctl', 'kill', '--kill-whom=main', '--signal=KILL', HTTPS])
empty_serve(); unavailable()
result = remote(['systemctl', 'show', HTTPS, '-p', 'Result', '-p', 'ExecMainStatus']).stdout.decode()
assert 'Result=signal' in result and 'ExecMainStatus=9' in result
write_report('serve-crash.json', {'result': result, 'foreground_removed': True})
remote(['systemctl', 'reset-failed', HTTPS])
passed('SIGKILL of Serve CLI removes its temporary configuration without a global reset')
remote(['systemctl', 'start', HTTPS]); available()
remote(['systemctl', 'stop', BACKEND]); empty_serve(); unavailable()
assert status(HTTPS) != 'active'
remote(['systemctl', 'start', BACKEND])
assert status(HTTPS) != 'active'
passed('backend stop also stops HTTPS; backend restart does not silently republish it')
remote(['systemctl', 'start', HTTPS]); available()
# Save only credential-free public metadata and typed audit/state before /run clears.
for name in ('local.jsonl', 'http.jsonl', 'panel-enrollment.json'):
    remote(['cp', runtime + '/' + name, evidence + '/pre-reboot-' + name])
boot_before = remote(['cat', '/proc/sys/kernel/random/boot_id']).stdout.decode().strip()
write_report('lifecycle-before-reboot.json', {'checks': checks, 'boot_before': boot_before,
             'mobile_cellular_https_signin': 'confirmed by user', 'production_changed': False})
remote(['systemctl', 'reboot'])
deadline = time.monotonic() + 420
while time.monotonic() < deadline:
    try:
        boot = remote(['cat', '/proc/sys/kernel/random/boot_id'], check=False, timeout=12)
        if boot.returncode == 0 and boot.stdout.decode().strip() != boot_before: break
    except subprocess.TimeoutExpired:
        pass
    time.sleep(3)
else:
    raise AssertionError('Owned lab VM did not return after reboot')
boot_after = boot.stdout.decode().strip()
for _ in range(40):
    state = remote(['tailscale', 'status', '--json'], check=False)
    if state.returncode == 0:
        node = json.loads(state.stdout)
        if node.get('BackendState') == 'Running' and (node.get('Self') or {}).get('Online'): break
    time.sleep(2)
else:
    raise AssertionError('Lab tailnet node did not recover online')
empty_serve(); unavailable()
assert status(HTTPS) != 'active' and status(BACKEND) != 'active'
public_cert = evidence + '/public-after-reboot.crt'
remote(['tailscale', 'cert', '--cert-file=' + public_cert, '--min-validity=0s', host], timeout=60)
script = 'import ssl,sys; print(ssl._ssl._test_decode_cert(sys.argv[1])["serialNumber"])'
serial = remote(['python3', '-c', script, public_cert]).stdout.decode().strip()
assert serial == renewal['after_serial']
passed('VM reboot preserves node/certificate identity but does not resurrect foreground Serve or the opt-in console')
write_report('lifecycle-report.json', {'checks': checks, 'boot_before': boot_before, 'boot_after': boot_after,
             'mobile_cellular_https_signin': 'confirmed by user', 'production_changed': False,
             'scheduled_renewal_after_waiting_for_expiry': False, 'employee_network_denial_tested': False})
print('PASS: ' + str(len(checks)) + ' real HTTPS lifecycle checks; evidence: ' + evidence, flush=True)
