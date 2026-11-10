#!/usr/bin/env python3
"""Opt-in real-tailnet trial, exclusively in the owned full-kernel lab VM."""
import hashlib
import grp
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
SOURCE = Path('/opt/smolofis-private-https-lab')
BINARY = SOURCE / 'smolofis-panel'
STATE = ROOT / 'private-https-trial.json'
BACKEND = 'smolofis-console-preview.service'
HTTPS = 'smolofis-console-https-preview.service'
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT / 'domain-ready').exists() or Path('/etc/smolofis-release').exists()
        or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel lab VM required')

def required(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=60, **kwargs)
    if result.returncode:
        raise SystemExit('Lab command failed: ' + args[0] + '; credential-bearing diagnostics suppressed')
    return result.stdout

def status():
    node = json.loads(required(['tailscale', 'status', '--json']))
    own = node.get('Self') or {}
    print(json.dumps({'state': node.get('BackendState'), 'online': own.get('Online'),
                      'dns_name': own.get('DNSName'), 'cert_domains': node.get('CertDomains')}, indent=2))
    print(required(['tailscale', 'serve', 'status', '--json']).decode())

action = sys.argv[1] if len(sys.argv) > 1 else 'status'
if action == 'status':
    status()
    if STATE.exists():
        record = json.loads(STATE.read_text())
        print(json.dumps({key: record[key] for key in ('origin', 'runtime', 'evidence', 'backend_user')}, indent=2))
elif action == 'start-https':
    assert STATE.exists(), 'Prepare the enrolled loopback backend first'
    record = json.loads(STATE.read_text())
    required(['systemctl', 'start', HTTPS])
    for _ in range(30):
        serve = json.loads(required(['tailscale', 'serve', 'status', '--json']))
        if serve.get('Foreground'):
            break
        if required(['systemctl', 'show', HTTPS, '-p', 'ActiveState', '--value']).strip() == b'failed':
            raise SystemExit('HTTPS unit failed; inspect its private journal')
        time.sleep(1)
    else:
        raise SystemExit('Foreground Serve session did not become visible')
    record['https_started'] = True
    record['scope'] = 'User-confirmed personal tailnet; employee network-denial acceptance remains pending'
    STATE.write_text(json.dumps(record, indent=2))
    (Path(record['evidence']) / 'serve-started.json').write_text(json.dumps(serve, indent=2))
    print('PASS: temporary foreground Serve session is active; mobile/TLS acceptance still pending')
    print(record['origin'] + '/console')
