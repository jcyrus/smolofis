"""Exercise offline directory preparation on real ext4/native SYSVOL ACLs; never production."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time

ROOT = Path('/var/lib/smolofis-identity-machine-lab')
SOURCE = Path('/opt/smolofis-office-bootstrap-lab')
BOOTSTRAP = SOURCE / 'bootstrap.py'
if (os.geteuid() != 0 or not Path('/etc/smolofis-m0-fullvm').exists()
        or not (ROOT/'domain-ready').exists() or Path('/run/systemd/container').exists()
        or subprocess.run(['systemd-detect-virt','--vm'],capture_output=True).returncode):
    raise SystemExit('Owned full-kernel VM required')
if not sys.argv[1:]:
    assert not Path('/etc/smolofis-release').exists(), 'Refusing an installed appliance'
    fixture=ROOT/('office-bootstrap-'+secrets.token_hex(5));fixture.mkdir(mode=0o700)
    created=[];release=Path('/etc/smolofis-release');release_inode=None
    try:
        for path in (Path('/etc/smolofis'),Path('/var/lib/smolofis')):
            if not path.exists():path.mkdir(mode=0o755);created.append(path)
        fd=os.open(release,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd);release_inode=release.stat().st_ino
        subprocess.run(['unshare','--mount','--fork',sys.executable,__file__,'--inside',str(fixture)],check=True,timeout=360)
    finally:
        if release_inode is not None:
            assert release.stat().st_ino==release_inode and release.stat().st_size==0
            release.unlink()
        for path in reversed(created):path.rmdir()
    raise SystemExit(0)
assert len(sys.argv)==3 and sys.argv[1]=='--inside'
fixture=Path(sys.argv[2]);assert fixture.parent==ROOT and fixture.name.startswith('office-bootstrap-')
subprocess.run(['mount','--make-rprivate','/'],check=True)
os.umask(0o077)
config=fixture/'config';config.mkdir(mode=0o755);config.chmod(0o755)
data=fixture/'data';data.mkdir(mode=0o755);data.chmod(0o755)
release=fixture/'release';release.write_text('0.1.0-office-bootstrap-lab\n')
for source,target in ((config,Path('/etc/smolofis')),(data,Path('/var/lib/smolofis')),(release,Path('/etc/smolofis-release'))):
    subprocess.run(['mount','--bind',str(source),str(target)],check=True)
for path in ('/etc/samba','/var/lib/samba'):
    subprocess.run(['mount','--bind',path,path],check=True)
    subprocess.run(['mount','-o','remount,bind,ro',path],check=True)
base=Path('/var/lib/smolofis/directory');plan_file=Path('/etc/smolofis/directory-plan.json')
plan={'version':1,'dns_domain':'office.smolofis.internal','short_domain':'SMOLOFIS','hostname':'smolofis-dc','address':'172.31.83.10','forwarder':None}
checks=[]

def passed(message):checks.append(message);print('PASS: '+message,flush=True)
def save_plan():plan_file.write_text(json.dumps(plan));plan_file.chmod(0o600)
def run(command,**kwargs):return subprocess.run(command,capture_output=True,timeout=140,**kwargs)
def invoke(command='status'):
    return run([sys.executable,str(BOOTSTRAP),command])
def status():
    r=invoke();assert r.returncode in (0,1)
    (fixture/'status-stderr.log').write_bytes(r.stderr)
    assert r.stdout, 'Status failed; inspect private status-stderr.log'
    return r,json.loads(r.stdout)
def snapshot():
    return {str(p.relative_to(base)):(p.lstat().st_ino,stat.S_IMODE(p.lstat().st_mode),hashlib.sha256(p.read_bytes()).hexdigest())
            for p in base.rglob('*') if p.is_file() and not p.is_symlink()}
def switch_data(label):
    subprocess.run(['umount','/var/lib/smolofis'],check=True)
    target=fixture/label;target.mkdir(mode=0o755);target.chmod(0o755)
    subprocess.run(['mount','--bind',str(target),'/var/lib/smolofis'],check=True)

def assert_global_unchanged(before):
    assert {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path('/etc/samba/smb.conf'),Path('/etc/hosts'),Path('/etc/resolv.conf'))}==before
    assert subprocess.run(['systemctl','is-active','--quiet','samba-ad-dc.service']).returncode==0

save_plan()
global_before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path('/etc/samba/smb.conf'),Path('/etc/hosts'),Path('/etc/resolv.conf'))}
initial=list(Path('/var/lib/smolofis').iterdir());r,s=status();assert r.returncode==1 and s['phase']=='absent'
assert list(Path('/var/lib/smolofis').iterdir())==initial
passed('absent status is read-only and creates neither state nor locks')
r=run([sys.executable,str(BOOTSTRAP),'prepare'],user=65534,group=65534,extra_groups=())
assert r.returncode==1 and r.stdout==b'' and not base.exists()
passed('non-root preparation is refused before reading the plan or writing data')
plan_file.chmod(0o666);r=invoke('prepare');assert r.returncode==1 and not base.exists();plan_file.chmod(0o600)
passed('untrusted setup plan is rejected before persistent writes')
process=subprocess.Popen([sys.executable,str(BOOTSTRAP),'prepare'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
arguments=Path(f'/proc/{process.pid}/cmdline').read_bytes()
stdout,stderr=process.communicate(timeout=140)
(fixture/'prepare-stdout.json').write_bytes(stdout);(fixture/'prepare-stderr.log').write_bytes(stderr)
assert process.returncode==0,'Preparation failed; inspect root-only provision.log'
secret=(base/'bootstrap-admin.secret').read_bytes().strip();assert len(secret)>=32
assert secret not in stdout+stderr+arguments and stat.S_IMODE((base/'bootstrap-admin.secret').stat().st_mode)==0o600
assert stat.S_IMODE(base.stat().st_mode)==0o700
prepared=json.loads(stdout);assert prepared['phase']=='prepared' and not prepared['service_started_by_command'] and not prepared['dns_changed_by_command']
passed('real native Samba provision uses protected generated credentials and creates an offline private data tree')
result=run(['samba-tool','ntacl','sysvolcheck','--configfile='+str(base/'domain/etc/smb.conf')])
(fixture/'sysvolcheck.log').write_bytes(result.stdout+result.stderr)
assert result.returncode==0,'Native SYSVOL ACL verification failed'
sysvol=base/'domain/state/sysvol'
if not sysvol.exists():sysvol=base/'domain/sysvol'
assert any('security.NTACL' in os.listxattr(p) for p in sysvol.rglob('*'))
passed('provisioned SYSVOL passes Samba default ACL verification with native security.NTACL')
assert_global_unchanged(global_before)
passed('global Samba configuration, DNS/hosts and existing directory service remain untouched')
before=snapshot();r=invoke('prepare');assert r.returncode==0 and json.loads(r.stdout)==prepared and snapshot()==before
r,s=status();assert r.returncode==0 and s==prepared and snapshot()==before
passed('identical prepare and status preserve the operation, directory identity, secret and data files')
plan['address']='172.31.83.11';save_plan();r=invoke('prepare');assert r.returncode==1 and snapshot()==before
plan['address']='172.31.83.10';save_plan()
state_file=base/'bootstrap-state.json';original=state_file.read_bytes();state=json.loads(original)
state['machine_digest']='ab'*32;state_file.write_text(json.dumps(state));r=invoke('prepare');assert r.returncode==1;state_file.write_bytes(original)
passed('changed plans and foreign-machine state refuse re-provisioning without replacing existing data')
db=base/'domain/private/sam.ldb';saved=db.with_suffix('.saved');db.rename(saved)
r=invoke();assert r.returncode==1 and r.stdout==b'';saved.rename(db)
passed('missing prepared artifacts fail status without repair or false prepared output')
secret_file=base/'bootstrap-admin.secret';secret_file.chmod(0o644)
r=invoke();assert r.returncode==1 and r.stdout==b'';secret_file.chmod(0o600)
passed('prepared status rejects a root secret with unsafe permissions')
switch_data('failed-data')
code="""import importlib.util
s=importlib.util.spec_from_file_location('bootstrap',%r);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def fail(*args):
 (m.BASE/'partial-evidence').write_text('retained')
 raise OSError('synthetic provisioning failure')
