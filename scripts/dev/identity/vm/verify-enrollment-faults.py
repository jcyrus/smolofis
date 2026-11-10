"""Owned full-kernel VM: real LDAPS enrollment under sync faults and process death."""
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import subprocess
from receipt_fault_tools import environment, traced

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
BINARY = Path('/opt/smolofis-receipt-journal-lab/smolofis-panel')
if (os.geteuid() != 0 or platform.machine() != 'x86_64'
        or not Path('/etc/smolofis-m0-fullvm').exists() or not (ROOT / 'domain-ready').exists()
        or Path('/etc/smolofis-release').exists() or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt', '--vm'], capture_output=True).returncode):
    raise SystemExit('Owned full-kernel x86 VM required')
os.umask(0o077)
prior = max(ROOT.glob('operation-receipts-*/report.json'), key=lambda p: p.stat().st_mtime)
template = json.loads(Path(json.loads(prior.read_text())['state']).read_text())
evidence = ROOT / ('enrollment-faults-' + secrets.token_hex(5)); evidence.mkdir(mode=0o700)
config = evidence / 'auth.json'
checks, cases = [], []
name, group = 'ef-' + secrets.token_hex(5), 'M1-Fault-' + secrets.token_hex(5)
password = 'Lab9!' + secrets.token_hex(18)
created_user = created_group = False

def passed(label):
    checks.append(label); print('PASS: ' + label, flush=True)

def directory_command(args):
    result = subprocess.run(args, capture_output=True, timeout=60)
    assert result.returncode == 0, 'Lab directory prerequisite failed; diagnostics suppressed'

def fixture(label):
    folder = evidence / label; folder.mkdir(mode=0o700)
    state, log = folder / 'state.json', folder / 'events.jsonl'
    record = dict(template, version=2, locked=True,
                  last_operation={'id': secrets.token_hex(32), 'operation': 'lockdown', 'outcome': 'applied'})
    state.write_text(json.dumps(record)); state.chmod(0o644)
    log.touch(mode=0o600)
    for path in (state, log, folder, evidence):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return state, log, state.read_bytes(), record['last_operation']['id']

def enroll(state, log, **kwargs):
    return traced(BINARY, state, log,
                  args=['--auth-enroll', str(config), str(state), name],
                  password=password.encode(), **kwargs)

def history(state, log):
    result = subprocess.run([str(BINARY), '--auth-operation-list'], env=environment(log),
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    return {r['id']: r['outcome'] for r in json.loads(result.stdout)['operations']}

def rows(log):
    content = log.read_text()
    assert password not in content and name not in content
    return [json.loads(line) for line in content.splitlines()]

try:
    directory_command(['samba-tool', 'user', 'create', name, password]); created_user = True
    directory_command(['samba-tool', 'group', 'add', group]); created_group = True
    directory_command(['samba-tool', 'group', 'addmembers', group, name])
    reader = evidence / 'reader-password'
    reader.write_text(json.loads((ROOT / 'passwords.json').read_text())['ldap-reader'])
    config.write_text(json.dumps({'origin': 'http://127.0.0.1:18093', 'directory': {
        'url': 'ldaps://127.0.0.1', 'realm': 'M0.SMOLOFIS.TEST',
        'base_dn': 'DC=m0,DC=smolofis,DC=test',
        'admin_group_dn': f'CN={group},CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_dn': 'CN=ldap-reader,CN=Users,DC=m0,DC=smolofis,DC=test',
        'reader_password_file': str(reader), 'ca_file': '/var/lib/samba/private/tls/ca.pem'}}))
    state, log, before, old = fixture('baseline')
    code, count, paths, output, error = enroll(state, log)
    assert code == 0 and count == 7, (code, count)
    assert paths[:4] == [str(state), str(state.parent), str(log), str(log)]
    assert Path(paths[4]).name.startswith('.panel-enrollment-')
    assert paths[5:] == [str(state.parent), str(log)]
    assert [r['event'] for r in rows(log)] == ['local_receipt_preserved', 'local_enrollment_authorized', 'local_enrollment_applied']
    assert not json.loads(state.read_text())['locked'] and history(state, log)[old] == 'applied'
    passed('real LDAPS baseline verifies seven sync points and checkpoint/authorization before unlocking')

    for phase in range(1, 8):
        state, log, before, old = fixture('eio-' + str(phase))
        code, count, paths, output, error = enroll(state, log, fail=phase)
        assert code != 0 and b' applied.\n' not in output and b'Enrollment saved.' not in output and b'os error 5' in error, (phase, code)
        current, events = json.loads(state.read_text()), rows(log)
        if phase <= 5:
            assert state.read_bytes() == before and current['locked']
        else:
            assert not current['locked'] and current['last_operation']['id'] != old
        if phase <= 2:
            assert not events
        elif phase == 3:
            assert [r['event'] for r in events] == ['local_receipt_preserved']
        elif phase == 4:
            assert [r['event'] for r in events] == ['local_receipt_preserved', 'local_enrollment_authorized']
            assert history(state, log)[events[-1]['operation_id']] == 'unknown'
        elif phase in (5, 6):
            assert events[-1]['event'] == 'local_operation_uncertain'
            assert b'enrollment outcome uncertain' in error
            assert history(state, log)[events[-1]['operation_id']] == 'unknown'
        else:
            assert events[-1]['event'] == 'local_enrollment_applied'
            assert b'applied to enrollment state' in error
        cases.append({'fault': 'EIO', 'phase': phase, 'sync_paths': paths,
                      'state_changed': state.read_bytes() != before})
        passed(f'enrollment sync EIO phase {phase} preserves locked state or reports the observed partial/uncertain commit')

    boundaries = [(p, False) for p in range(1, 8)] + [(3, True), (4, True), (6, True), (7, True)]
    for phase, after in boundaries:
        state, log, before, old = fixture(f'kill-{phase}-' + ('after' if after else 'before'))
        code, count, paths, output, error = enroll(state, log, kill=phase, after=after)
        assert code == -signal.SIGKILL and count == phase and b' applied.\n' not in output and b'Enrollment saved.' not in output
        current, events = json.loads(state.read_text()), rows(log)
        if phase <= 5:
            assert state.read_bytes() == before and current['locked']
        else:
            assert not current['locked'] and current['last_operation']['id'] != old
            assert history(state, log)[old] == 'applied'
        authorized = [r for r in events if r['event'] == 'local_enrollment_authorized']
        if authorized:
            applied = any(r['event'] == 'local_enrollment_applied' for r in events)
            assert history(state, log)[authorized[-1]['operation_id']] == ('applied' if applied else 'unknown')
        cases.append({'fault': 'SIGKILL', 'phase': phase, 'after_sync': after,
                      'sync_paths': paths, 'state_changed': state.read_bytes() != before})
        passed(f'enrollment process death {"after" if after else "before"} sync phase {phase} retains honest access/history evidence')
finally:
    # Always attempt all cleanup steps, including after a failing assertion.
    failures = []
    for args, created in ((['samba-tool', 'user', 'delete', name], created_user),
                          (['samba-tool', 'group', 'delete', group], created_group)):
        if created:
            result = subprocess.run(args, capture_output=True, timeout=60)
            if result.returncode:
                failures.append(args[1])
    if (evidence / 'reader-password').exists():
        (evidence / 'reader-password').unlink()
    assert not failures, 'Synthetic directory cleanup incomplete'
report = {'checks': checks, 'cases': cases,
          'binary_sha256': hashlib.file_digest(BINARY.open('rb'), 'sha256').hexdigest(),
          'real_verified_ldaps': True, 'synthetic_directory_accounts_removed': True,
          'enrollment_password_input': 'protected pipe; not enrollment argv or report',
          'injection': 'ptrace x86_64 fsync/fdatasync -EIO and process-only SIGKILL',
          'hardware_power_loss_tested': False, 'appliance_ab_rollback_tested': False,
          'production_modified': False}
(evidence / 'report.json').write_text(json.dumps(report, indent=2))
print(f'PASS: {len(checks)} enrollment fault/crash checks; evidence retained at {evidence}', flush=True)
