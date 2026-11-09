#!/bin/bash
set -euo pipefail
umask 077
cat > /etc/samba/smb.conf <<'CONF'
[global]
    workgroup = M0
    realm = M0.SMOLOFIS.TEST
    security = ADS
    idmap config * : backend = tdb
    idmap config * : range = 3000-7999
    idmap config M0 : backend = rid
    idmap config M0 : range = 10000-999999
    winbind use default domain = yes
    template shell = /usr/sbin/nologin
    server min protocol = SMB2
[projects]
    path = /srv/shares/projects
    read only = yes
    valid users = @M0\Engineering @M0\Readers
    write list = @M0\Engineering
    force group = M0\Engineering
    create mask = 0660
    directory mask = 0770
CONF
sed -i '/^passwd:/ {/winbind/! s/$/ winbind/}; /^group:/ {/winbind/! s/$/ winbind/}' /etc/nsswitch.conf
if [[ ! -f /var/lib/samba/private/secrets.tdb ]]; then
    python3 -c 'import json; print(json.load(open("/lab/passwords.json"))["Administrator"])' |
        net ads join -S dc-m0.m0.smolofis.test -U Administrator >/dev/null
fi
winbindd --foreground --no-process-group &
winbind_pid=$!
for _ in {1..60}; do
    if getent group 'M0\Engineering' >/dev/null; then break; fi
    sleep 1
done
mkdir -p /srv/shares/projects
chgrp 'M0\Engineering' /srv/shares/projects
chmod 2770 /srv/shares/projects
setfacl -m 'g:M0\Readers:rx,d:g:M0\Readers:rx' /srv/shares/projects
smbd --foreground --no-process-group &
smb_pid=$!
trap 'kill "$winbind_pid" "$smb_pid" 2>/dev/null || true; wait' TERM INT EXIT
wait -n "$winbind_pid" "$smb_pid"
