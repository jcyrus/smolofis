#!/usr/bin/env python3
"""Abruptly terminate ONLY the owned restored QEMU lab at an archival boundary."""
import importlib.util
import json
from pathlib import Path
import select
import subprocess
import time

spec = importlib.util.spec_from_file_location('lab', Path(__file__).with_name('lab.py'))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
lab.PORT = '12223'
name = 'smolofis-m0-fullvm-restored'
info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
assert info['Config'].get('Labels', {}).get('smolofis.lab') == 'm0-fullvm'
assert info['State']['Running']
assert any(m['Source'] == str(lab.STATE) and m['Destination'] == '/lab' for m in info['Mounts'])
assert 'file=/lab/restored.qcow2,if=virtio,format=qcow2' in info['Config']['Cmd']
with Path(__file__).with_name('verify-archive-sync.py').open('rb') as source:
    lab.run(lab.ssh_args()+['sudo -n tee /opt/smolofis-identity-lab/vm/verify-archive-sync.py >/dev/null'], stdin=source)
process = subprocess.Popen(lab.ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-archive-sync.py power'], stdout=subprocess.PIPE)
try:
    assert select.select([process.stdout], [], [], 45)[0], 'No archival pause acknowledgement; VM left running'
    ready = json.loads(process.stdout.readline())
    directory = Path(ready['directory'])
    assert directory.parent == Path('/var/lib/smolofis-identity-machine-lab') and directory.name.startswith('archive-power-')
    assert ready['pause'] == 'after rename, before parent directory fsync'
    evidence = lab.STATE / (directory.name + '-host.json')
    evidence.write_text(json.dumps(ready, indent=2))
    print('Armed boundary confirmed: ' + str(directory), flush=True)
    try:
        lab.run(['docker', 'kill', '--signal', 'KILL', name])
    finally:
        # Always restart the owned guest, even if kill returns an ambiguous error.
        lab.run(['docker', 'start', name])
    process.wait(timeout=20)
    deadline = time.monotonic() + 420
    while True:
        result = subprocess.run(lab.ssh_args()+['true'], capture_output=True, timeout=12)
        if result.returncode == 0:
            break
        assert time.monotonic() < deadline, 'Guest startup timed out; evidence retained'
        time.sleep(3)
    lab.run(lab.ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-archive-sync.py recover ' + str(directory)])
    ready['recovery_verified'] = True
    ready['shutdown'] = 'Docker SIGKILL of QEMU, no guest shutdown'
    evidence.write_text(json.dumps(ready, indent=2))
finally:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
