#!/bin/bash
# Synthetic credentials live only in the disposable lab volume. Never trace.
set -euo pipefail
umask 077
realm=LAB.SMOLOFIS.TEST

case "${1:-}" in
dc)
    if [[ ! -f /lab/passwords.json ]]; then
        python3 - <<'PY'
import json, secrets
from pathlib import Path
Path('/lab/passwords.json').write_text(json.dumps({
    name: 'Lab9!' + secrets.token_hex(18)
    for name in ('Administrator', 'alice', 'reader', 'outsider', 'ldap-reader')
}))
PY
    fi
    # Upgrade existing disposable lab volumes with additional test identities.
    python3 - <<'PY'
import json, secrets
from pathlib import Path
p = Path('/lab/passwords.json')
values = json.loads(p.read_text())
for name in ('lab-admin', 'legacy-local', 'legacy-dir'):
    values.setdefault(name, 'Lab9!' + secrets.token_hex(18))
p.write_text(json.dumps(values))
PY
    password() { python3 -c 'import json,sys; print(json.load(open("/lab/passwords.json"))[sys.argv[1]])' "$1"; }
    if [[ ! -f /var/lib/samba/private/sam.ldb ]]; then
        # The package provides a standalone-server config; provisioning needs a blank file.
        : > /etc/samba/smb.conf
        samba-tool domain provision --realm="$realm" --domain=LAB \
            --server-role=dc --dns-backend=SAMBA_INTERNAL --use-rfc2307 \
            --host-ip=10.83.0.10 --adminpass="$(password Administrator)" \
            --option='acl_xattr:security_acl_name=user.NTACL' > /lab/provision.log 2>&1
        for name in alice reader outsider ldap-reader; do
            samba-tool user create "$name" "$(password "$name")" \
                --mail-address="$name@lab.smolofis.test" >/dev/null
        done
        samba-tool group add Engineering >/dev/null
        samba-tool group add Readers >/dev/null
        samba-tool group addmembers Engineering alice >/dev/null
        samba-tool group addmembers Readers reader >/dev/null
    fi
    if ! samba-tool user show legacy-dir >/dev/null 2>&1; then
        samba-tool user create legacy-dir "$(password legacy-dir)" \
            --mail-address=legacy-dir@lab.smolofis.test >/dev/null
    fi
    # Explicit certificate SANs allow Gitea to verify LDAPS by service name.
    if [[ ! -f /lab/ca.crt ]]; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
            -subj '/CN=SmolOfis disposable lab CA' \
            -keyout /lab/ca.key -out /lab/ca.crt 2>/dev/null
        openssl req -newkey rsa:2048 -nodes -subj '/CN=dc.lab.smolofis.test' \
            -keyout /var/lib/samba/private/tls/key.pem -out /lab/dc.csr 2>/dev/null
        printf 'subjectAltName=DNS:dc,DNS:dc.lab.smolofis.test\nextendedKeyUsage=serverAuth\n' > /lab/cert.ext
        openssl x509 -req -in /lab/dc.csr -CA /lab/ca.crt -CAkey /lab/ca.key \
            -CAcreateserial -days 30 -extfile /lab/cert.ext \
            -out /var/lib/samba/private/tls/cert.pem 2>/dev/null
        cp /lab/ca.crt /var/lib/samba/private/tls/ca.pem
        chmod 0644 /lab/ca.crt
    fi
    password ldap-reader > /lab/ldap-bind-password
    chmod 0644 /lab/ldap-bind-password
    password lab-admin > /lab/gitea-admin-password
    chmod 0644 /lab/gitea-admin-password
    touch /lab/ready
    exec samba --foreground --no-process-group
    ;;
files)
    cat > /etc/samba/smb.conf <<'CONF'
[global]
    workgroup = LAB
    realm = LAB.SMOLOFIS.TEST
    security = ADS
    idmap config * : backend = tdb
    idmap config * : range = 3000-7999
    idmap config LAB : backend = rid
    idmap config LAB : range = 10000-999999
    winbind use default domain = yes
    template shell = /usr/sbin/nologin
    server min protocol = SMB2
[projects]
    path = /srv/shares/projects
    read only = yes
    valid users = @LAB\Engineering @LAB\Readers
    write list = @LAB\Engineering
    force group = LAB\Engineering
    create mask = 0660
    directory mask = 0770
CONF
    sed -i '/^passwd:/ {/winbind/! s/$/ winbind/}; /^group:/ {/winbind/! s/$/ winbind/}' /etc/nsswitch.conf
    if [[ ! -f /var/lib/samba/private/secrets.tdb ]]; then
        python3 -c 'import json; print(json.load(open("/lab/passwords.json"))["Administrator"])' |
            net ads join -S dc.lab.smolofis.test -U Administrator >/dev/null
    fi
    winbindd --foreground --no-process-group &
    winbind_pid=$!
    for _ in {1..60}; do
        if getent group 'LAB\Engineering' >/dev/null; then break; fi
        sleep 1
    done
    mkdir -p /srv/shares/projects
    chgrp 'LAB\Engineering' /srv/shares/projects
    chmod 2770 /srv/shares/projects
    setfacl -m 'g:LAB\Readers:rx,d:g:LAB\Readers:rx' /srv/shares/projects
    smbd --foreground --no-process-group &
    smb_pid=$!
    trap 'kill "$winbind_pid" "$smb_pid" 2>/dev/null || true; wait' TERM INT EXIT
    wait -n "$winbind_pid" "$smb_pid"
    ;;
verify)
    shift
    exec python3 /usr/local/bin/verify-identity "$@"
    ;;
migration)
    shift
    exec python3 /usr/local/bin/lab-migration "$@"
    ;;
live-smb)
    shift
    exec python3 /usr/local/bin/lab-live-smb client "$@"
    ;;
*) echo 'usage: identity-lab dc|files|verify' >&2; exit 2 ;;
esac