m.provision_domain=fail
try:m.main()
except Exception:raise SystemExit(1)
""" % str(BOOTSTRAP)
r=run([sys.executable,'-c',code,'prepare']);assert r.returncode==1
r,s=status();assert s['phase']=='failed';before=snapshot();r=invoke('prepare');assert r.returncode==1 and snapshot()==before
passed('failure retains private partial evidence and refuses automatic replay')
switch_data('interrupted-data')
code="""import importlib.util,os,signal
s=importlib.util.spec_from_file_location('bootstrap',%r);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def crash(*args):os.kill(os.getpid(),signal.SIGKILL)
m.provision_domain=crash;m.main()
""" % str(BOOTSTRAP)
r=run([sys.executable,'-c',code,'prepare']);assert r.returncode==-signal.SIGKILL
before=snapshot();r,s=status();assert s['phase']=='interrupted' and snapshot()==before
r=invoke('prepare');assert r.returncode==1 and snapshot()==before
passed('SIGKILL leaves an interrupted state that status reports without rewriting or replaying it')
assert_global_unchanged(global_before)
report={'checks':checks,'source_sha256':hashlib.sha256(BOOTSTRAP.read_bytes()).hexdigest(),'real_samba_provision':True,
        'native_sysvol_acl':True,'production_modified':False,'new_services_started':False,'global_configuration_unchanged':True}
(fixture/'report.json').write_text(json.dumps(report,indent=2))
print(f'PASS: {len(checks)} offline bootstrap checks; evidence {fixture}',flush=True)
