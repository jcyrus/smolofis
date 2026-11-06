"""Lifecycle controls restricted to the synthetic Alice account in this lab."""
import json
from pathlib import Path
import secrets
import sys

from samba.auth import system_session
from samba.param import LoadParm
from samba.samdb import SamDB
import ldb

lp = LoadParm()
lp.load('/etc/samba/smb.conf')
db = SamDB(url='/var/lib/samba/private/sam.ldb', session_info=system_session(), lp=lp)
user_filter = '(&(objectClass=user)(sAMAccountName=alice))'
password_file = Path('/lab/passwords.json')
action = sys.argv[1]

if action == 'rotate':
    passwords = json.loads(password_file.read_text())
    passwords['alice-previous'] = passwords['alice']
    passwords['alice'] = 'Lab9!' + secrets.token_hex(18)
    db.setpassword(user_filter, passwords['alice'], force_change_at_next_login=False)
    password_file.write_text(json.dumps(passwords))
elif action == 'disable':
    db.disable_account(user_filter)
elif action == 'enable':
    db.enable_account(user_filter)
elif action in ('snapshot', 'compare'):
    result = db.search(base=db.domain_dn(), scope=ldb.SCOPE_SUBTREE,
                       expression=user_filter, attrs=['objectGUID', 'objectSid'])
    identity = {key: bytes(result[0][key][0]).hex() for key in ('objectGUID', 'objectSid')}
    path = Path('/lab/identity-baseline.json')
    if action == 'snapshot':
        path.write_text(json.dumps(identity))
    elif json.loads(path.read_text()) != identity:
        raise SystemExit('FAIL: directory identity changed across restart')
else:
    raise SystemExit('unknown lifecycle action')
print(f'PASS: directory lifecycle {action}')
