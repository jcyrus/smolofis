#!/usr/bin/env python3
"""Prepare an offline native AD data tree. No service, DNS or host configuration changes."""
import argparse
import contextlib
import fcntl
import gc
import hashlib
import inspect
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
ROOT = Path('/var/lib/smolofis')
BASE = ROOT / 'directory'
PLAN = Path('/etc/smolofis/directory-plan.json')
STATE = BASE / 'bootstrap-state.json'
LOCK = ROOT / '.directory-bootstrap.lock'
PRIVATE_NETWORKS = tuple(ipaddress.IPv4Network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_plan(plan):
    require(isinstance(plan, dict) and set(plan) == {'version', 'dns_domain', 'short_domain', 'hostname', 'address', 'forwarder'}, 'Plan fields must be explicit')
    require(type(plan['version']) is int and plan['version'] == 1, 'Unsupported plan version')
    domain, short, host = plan['dns_domain'], plan['short_domain'], plan['hostname']
    require(isinstance(domain, str) and 1 < len(domain) <= 253 and domain == domain.lower(), 'Invalid DNS domain')
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    require(len(domain.split('.')) >= 2 and all(re.fullmatch(label, value) for value in domain.split('.')) and not domain.endswith('.local'), 'Use an explicit DNS domain separate from mDNS')
    require(isinstance(short, str) and re.fullmatch(r'[A-Z][A-Z0-9-]{0,14}', short) and not short.endswith('-'), 'Invalid short domain')
    require(isinstance(host, str) and len(host) <= 15 and re.fullmatch(label, host) and host.upper() != short, 'DC name must differ from the short domain')
    require(isinstance(plan['address'], str), 'IPv4 address required')
    address = ipaddress.IPv4Address(plan['address'])
    require(any(address in network for network in PRIVATE_NETWORKS), 'Private unicast IPv4 address required')
    forwarder = plan['forwarder']
    if forwarder is not None:
        require(isinstance(forwarder, str), 'Invalid DNS forwarder')
        ip = ipaddress.IPv4Address(forwarder)
        require(not ip.is_unspecified and not ip.is_multicast and not ip.is_loopback and not ip.is_link_local and ip != address, 'Invalid or self-referential DNS forwarder')
    return dict(plan)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON field')
        result[key] = value
    return result


def trusted(path, directory=False, private=False):
    require(path.is_absolute(), 'Absolute trusted path required')
    for current in (path, *path.parents):
        info = current.lstat()
        require((stat.S_ISDIR(info.st_mode) if current != path or directory else stat.S_ISREG(info.st_mode))
                and info.st_uid == 0 and info.st_mode & 0o022 == 0, 'Untrusted path')
        if current == path and not directory:
            require(info.st_nlink == 1, 'Linked control file')
    if private:
        require(stat.S_IMODE(path.stat().st_mode) == (0o700 if directory else 0o600), 'Owner-only path required')


def read_json(path, limit=65536, private=False):
    trusted(path, private=private)
    with path.open('rb') as stream:
        contents = stream.read(limit + 1)
    require(len(contents) <= limit, 'Control file too large')
    return json.loads(contents, object_pairs_hook=unique_object)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def exclusive(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def save_state(value):
    path = BASE / ('.bootstrap-state-' + secrets.token_hex(8))
    exclusive(path, (json.dumps(value, sort_keys=True) + '\n').encode())
    os.replace(path, STATE)
    sync_directory(BASE)


def machine_digest():
    path = Path('/etc/machine-id')
    trusted(path)
    with path.open() as stream:
        value = stream.read(130).strip()
    require(re.fullmatch('[a-f0-9]{32}', value) and value != '0' * 32, 'Initialized machine identity required')
    return hashlib.sha256(value.encode()).hexdigest()


def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@contextlib.contextmanager
def lock(create):
    if create:
        try:
            exclusive(LOCK, b'')
        except FileExistsError:
            pass
    trusted(LOCK, private=True)
    descriptor = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        require(info.st_ino == LOCK.stat().st_ino and info.st_nlink == 1, 'Lock changed while opening')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def private_log():
    path = BASE / 'provision.log'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    saved = (os.dup(1), os.dup(2))
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.fsync(descriptor)
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for value in (*saved, descriptor):
            os.close(value)


def samba_api():
    from samba.auth import system_session
    from samba.provision import provision
    import samba
    require(samba.version.startswith('4.22.'), 'This candidate requires the validated Samba 4.22 API')
    required = {'targetdir', 'adminpass', 'skip_sysvolacl', 'use_rfc2307', 'dns_backend'}
    require(required <= set(inspect.signature(provision).parameters), 'Unsupported Samba API')
    return provision, system_session


def provision_domain(plan, password, api):
    import ldb
    from samba.param import LoadParm
    provision, system_session = api
    logger = logging.getLogger('smolofis.directory.bootstrap')
    logger.addHandler(logging.StreamHandler(sys.stderr))
    logger.setLevel(logging.INFO)
    lp = LoadParm()
    lp.set('interfaces', 'lo ' + plan['address'] + '/32')
    lp.set('bind interfaces only', 'yes')
    lp.set('old password allowed period', '0')
    result = provision(logger, system_session(), targetdir=str(BASE / 'domain'),
                       realm=plan['dns_domain'].upper(), domain=plan['short_domain'],
                       hostname=plan['hostname'], hostip=plan['address'],
                       adminpass=password, serverrole='active directory domain controller',
                       dns_backend='SAMBA_INTERNAL', dns_forwarder=plan['forwarder'],
                       use_rfc2307=True, skip_sysvolacl=False, lp=lp)
    require(result.lp.get('bind interfaces only') and result.lp.get('old password allowed period') == 0, 'Unsafe provisioned service policy')
    require(set(result.lp.get('interfaces')) == {'lo', plan['address'] + '/32'}, 'Unexpected directory interfaces')
    entries = result.samdb.search(base=result.samdb.domain_dn(), scope=ldb.SCOPE_BASE, attrs=['objectGUID'])
    require(len(entries) == 1, 'Provisioned domain identity missing')
    return {'domain_sid': str(result.samdb.get_domain_sid()),
            'domain_guid': str(uuid.UUID(bytes_le=bytes(entries[0]['objectGUID'][0])))}


def sync_tree():
    for path in sorted(BASE.rglob('*')):
        if path.is_symlink():
            require(path.resolve().is_relative_to(BASE), 'Provisioned path escapes its data tree')
        elif path.is_file():
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            require(path.is_dir(), 'Unexpected provisioned file type')
    for path in sorted((path for path in BASE.rglob('*') if path.is_dir() and not path.is_symlink()), key=lambda value: len(value.parts), reverse=True):
        sync_directory(path)
    sync_directory(BASE)
    sync_directory(ROOT)


def status_unlocked(plan):
    trusted(BASE, directory=True, private=True)
    state = read_json(STATE, limit=4096, private=True)
    require(set(state) <= {'schema', 'operation_id', 'machine_digest', 'plan_digest', 'phase', 'domain_sid', 'domain_guid'}
            and type(state.get('schema')) is int and state.get('schema') == 1 and re.fullmatch('[a-f0-9]{64}', state.get('operation_id', ''))
            and state.get('phase') in ('preparing', 'failed', 'prepared'), 'Invalid bootstrap state')
    require(state['machine_digest'] == machine_digest(), 'Bootstrap belongs to a different appliance')
    require(state['plan_digest'] == plan_digest(plan), 'Bootstrap plan differs; inspect existing data before continuing')
    if state['phase'] == 'prepared':
        require(re.fullmatch(r'S-1-5-21-[0-9]+-[0-9]+-[0-9]+', state.get('domain_sid', '')), 'Invalid prepared domain SID')
        uuid.UUID(state['domain_guid'])
        trusted(BASE / 'bootstrap-admin.secret', private=True)
        for name in ('domain/private/sam.ldb', 'domain/etc/smb.conf', 'bootstrap-admin.secret'):
            path = BASE / name
            require(path.is_file() and not path.is_symlink() and path.stat().st_size > 0, 'Prepared artifact missing')
    return state


def prepare(plan):
    api = samba_api()  # Package/API and device validation precede persistent writes.
    device = machine_digest()
    with lock(create=True):
        if BASE.exists() or BASE.is_symlink():
            state = status_unlocked(plan)
            require(state['phase'] == 'prepared', 'Incomplete bootstrap retained; automatic replay is refused')
            return state
        BASE.mkdir(mode=0o700)
        sync_directory(ROOT)
        state = {'schema': 1, 'operation_id': secrets.token_hex(32), 'machine_digest': device,
                 'plan_digest': plan_digest(plan), 'phase': 'preparing'}
        save_state(state)
        try:
            password = secrets.token_urlsafe(32) + 'Aa1!'
            exclusive(BASE / 'bootstrap-admin.secret', password.encode() + b'\n')
            with private_log():
                identity = provision_domain(plan, password, api)
                del password
                gc.collect()
            sync_tree()
            state.update(identity, phase='prepared')
            save_state(state)
        except Exception:
            state['phase'] = 'failed'
            save_state(state)
            raise RuntimeError('Directory preparation failed; protected data retained, automatic retry refused') from None
        return state


def summary(state):
    return {'schema': 1, 'operation_id': state['operation_id'],
            'phase': 'interrupted' if state['phase'] == 'preparing' else state['phase'],
            'service_started_by_command': False, 'dns_changed_by_command': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'status'))
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Local root access required')
    os.umask(0o077)
    trusted(Path('/etc/smolofis-release'))
    trusted(ROOT, directory=True)
    filesystem = subprocess.run(['/usr/bin/findmnt', '-n', '-o', 'FSTYPE', '--mountpoint', str(ROOT)], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    require(filesystem in ('ext4', 'xfs', 'btrfs'), 'Validated native ACL-capable data filesystem required')
    plan = validate_plan(read_json(PLAN))
    if args.command == 'prepare':
        state = prepare(plan)
    elif not BASE.exists() and not BASE.is_symlink():
        print(json.dumps({'schema': 1, 'phase': 'absent', 'service_started_by_command': False, 'dns_changed_by_command': False}))
        return 1
    else:
        with lock(create=False):
            state = status_unlocked(plan)
    print(json.dumps(summary(state)))
    return 0 if state['phase'] == 'prepared' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Directory bootstrap refused or incomplete; inspect protected setup state and prerequisites.', file=sys.stderr)
        raise SystemExit(1)
