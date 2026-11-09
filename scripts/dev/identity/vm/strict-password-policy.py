"""Apply the explicit immediate-reset candidate policy to the owned lab DC."""
import os
from pathlib import Path
import re
import subprocess

if os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists() or not Path('/var/lib/smolofis-identity-machine-lab/domain-ready').exists():
    raise SystemExit('Refusing policy change outside the owned VM')
path = Path('/etc/samba/smb.conf')
config = path.read_text()
pattern = r'(?im)^\s*old password allowed period\s*=.*$'
if re.search(pattern, config):
    config = re.sub(pattern, '\told password allowed period = 0', config)
else:
    config = re.sub(r'(?im)^\[global\]\s*$', '[global]\n\told password allowed period = 0', config, count=1)
if config == path.read_text():
    print('Candidate policy already configured')
else:
    path.with_suffix('.m0-before-strict').write_text(path.read_text())
    path.write_text(config)
    subprocess.run(['testparm', '-s'], check=True, capture_output=True)
    subprocess.run(['systemctl', 'restart', 'samba-ad-dc'], check=True)
effective = subprocess.check_output(['testparm', '-s', '--parameter-name=old password allowed period'], stderr=subprocess.DEVNULL, text=True).strip()
if effective != '0':
    raise SystemExit('FAIL: immediate-reset lab policy not effective')
print('PASS: lab old-password grace period is explicitly zero')