elif action == 'stop':
    assert STATE.exists(), 'No owned trial state'
    record = json.loads(STATE.read_text())
    runtime = Path(record['runtime']); evidence = Path(record['evidence'])
    assert runtime.parent == Path('/run') and runtime.name.startswith('smolofis-private-https-')
    if (runtime / 'panel-enrollment.json').exists():
        environment = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}
        environment['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(runtime / 'local.jsonl')
        required([str(BINARY), '--auth-lock', str(runtime / 'panel-enrollment.json')], env=environment)
    for unit in (HTTPS, BACKEND):
        subprocess.run(['systemctl', 'stop', unit], capture_output=True, timeout=30)
        result = subprocess.run(['journalctl', '-u', unit, '--no-pager', '-n', '200'], capture_output=True)
        (evidence / (unit + '.journal')).write_bytes(result.stdout)
    for name in ('local.jsonl', 'http.jsonl', 'panel-enrollment.json'):
        if (runtime / name).exists():
            shutil.copyfile(runtime / name, evidence / name)
    serve = json.loads(required(['tailscale', 'serve', 'status', '--json']))
    assert not serve or all(value is None or value == {} for value in serve.values()), 'Foreground Serve cleanup incomplete; inspect without global reset'
    (evidence / 'serve-after-stop.json').write_text(json.dumps(serve, indent=2))
    for target in record['created_units']:
        target = Path(target)
        assert target.parent == Path('/run/systemd/system') and target.name in (BACKEND, HTTPS)
        dropin = Path(str(target) + '.d')
        owned = dropin / '10-owned-lab.conf'
        if owned.exists():
            assert str(runtime) in owned.read_text()
            owned.unlink(); dropin.rmdir()
        if target.exists(): target.unlink()
    required(['systemctl', 'daemon-reload'])
    for name in record['created_directory_users']:
        assert name in ('mobile-admin', 'mobile-user')
        required(['samba-tool', 'user', 'delete', name])
    if record['created_group']:
        assert record['group'] == 'M1-PrivateConsole'
        required(['samba-tool', 'group', 'delete', record['group']])
    if record['created_backend_user']:
        username = record['backend_user']; assert username.startswith('m1-https-')
        required(['userdel', username])
        try:
            grp.getgrnam(username)
        except KeyError:
            pass
        else:
            required(['groupdel', username])
    if runtime.exists():
        for path in runtime.iterdir():
            assert path.is_file() and not path.is_symlink()
            path.unlink()
        runtime.rmdir()
    if record['created_etc']:
        Path('/etc/smolofis').rmdir()
    record['stopped'] = True
    (evidence / 'trial-record.json').write_text(json.dumps(record, indent=2))
    STATE.unlink()
    print('PASS: foreground Serve removed, enrollment locked, owned backend/config/users stopped and cleaned; evidence retained')
elif action == 'prepare-backend':
    if STATE.exists():
        raise SystemExit('Existing lab trial; inspect before replacing')
    password = sys.stdin.read().rstrip('\r\n')
    assert 8 <= len(password) <= 1024, 'Supply a disposable lab password on protected stdin'
    node = json.loads(required(['tailscale', 'status', '--json']))
    own = node.get('Self') or {}
    host = own.get('DNSName', '').rstrip('.')
    assert node.get('BackendState') == 'Running' and own.get('Online') and host.startswith('smolofis-m0-private.') and host.endswith('.ts.net')
    for unit in (BACKEND, HTTPS):
        for base in ('/etc/systemd/system', '/run/systemd/system', '/usr/lib/systemd/system'):
            assert not (Path(base) / unit).exists() and not (Path(base) / (unit + '.d')).exists()
    for path in ('/etc/smolofis/panel-auth.json', '/etc/smolofis/panel-enrollment.json'):
        assert not Path(path).exists() and not Path(path).is_symlink()
    assert BINARY.is_file()
    os.umask(0o077)
    suffix = secrets.token_hex(4)
    evidence = ROOT / ('private-https-' + suffix)
    evidence.mkdir(mode=0o700)
    runtime = Path('/run') / ('smolofis-private-https-' + suffix)
    runtime.mkdir(mode=0o755); runtime.chmod(0o755)
    username = 'm1-https-' + suffix
    record = {'version': 1, 'origin': 'https://' + host, 'runtime': str(runtime),
              'evidence': str(evidence), 'backend_user': username,
              'admin': 'mobile-admin', 'nonadmin': 'mobile-user', 'group': 'M1-PrivateConsole',
              'created_units': [], 'created_directory_users': [], 'created_group': False,
              'created_backend_user': False, 'created_etc': False,
              'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest()}
    def save():
        STATE.write_text(json.dumps(record, indent=2)); STATE.chmod(0o600)
    save()  # Persist cleanup ownership before provisioning anything.
    for account in (record['admin'], record['nonadmin']):
        result = subprocess.run(['samba-tool', 'user', 'show', account], capture_output=True)
        assert result.returncode != 0, 'Existing lab account; inspect before reuse'
    result = subprocess.run(['samba-tool', 'group', 'show', record['group']], capture_output=True)
    assert result.returncode != 0, 'Existing group; inspect before reuse'
    required(['groupadd', '--system', username])
    required(['useradd', '--system', '--gid', username, '--no-create-home', '--shell', '/usr/sbin/nologin', username])
    record['created_backend_user'] = True; save()
    account = pwd.getpwnam(username)
    for name in (record['admin'], record['nonadmin']):
        required(['samba-tool', 'user', 'create', name, password])
        record['created_directory_users'].append(name); save()
    required(['samba-tool', 'group', 'add', record['group']])
    record['created_group'] = True; save()
    required(['samba-tool', 'group', 'addmembers', record['group'], record['admin']])
    passwords = json.loads((ROOT / 'passwords.json').read_text())
    reader = runtime / 'reader-password'
    reader.write_text(passwords['ldap-reader']); reader.chmod(0o640); os.chown(reader, 0, account.pw_gid)
    ca = runtime / 'ca.pem'
    shutil.copyfile('/var/lib/samba/private/tls/ca.pem', ca); ca.chmod(0o644)
    configuration = {'origin': record['origin'], 'directory': {
        'url': 'ldaps://127.0.0.1', 'realm': 'M0.SMOLOFIS.TEST',
        'base_dn': 'DC=m0,DC=smolofis,DC=test',
        'admin_group_dn': 'CN=' + record['group'] + ',CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_dn': 'CN=ldap-reader,CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_password_file': str(reader), 'ca_file': str(ca)}}
    config = runtime / 'panel-auth.json'
    config.write_text(json.dumps(configuration)); config.chmod(0o644)
    state = runtime / 'panel-enrollment.json'
    local_audit = runtime / 'local.jsonl'; local_audit.touch(mode=0o600)
    environment = {key: value for key, value in os.environ.items() if not key.startswith('SMOLOFIS_')}
    environment['SMOLOFIS_LOCAL_AUDIT_FILE'] = str(local_audit)
    required([str(BINARY), '--auth-enroll', str(config), str(state), record['admin']], input=password.encode(), env=environment)
    http_audit = runtime / 'http.jsonl'; http_audit.touch(mode=0o600); os.chown(http_audit, account.pw_uid, account.pw_gid)
    etc = Path('/etc/smolofis')
    if not etc.exists():
        etc.mkdir(mode=0o755); etc.chmod(0o755); record['created_etc'] = True; save()
    for unit in (BACKEND, HTTPS):
        target = Path('/run/systemd/system') / unit
        shutil.copyfile(SOURCE / unit, target); target.chmod(0o644)
        record['created_units'].append(str(target)); save()
        dropin = Path(str(target) + '.d'); dropin.mkdir(mode=0o755); dropin.chmod(0o755)
        common = f'[Service]\nBindReadOnlyPaths={runtime}:/etc/smolofis\n'
        if unit == BACKEND:
            text = common + (f'ExecStart=\nExecStart={BINARY} --private-console\nUser={username}\nGroup={username}\nRestart=no\n'
                             f'Environment=SMOLOFIS_AUDIT_FILE={http_audit}\nReadWritePaths={http_audit}\n')
        else:
            text = common + (f'ExecStartPre=\nExecStartPre={BINARY} --console-access-check /etc/smolofis/panel-auth.json /etc/smolofis/panel-enrollment.json\n')
        (dropin / '10-owned-lab.conf').write_text(text)
    required(['systemctl', 'daemon-reload'])
    required(['systemd-analyze', 'verify', '/run/systemd/system/' + BACKEND, '/run/systemd/system/' + HTTPS])
    required(['systemctl', 'start', BACKEND])
    required([str(BINARY), '--console-access-check', str(config), str(state)])
    (evidence / 'preflight.json').write_text(json.dumps({'passed': True, 'origin': record['origin'],
             'binary_sha256': record['binary_sha256'], 'serve_started': False}, indent=2))
    print('PASS: enrolled unprivileged loopback backend and real-node preflight; HTTPS listener NOT started')
    print(json.dumps({'origin': record['origin'], 'admin': record['admin'], 'nonadmin': record['nonadmin'],
                      'evidence': str(evidence)}, indent=2))
else:
    raise SystemExit('usage: private-https-trial.py status|prepare-backend|start-https|stop')
