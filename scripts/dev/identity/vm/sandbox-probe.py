"""Runs as ExecStartPost under the actual candidate unit's hardening."""
import errno
import json
import os
from pathlib import Path
import socket
import sys

mode, outside, home, writable = sys.argv[1:]
status = Path('/proc/self/status').read_text().splitlines()
fields = dict(line.split(':', 1) for line in status if ':' in line)
assert fields['NoNewPrivs'].strip() == '1'
assert int(fields['CapEff'].strip(), 16) == 0
try:
    descriptor = os.open(outside, os.O_WRONLY | os.O_APPEND)
except OSError as error:
    assert error.errno == errno.EROFS
else:
    os.close(descriptor)
    mounts = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        if outside == fields[4] or outside.startswith(fields[4].rstrip('/') + '/'):
            mounts.append({'target': fields[4], 'options': fields[5]})
    print(json.dumps({'outside_path_mounts': mounts}), flush=True)
    raise SystemExit('FAIL: write outside allowlist was possible')
try:
    Path(home).read_bytes()
except OSError as error:
    assert error.errno in (errno.EACCES, errno.ENOENT)
else:
    raise SystemExit('FAIL: synthetic home file was visible')
if mode == 'backend':
    assert os.getuid() != 0 and fields['Seccomp'].strip() == '2'
    assert '0' not in fields['Groups'].split()
    descriptor = os.open(writable, os.O_WRONLY | os.O_APPEND)
    os.close(descriptor)
elif mode == 'worker':
    assert os.getuid() == 0
    with (Path(writable) / 'sandbox-probe').open('xb') as stream:
        stream.write(b'owned worker runtime only\n')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
        pass
    try:
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError as error:
        assert error.errno in (errno.EAFNOSUPPORT, errno.EPERM)
    else:
        connection.close()
        raise SystemExit('FAIL: worker could create an IPv4 socket')
else:
    raise SystemExit('Unknown sandbox probe mode')
print(json.dumps({'sandbox_probe': mode, 'uid': os.getuid(), 'no_new_privileges': True,
    'effective_capabilities': 0, 'outside_allowlist_readonly': True, 'home_hidden': True}), flush=True)
