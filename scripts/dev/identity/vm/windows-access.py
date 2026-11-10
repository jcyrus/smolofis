#!/usr/bin/env python3
"""Temporary loopback-only SSH relay for Windows 11 24H2 SMB acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

spec = importlib.util.spec_from_file_location('lab', Path(__file__).with_name('lab.py'))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
lab.PORT = '12223'
RELAY = 'smolofis@192.168.1.17'
STATE = lab.STATE / 'windows-access.json'
LAB_CONTROL = lab.STATE / 'windows-lab.sock'
RELAY_CONTROL = lab.STATE / 'windows-relay.sock'

def command(arguments, check=True):
    return subprocess.run(arguments, check=check, capture_output=True, timeout=20)

def stop():
    if RELAY_CONTROL.exists():
        # Stop the optional standard-port proxy before closing its backend.
        command(['ssh', '-S', str(RELAY_CONTROL), RELAY,
                 'sudo -n /bin/sh -c "if test -f /run/smolofis-m0-windows-relay/stop.sh; then /bin/sh /run/smolofis-m0-windows-relay/stop.sh; fi"'], check=False)
    for control, destination in ((RELAY_CONTROL, RELAY), (LAB_CONTROL, lab.ssh_args()[-1])):
        if control.exists():
            command(['ssh', '-S', str(control), '-O', 'exit', destination], check=False)
    if STATE.exists():
        STATE.unlink()

action = sys.argv[1] if len(sys.argv) > 1 else 'status'
if action == 'expire':
    session = sys.argv[2]
    time.sleep(7200)
    if STATE.exists() and json.loads(STATE.read_text())['session'] == session:
        stop()
elif action == 'stop':
    stop()
    print('Stopped task-owned Windows lab SSH relays.')
elif action == 'status':
    for control, destination in ((LAB_CONTROL, lab.ssh_args()[-1]), (RELAY_CONTROL, RELAY)):
        result = command(['ssh', '-S', str(control), '-O', 'check', destination], check=False)
        print(control.name + ': ' + ('active' if result.returncode == 0 else 'inactive'))
    if STATE.exists():
        print(STATE.read_text())
elif action == 'start':
    if STATE.exists() or LAB_CONTROL.exists() or RELAY_CONTROL.exists():
        raise SystemExit('Existing task relay state; inspect status or stop it before starting again.')
    command(lab.ssh_args()+['sudo -n test -f /etc/smolofis-m0-fullvm'])
    started = False
    try:
        command(lab.ssh_args()[:-1]+['-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', '-M', '-S', str(LAB_CONTROL), '-fNT',
            '-L', '127.0.0.1:11445:127.0.0.1:1445', lab.ssh_args()[-1]])
        started = True
        command(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=8', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', '-M', '-S', str(RELAY_CONTROL), '-fNT',
            '-R', '127.0.0.1:11445:127.0.0.1:11445', RELAY])
        record = {'session': secrets.token_hex(8), 'started_epoch': int(time.time()),
                  'expires_epoch': int(time.time())+7200, 'relay_bind': '127.0.0.1:11445',
                  'production_smb_installed': False}
        descriptor = os.open(STATE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(record, stream, indent=2)
        subprocess.Popen([sys.executable, __file__, 'expire', record['session']],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        print(json.dumps(record, indent=2))
    except BaseException:
        if started:
            stop()
        raise
else:
    raise SystemExit('usage: windows-access.py start|status|stop')
