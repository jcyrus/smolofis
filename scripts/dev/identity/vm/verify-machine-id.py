"""Exercise the init-bottom script against private Linux mount fixtures."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
SCRIPT = Path('/opt/smolofis-identity-lab/vm/smolofis-machine-id')
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel lab VM required')
if len(sys.argv) == 1:
    subprocess.run(['unshare', '--mount', '--fork', sys.executable, __file__, '--inside'], check=True, timeout=60)
    raise SystemExit(0)
assert sys.argv[1:] == ['--inside']
subprocess.run(['mount', '--make-rprivate', '/'], check=True)
os.umask(0o022)
directory = ROOT / ('machine-id-' + secrets.token_hex(5))
directory.mkdir(mode=0o700)
checks = []

def passed(label):
    checks.append(label)
    print('PASS: ' + label, flush=True)

def fixture(name, persistent=True):
    root = directory / name
    (root / 'etc/smolofis').mkdir(mode=0o755, parents=True)
    (root / 'etc/machine-id').write_bytes(b'')
    if persistent:
        subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=1m,mode=0755,nodev,nosuid', 'tmpfs', str(root / 'etc/smolofis')], check=True)
    return root

def run(root, allowed=True):
    result = subprocess.run(['sh', str(SCRIPT)], env=dict(os.environ, rootmnt=str(root)), capture_output=True, timeout=15)
    assert (result.returncode == 0) == allowed, result.stderr
    return result

root = fixture('persistent')
run(root)
saved = root / 'etc/smolofis/identity/machine-id'
value = saved.read_bytes()
assert len(value) == 33 and value[-1:] == b'\n' and int(value.strip(), 16) > 0
assert (root / 'etc/machine-id').read_bytes() == value
assert (root / 'etc/machine-id').stat().st_mode & 0o777 == 0o644
passed('persistent first boot initializes a unique protected ID before systemd')
(root / 'etc/machine-id').write_bytes(b'')
run(root)
assert saved.read_bytes() == value and (root / 'etc/machine-id').read_bytes() == value
passed('later boot restores the exact ID after the volatile root is replaced')
other = fixture('other-device')
run(other)
assert (other / 'etc/machine-id').read_bytes() != value
passed('another persistent device receives a different ID')
volatile = fixture('live-usb', persistent=False)
run(volatile)
assert not (volatile / 'etc/smolofis/identity').exists()
assert (volatile / 'etc/machine-id').read_bytes() == b''
passed('nonpersistent live root is left to normal systemd initialization')
for invalid in (b'broken\n', b'0' * 32 + b'\n', b'a' * 64):
    saved.write_bytes(invalid)
    before = (root / 'etc/machine-id').read_bytes()
    run(root, allowed=False)
    assert saved.read_bytes() == invalid and (root / 'etc/machine-id').read_bytes() == before
saved.write_bytes(value)
passed('corrupt zero or oversized durable IDs are refused without reinitialization')
target = saved.parent / 'target'
target.write_bytes(value)
saved.unlink()
saved.symlink_to(target)
run(root, allowed=False)
assert target.read_bytes() == value
saved.unlink()
os.link(target, saved)
run(root, allowed=False)
saved.unlink()
saved.write_bytes(value)
identity = saved.parent
identity.chmod(0o777)
run(root, allowed=False)
identity.chmod(0o700)
passed('unsafe symbolic links hard links and writable identity directories are refused')
runtime = root / 'etc/machine-id'
runtime.unlink()
runtime.symlink_to(target)
run(root, allowed=False)
assert target.read_bytes() == value
runtime.unlink()
runtime.write_bytes(b'')
run(root)
assert runtime.read_bytes() == value
passed('runtime symlink substitution is refused and a regular runtime file recovers')
# Build an actual initramfs using a private copy of its configuration. This
# never replaces /boot or the guest's running boot configuration.
import shutil
configuration = directory / 'initramfs-tools'
shutil.copytree('/etc/initramfs-tools', configuration)
(configuration / 'scripts/init-bottom').mkdir(parents=True, exist_ok=True)
(configuration / 'hooks').mkdir(parents=True, exist_ok=True)
shutil.copyfile(SCRIPT, configuration / 'scripts/init-bottom/smolofis-machine-id')
shutil.copyfile(SCRIPT.with_name('smolofis-machine-id-tools'), configuration / 'hooks/smolofis-machine-id-tools')
for path in (configuration / 'scripts/init-bottom/smolofis-machine-id', configuration / 'hooks/smolofis-machine-id-tools'):
    path.chmod(0o755)
image = directory / 'test-initrd.img'
with (directory / 'initramfs-build.log').open('wb') as output:
    subprocess.run(['/usr/sbin/mkinitramfs', '-d', str(configuration), '-o', str(image), os.uname().release],
                   stdout=output, stderr=subprocess.STDOUT, check=True, timeout=240)
contents = subprocess.check_output(['lsinitramfs', str(image)], text=True).splitlines()
assert 'scripts/init-bottom/smolofis-machine-id' in contents
for tool in ('stat', 'cat', 'tr', 'mktemp', 'sync', 'mv', 'mkdir', 'chmod'):
    assert any(path.endswith('/' + tool) for path in contents), tool
passed('real Linux initramfs includes the early-boot script and every explicit tool dependency')
# Reproduce mkfs -d copying the non-root runner UID into the smoke disk,
# then run the smoke script's actual offline ownership normalization block.
source_tree = directory / 'smoke-source'
paths = ['var/lib/docker', 'var/lib/containerd', 'var/lib/smolofis', 'var/lib/tailscale', 'etc/smolofis', 'home']
for path in paths:
    (source_tree / path).mkdir(parents=True, exist_ok=True)
(source_tree / 'persistence.conf').write_text('/etc/smolofis bind\n')
for path in [source_tree, *source_tree.rglob('*')]:
    os.chown(path, 65534, 65534)
scratch = directory / 'smoke.raw'
with scratch.open('wb') as stream:
    stream.truncate(32 * 1024 * 1024)
subprocess.run(['mkfs.ext4', '-F', '-q', '-E', 'root_owner=0:0', '-d', str(source_tree), str(scratch)], check=True)
before = subprocess.check_output(['debugfs', '-R', 'stat /etc/smolofis', str(scratch)], stderr=subprocess.DEVNULL, text=True)
assert '65534' in before
smoke = SCRIPT.with_name('smoke-test-iso.sh').read_text()
start = smoke.index('    # mkfs -d preserves')
end = smoke.index('    EXTRA_QEMU_ARGS', start)
block = 'set -euo pipefail\ndie() { echo "$*" >&2; exit 1; }\n' + smoke[start:end]
subprocess.run(['bash', '-c', block], check=True,
               env=dict(os.environ, WORK=str(directory), SCRATCH_DISK=str(scratch)), timeout=30)
passed('smoke persistence image repairs and verifies copied runner ownership without host chown or sudo')
report = {'checks': checks, 'script_sha256': hashlib.file_digest(SCRIPT.open('rb'), 'sha256').hexdigest(),
          'production_modified': False, 'actual_initramfs_boot_tested': False}
(directory / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} native machine-ID checks; evidence retained at {directory}', flush=True)
