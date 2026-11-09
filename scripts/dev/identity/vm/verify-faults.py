"""Bounded DNS and NTP-service outages in the owned guest; never change clocks."""
import json
import os
from pathlib import Path
import subprocess
import time

root = Path('/var/lib/smolofis-identity-machine-lab')
if os.geteuid() != 0 or not (root / 'domain-ready').exists() or not Path('/etc/smolofis-m0-fullvm').exists():
    raise SystemExit('Refusing fault injection outside the owned lab VM')
def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, timeout=20, **kwargs)
def required(*args):
    result = run(*args)
    if result.returncode:
        raise SystemExit('FAIL: lab fault operation: ' + args[0])
    return result.stdout.decode().strip()
def dns_ok():
    try:
        result = run('dig', '+time=2', '+tries=1', '+short', '_ldap._tcp.m0.smolofis.test', 'SRV')
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0 and b'dc-m0.m0.smolofis.test.' in result.stdout
if not dns_ok():
    raise SystemExit('Refusing DNS fault without a healthy baseline')
if run('nft', 'list', 'table', 'inet', 'm0_fault').returncode == 0:
    raise SystemExit('Refusing to overwrite existing fault rules')
rules = '''table inet m0_fault {
 chain output {
  type filter hook output priority -20; policy accept;
  ip daddr 172.31.83.10 udp dport 53 drop
  ip daddr 172.31.83.10 tcp dport 53 drop
 }
}
'''
try:
    result = run('nft', '-f', '-', input=rules.encode())
    if result.returncode:
        raise SystemExit('FAIL: DNS fault installation')
    required('resolvectl', 'flush-caches')
    if dns_ok():
        raise SystemExit('FAIL: DNS health did not detect authoritative DNS outage')
    print('PASS: normal resolver detects injected AD DNS outage', flush=True)
finally:
    required('nft', 'delete', 'table', 'inet', 'm0_fault')
    required('resolvectl', 'reset-server-features')
    required('resolvectl', 'flush-caches')
for _ in range(10):
    if dns_ok():
        break
    time.sleep(2)
else:
    raise SystemExit('FAIL: AD DNS did not recover after fault removal')
print('PASS: DNS lookup recovers after fault removal and cache flush', flush=True)
initial_ntp = required('timedatectl', 'show', '-p', 'NTP', '--value')
if initial_ntp != 'yes':
    raise SystemExit('Refusing NTP fault without enabled NTP baseline')
try:
    required('timedatectl', 'set-ntp', 'false')
    if required('timedatectl', 'show', '-p', 'NTP', '--value') != 'no':
        raise SystemExit('FAIL: disabled NTP service not observable')
    print('PASS: NTP service outage is observable independently of cached synchronization status', flush=True)
finally:
    required('timedatectl', 'set-ntp', 'true')
for _ in range(60):
    if required('timedatectl', 'show', '-p', 'NTPSynchronized', '--value') == 'yes':
        break
    time.sleep(1)
else:
    raise SystemExit('FAIL: NTP did not regain synchronization')
report = {'dns_outage_detected': True, 'dns_recovery_passed': True,
          'ntp_service_outage_detected': True, 'ntp_reenabled_and_synchronized': True,
          'clock_changed': False, 'kerberos_clock_skew_tested': False}
(root / 'fault-report.json').write_text(json.dumps(report, indent=2))
print('PASS: NTP re-enabled and synchronized; no clock-skew acceptance claimed', flush=True)
