"""Kerberos skew detection/recovery with process-local fake time, never set clocks."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

root = Path('/var/lib/smolofis-identity-machine-lab')
if os.geteuid() != 0 or not (root / 'domain-ready').exists() or not Path('/etc/smolofis-m0-fullvm').exists():
    raise SystemExit('Refusing clock-skew probe outside the owned VM')
password = json.loads((root / 'passwords.json').read_text())['alice']
libraries = subprocess.check_output(['dpkg', '-L', 'libfaketime'], text=True).splitlines()
library = next((path for path in libraries if path.endswith('/libfaketime.so.1')), None)
if not library:
    raise SystemExit('Install the libfaketime lab dependency first')
with tempfile.TemporaryDirectory() as directory:
    temp = Path(directory)
    config = temp / 'krb5.conf'
    config.write_text('''[libdefaults]
 default_realm = M0.SMOLOFIS.TEST
 dns_lookup_kdc = false
 dns_lookup_realm = false
 kdc_timesync = 0
 clockskew = 300
 udp_preference_limit = 1
[realms]
 M0.SMOLOFIS.TEST = {
  kdc = 172.31.83.10
 }
''')
    environment = {**os.environ, 'KRB5_CONFIG': str(config), 'KRB5CCNAME': 'FILE:' + str(temp / 'ticket')}
    def authenticate(skew=False):
        (temp / 'ticket').unlink(missing_ok=True)
        env = dict(environment)
        if skew:
            env.update(LD_PRELOAD=library, FAKETIME='+10m', FAKETIME_DONT_FAKE_MONOTONIC='1')
        return subprocess.run(['kinit', 'alice@M0.SMOLOFIS.TEST'], input=(password + '\n').encode(),
                              capture_output=True, env=env, timeout=45)
    if authenticate().returncode:
        raise SystemExit('FAIL: healthy Kerberos baseline; output suppressed')
    print('PASS: Kerberos login with real process time', flush=True)
    before = time.time()
    result = authenticate(skew=True)
    if result.returncode == 0 or b'clock skew' not in result.stderr.lower():
        raise SystemExit('FAIL: skewed Kerberos client did not report clock skew; output suppressed')
    if abs(time.time() - before) > 120:
        raise SystemExit('FAIL: unexpected real-time discontinuity')
    print('PASS: process-local +10-minute client skew produces explicit Kerberos clock-skew error', flush=True)
    if authenticate().returncode:
        raise SystemExit('FAIL: Kerberos recovery after removing process-local skew')
    print('PASS: normal-time Kerberos client recovers; no system clock changed', flush=True)
(root / 'clock-skew-report.json').write_text(json.dumps({'baseline_passed': True,
    'skew_seconds': 600, 'explicit_skew_error': True, 'recovery_passed': True,
    'system_clock_changed': False, 'client_kdc_timesync': 0}, indent=2))
