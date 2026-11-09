"""Route the synthetic AD zone to its authoritative guest DNS server."""
import os
from pathlib import Path
import re
import subprocess

if os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists() or not Path('/var/lib/smolofis-identity-machine-lab/domain-ready').exists():
    raise SystemExit('Refusing DNS configuration outside the owned VM')
path = Path('/etc/samba/smb.conf')
config = path.read_text()
pattern = r'(?im)^\s*dns forwarder\s*=.*$'
if re.search(pattern, config):
    config = re.sub(pattern, '\tdns forwarder = 10.0.2.3', config)
else:
    config = re.sub(r'(?im)^\[global\]\s*$', '[global]\n\tdns forwarder = 10.0.2.3', config, count=1)
path.write_text(config)
subprocess.run(['testparm', '-s'], check=True, capture_output=True)
directory = Path('/etc/systemd/resolved.conf.d')
directory.mkdir(exist_ok=True)
(directory / 'm0-directory.conf').write_text('[Resolve]\nDNS=172.31.83.10\nDomains=~m0.smolofis.test\n')
subprocess.run(['systemctl', 'restart', 'systemd-resolved'], check=True)
subprocess.run(['smbcontrol', 'all', 'reload-config'], check=True, capture_output=True)
for args in (['dig', '+short', '_ldap._tcp.m0.smolofis.test', 'SRV'], ['samba_dnsupdate', '--verbose']):
    result = subprocess.run(args, capture_output=True, timeout=120)
    if result.returncode or not result.stdout:
        raise SystemExit('FAIL: lab DNS registration or lookup; inspect guest diagnostics')
print('PASS: system resolver locates AD SRV records and samba_dnsupdate exits successfully')
