#!/usr/bin/env python3
"""Full-kernel x86 M0 lab. Only task-owned files and loopback SSH are exposed."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[4]
STATE = REPO / 'work/identity-m0-vm'
IMAGE = 'smolofis-m0-qemu:local'
NAME = 'smolofis-m0-fullvm'
PORT = '12222'
FIREWALL = '''flush ruleset
table inet m0_guard {
 chain input {
  type filter hook input priority 0; policy drop;
  iifname "lo" accept
  ct state established,related accept
  iifname "lab0" ip saddr 172.31.83.0/24 accept
  iifname "br-*" ip saddr 10.85.0.0/24 accept
  udp dport 68 accept
  tcp dport 22 accept
 }
 chain output {
  type filter hook output priority 0; policy accept;
  oifname "lo" accept
  ct state established,related accept
  oifname "lab0" ip daddr 172.31.83.0/24 accept
  ip daddr { 10.0.2.2, 10.0.2.3 } udp dport { 53, 67 } accept
  ip daddr { 10.0.2.2, 10.0.2.3 } tcp dport 53 accept
  oifname "br-*" ip daddr 10.85.0.0/24 accept
  meta nfproto ipv6 reject
  ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 100.64.0.0/10 } reject
 }
 chain forward {
  type filter hook forward priority -10; policy accept;
  ct state established,related accept
  oifname "br-*" ip daddr 10.85.0.0/24 accept
  meta nfproto ipv6 reject
  ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 100.64.0.0/10 } reject
 }
}
'''


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def docker_tool(*args):
    return ['docker', 'run', '--rm', '--network', 'none', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
            '--mount', f'type=bind,source={STATE},target=/lab', '--entrypoint', args[0], IMAGE, *args[1:]]


def ssh_args():
    return ['ssh', '-i', str(STATE / 'client-key'), '-p', PORT,
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={STATE / "known_hosts"}',
            'm0@127.0.0.1']


def prepare():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    entries = [line.split() for line in (STATE / 'SHA512SUMS').read_text().splitlines()]
    expected = next(parts[0] for parts in entries if parts[-1].lstrip('*').split('/')[-1] == 'debian-13-genericcloud-amd64.qcow2')
    digest = hashlib.file_digest((STATE / 'base.qcow2').open('rb'), 'sha512').hexdigest()
    if digest != expected:
        raise SystemExit('Debian cloud image checksum mismatch; refusing to boot')
    os.chmod(STATE / 'base.qcow2', 0o444)
    (STATE / 'image-evidence.json').write_text(json.dumps({'source':'https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2', 'sha512':digest, 'verification':'official HTTPS checksum; detached signature not verified'}, indent=2))
    for name in ('client-key', 'host-key'):
        if not (STATE / name).exists():
            run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', STATE / name])
    host_key = (STATE / 'host-key.pub').read_text().split()
    (STATE / 'known_hosts').write_text(''.join(
        f'[127.0.0.1]:{port} {host_key[0]} {host_key[1]}\n' for port in ('12222', '12223')))
    data = {
        'hostname': 'dc-m0', 'manage_etc_hosts': True,
        'users': [{'name':'m0', 'shell':'/bin/bash', 'sudo':'ALL=(ALL) NOPASSWD:ALL',
                   'ssh_authorized_keys':[(STATE / 'client-key.pub').read_text().strip()]}],
        'ssh_pwauth':False, 'disable_root':True,
        'ssh_keys': {'ed25519_private':(STATE / 'host-key').read_text(), 'ed25519_public':(STATE / 'host-key.pub').read_text().strip()},
        'package_update':True, 'packages':['nftables', 'ca-certificates', 'python3', 'curl'],
        'write_files':[
            {'path':'/etc/smolofis-m0-fullvm', 'permissions':'0600', 'content':'isolated full-kernel identity lab v1\n'},
            {'path':'/etc/nftables.conf', 'permissions':'0600', 'content':FIREWALL},
            {'path':'/usr/sbin/policy-rc.d', 'permissions':'0755', 'content':'#!/bin/sh\nexit 101\n'}],
        'runcmd':[['systemctl','enable','--now','nftables'], ['touch','/var/lib/m0-bootstrap-ready']]
    }
    network = {'version':2,'ethernets':{
        'wan':{'match':{'macaddress':'52:54:00:83:00:01'},'set-name':'wan0','dhcp4':True,'dhcp6':False},
        'lab':{'match':{'macaddress':'52:54:00:83:00:10'},'set-name':'lab0','addresses':['172.31.83.10/24'],'optional':True}}}
    for name, value in [('user-data',data), ('meta-data',{'instance-id':'smolofis-m0-fullvm-v1', 'local-hostname':'dc-m0'}), ('network-config',network)]:
        path = STATE / name
        path.write_text(('#cloud-config\n' if name == 'user-data' else '') + json.dumps(value, indent=2) + '\n')
        path.chmod(0o600)
    if not (STATE / 'seed.iso').exists():
        run(docker_tool('cloud-localds','--network-config=/lab/network-config','/lab/seed.iso','/lab/user-data','/lab/meta-data'))
    if not (STATE / 'dc.qcow2').exists():
        run(docker_tool('qemu-img','create','-f','qcow2','-F','qcow2','-b','/lab/base.qcow2','/lab/dc.qcow2','10G'))
    print('PASS: cloud image checksum verified; private keys and seed remain in ignored lab storage')


def up(disk='dc.qcow2'):
    prepare()
    existing = subprocess.run(['docker','inspect',NAME], capture_output=True, text=True)
    if existing.returncode == 0:
        info = json.loads(existing.stdout)[0]
        if info['Config'].get('Labels',{}).get('smolofis.lab') != 'm0-fullvm':
            raise SystemExit('Refusing to reuse a foreign container')
        if not info['State']['Running']:
            run(['docker','start',NAME])
        return
    run(['docker','run','--detach','--name',NAME,'--label','smolofis.lab=m0-fullvm',
         '--cap-drop=ALL','--security-opt=no-new-privileges','--user',f'{os.getuid()}:{os.getgid()}',
         '--memory','4g','--cpus','2','--pids-limit','256','--publish',f'127.0.0.1:{PORT}:2222',
         '--mount',f'type=bind,source={STATE},target=/lab',IMAGE,
         '-machine','q35','-accel','tcg,thread=multi','-cpu','max','-smp','2','-m','3072',
         '-display','none','-serial','file:/lab/console.log','-monitor','none',
         '-drive',f'file=/lab/{disk},if=virtio,format=qcow2',
         '-drive','file=/lab/seed.iso,media=cdrom,readonly=on',
         '-netdev','user,id=wan,ipv6=off,hostfwd=tcp:0.0.0.0:2222-:22',
         '-device','virtio-net-pci,netdev=wan,mac=52:54:00:83:00:01',
         '-netdev','socket,id=lab,listen=127.0.0.1:18300',
         '-device','virtio-net-pci,netdev=lab,mac=52:54:00:83:00:10'])


def main():
    global NAME, PORT
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if action == 'up':
        up()
    elif action == 'ssh':
        run(ssh_args() + sys.argv[2:])
    elif action == 'restored-ssh':
        PORT = '12223'
        run(ssh_args() + sys.argv[2:])
    elif action == 'check-machine-id':
        PORT = '12223'
        for source, name in (
            (Path(__file__).with_name('verify-machine-id.py'), 'verify-machine-id.py'),
            (REPO / 'config/initramfs/smolofis-machine-id', 'smolofis-machine-id'),
            (REPO / 'config/initramfs/smolofis-machine-id-tools', 'smolofis-machine-id-tools'),
            (REPO / 'scripts/smoke-test-iso.sh', 'smoke-test-iso.sh'),
        ):
            with source.open('rb') as stream:
                run(ssh_args()+[f'sudo -n tee /opt/smolofis-identity-lab/vm/{name} >/dev/null'], stdin=stream)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-machine-id.py'])
    elif action == 'check-audit-lifecycle-gate':
        PORT = '12223'
        for source, name in (
            (Path(__file__).with_name('verify-audit-workflow.py'), 'verify-audit-workflow.py'),
            (REPO / 'scripts/smolofis-admin.sh', 'smolofis-admin.sh'),
            (REPO / 'scripts/dev/identity/verify-audit-lifecycle.sh', 'verify-audit-lifecycle.sh'),
        ):
            with source.open('rb') as stream:
                run(ssh_args()+[f'sudo -n tee /opt/smolofis-identity-lab/vm/{name} >/dev/null'], stdin=stream)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-audit-workflow.py --lifecycle'])
    elif action == 'check-audit-workflow':
        PORT = '12223'
        for source, name in (
            (Path(__file__).with_name('verify-audit-workflow.py'), 'verify-audit-workflow.py'),
            (REPO / 'scripts/smolofis-admin.sh', 'smolofis-admin.sh'),
        ):
            with source.open('rb') as stream:
                run(ssh_args()+[f'sudo -n tee /opt/smolofis-identity-lab/vm/{name} >/dev/null'], stdin=stream)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-audit-workflow.py'])
    elif action == 'check-archive-sync':
        PORT = '12223'
        with Path(__file__).with_name('verify-archive-sync.py').open('rb') as source:
            run(ssh_args()+['sudo -n tee /opt/smolofis-identity-lab/vm/verify-archive-sync.py >/dev/null'], stdin=source)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-archive-sync.py sync'])
    elif action == 'check-audit-archive':
        PORT = '12223'
        with Path(__file__).with_name('verify-audit-archive.py').open('rb') as source:
            run(ssh_args()+['sudo -n tee /opt/smolofis-identity-lab/vm/verify-audit-archive.py >/dev/null'], stdin=source)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-audit-archive.py'])
    elif action == 'check-operation-history':
        PORT = '12223'
        with Path(__file__).with_name('verify-operation-history.py').open('rb') as source:
            run(ssh_args()+['sudo -n tee /opt/smolofis-identity-lab/vm/verify-operation-history.py >/dev/null'], stdin=source)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-operation-history.py'])
    elif action == 'check-operation-receipts':
        PORT = '12223'
        with Path(__file__).with_name('verify-operation-receipts.py').open('rb') as source:
            run(ssh_args()+['sudo -n tee /opt/smolofis-identity-lab/vm/verify-operation-receipts.py >/dev/null'], stdin=source)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-operation-receipts.py'])
    elif action == 'check-service-sandbox':
        PORT = '12223'
        files = (
            (REPO / 'config/systemd/smolofis-console-preview.service', 'smolofis-console-preview.service'),
            (REPO / 'config/systemd/smolofis-admin-worker.service', 'smolofis-admin-worker.service'),
            (REPO / 'config/systemd/smolofis-console-preview.service.d/audit.conf', 'audit.conf'),
            (Path(__file__).with_name('sandbox-probe.py'), 'sandbox-probe.py'),
            (Path(__file__).with_name('verify-service-sandbox.py'), 'verify-service-sandbox.py'),
        )
        for source, name in files:
            with source.open('rb') as stream:
                run(ssh_args()+[f'sudo -n tee /opt/smolofis-identity-lab/vm/{name} >/dev/null'], stdin=stream)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-service-sandbox.py'])
    elif action == 'check-web-audit':
        PORT = '12223'
        with Path(__file__).with_name('verify-web-audit.py').open('rb') as source:
            run(ssh_args()+['sudo -n tee /opt/smolofis-identity-lab/vm/verify-web-audit.py >/dev/null'], stdin=source)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-web-audit.py'])
    elif action == 'check-local-audit':
        PORT = '12223'
        binary = STATE / 'panel-audit'
        with binary.open('rb') as source:
            header = source.read(20)
        if header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\x3e\x00':
            raise SystemExit('Build the Linux x86_64 panel artifact before VM audit acceptance')
        run(ssh_args()+['sudo -n install -d -m 0755 /opt/smolofis-panel-audit-lab'])
        for source, destination in (
            (binary, '/opt/smolofis-panel-audit-lab/smolofis-panel'),
            (Path(__file__).with_name('verify-local-audit.py'), '/opt/smolofis-identity-lab/vm/verify-local-audit.py'),
        ):
            with source.open('rb') as stream:
                run(ssh_args()+[f'sudo -n tee {destination} >/dev/null'], stdin=stream)
        run(ssh_args()+['sudo -n chmod 0755 /opt/smolofis-panel-audit-lab/smolofis-panel'])
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/verify-local-audit.py'])
    elif action == 'restore':
        info = json.loads(subprocess.check_output(['docker', 'inspect', NAME]))[0]
        if info['State']['Running'] or info['Config'].get('Labels', {}).get('smolofis.lab') != 'm0-fullvm':
            raise SystemExit('Cold restore requires the owned original VM to be fully stopped')
        if (STATE / 'checkpoint.qcow2').exists() or (STATE / 'restored.qcow2').exists():
            raise SystemExit('Refusing to overwrite an existing checkpoint or restored guest disk')
        run(docker_tool('qemu-img', 'convert', '-O', 'qcow2', '/lab/dc.qcow2', '/lab/checkpoint.qcow2'))
        (STATE / 'checkpoint.qcow2').chmod(0o444)
        digest = hashlib.file_digest((STATE / 'checkpoint.qcow2').open('rb'), 'sha256').hexdigest()
        (STATE / 'checkpoint-evidence.json').write_text(json.dumps({'sha256': digest, 'type': 'cold flattened full guest disk', 'original_stopped': True}, indent=2))
        run(docker_tool('qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', '/lab/checkpoint.qcow2', '/lab/restored.qcow2'))
        NAME += '-restored'
        PORT = '12223'
        up('restored.qcow2')
    elif action == 'provision':
        # Send only these two reviewed files, never host homes or an SSH agent.
        run(ssh_args()+['sudo -n install -d -m 0700 /opt/smolofis-identity-lab'])
        for name in ('provision-machine.sh','verify-machine.py'):
            with (REPO / 'scripts/dev/identity' / name).open('rb') as source:
                run(ssh_args()+[f'sudo -n tee /opt/smolofis-identity-lab/{name} >/dev/null'], stdin=source)
        run(ssh_args()+['sudo -n env SMOLOFIS_IDENTITY_MACHINE_LAB=1 bash /opt/smolofis-identity-lab/provision-machine.sh'])
    elif action == 'stop':
        run(ssh_args()+['sudo -n systemctl poweroff'])
    elif action == 'services':
        run(ssh_args()+['sudo -n test -f /var/lib/smolofis-identity-machine-lab/domain-ready'])
        # Apply before Docker creates its own rules; never flush a live Docker ruleset.
        active = subprocess.run(ssh_args()+['sudo -n systemctl is-active --quiet docker'])
        if active.returncode == 0:
            run(ssh_args()+['sudo -n nft list chain inet m0_guard forward >/dev/null'])
        elif active.returncode in (3, 4):
            run(ssh_args()+['sudo -n tee /etc/nftables.conf >/dev/null'], input=FIREWALL.encode())
            run(ssh_args()+['sudo -n nft -f /etc/nftables.conf'])
        else:
            raise SystemExit('Unable to establish guest Docker state; refusing firewall changes')
        run(ssh_args()+['sudo -n install -d -m 0700 /opt/smolofis-identity-lab/vm'])
        for name in ('compose.yml', 'Member.Dockerfile', 'member.sh', 'prepare-services.py',
                     'configure-gitea.py', 'verify-services.py', 'verify-lifecycle.py',
                     'strict-password-policy.py', 'verify-checkpoint.py', 'verify-migration.py',
                     'configure-dns.py', 'verify-faults.py', 'verify-domain-restore.py',
                     'verify-clock-skew.py'):
            with (Path(__file__).parent / name).open('rb') as source:
                run(ssh_args()+[f'sudo -n tee /opt/smolofis-identity-lab/vm/{name} >/dev/null'], stdin=source)
        run(ssh_args()+['sudo -n python3 /opt/smolofis-identity-lab/vm/prepare-services.py'])
        run(ssh_args()+['sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose'])
        run(ssh_args()+['sudo -n systemctl enable --now docker'])
        run(ssh_args()+['sudo -n docker-compose -f /opt/smolofis-identity-lab/vm/compose.yml up -d --build'])
    elif action == 'check-services':
        for name in ('configure-gitea.py', 'verify-services.py'):
            run(ssh_args()+[f'sudo -n python3 /opt/smolofis-identity-lab/vm/{name}'])
    elif action == 'status':
        run(['docker','ps','--all','--filter',f'name=^/{NAME}$','--format','{{.Names}} {{.Status}} {{.Ports}}'])
    else:
        raise SystemExit('usage: lab.py up|status|ssh <command>|provision|services|check-services|check-local-audit|check-web-audit|check-service-sandbox|check-operation-receipts|check-operation-history|check-audit-archive|check-archive-sync|check-audit-workflow|check-audit-lifecycle-gate|check-machine-id|stop|restore|restored-ssh <command>')

if __name__ == '__main__':
    main()
