#!/usr/bin/env bash
# Destructive to an EMPTY LAB MACHINE ONLY, never an installed appliance.
set -euo pipefail
umask 077
LAB_ROOT=/var/lib/smolofis-identity-machine-lab
if [[ "${SMOLOFIS_IDENTITY_MACHINE_LAB:-}" != 1 || "$EUID" != 0 ]]; then
    echo 'Run as root only in an isolated empty lab machine with SMOLOFIS_IDENTITY_MACHINE_LAB=1.' >&2
    exit 2
fi
if [[ -e /etc/smolofis-release || ! -f /etc/smolofis-m0-fullvm || -e /run/systemd/container ]] \
    || ! systemd-detect-virt --vm >/dev/null; then
    echo 'Refusing provisioning outside a marked full-kernel VM lab or on an appliance.' >&2
    exit 2
fi
[[ -f /var/lib/m0-bootstrap-ready ]] && systemctl is-active --quiet nftables \
    || { echo 'Lab bootstrap and network isolation must finish first.' >&2; exit 2; }
# shellcheck source=/dev/null
source /etc/os-release
if [[ "$ID" != debian || "$VERSION_ID" != 13 ]]; then
    echo 'This lab provisioner requires Debian 13.' >&2
    exit 2
fi
if [[ -e /var/lib/samba/private/sam.ldb && ! -f "$LAB_ROOT/owned" ]]; then
    echo 'Refusing to touch a directory not created by this lab.' >&2
    exit 2
fi
if [[ -e /var/lib/samba/private/sam.ldb && ! -f "$LAB_ROOT/domain-ready" ]]; then
    echo 'Refusing to reuse a partially provisioned lab domain; inspect its log and use a fresh lab machine.' >&2
    exit 2
fi
install -d -m 0700 "$LAB_ROOT"
touch "$LAB_ROOT/owned"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends samba-ad-dc samba-ad-provision \
    samba-vfs-modules samba winbind smbclient ldap-utils acl openssl python3 bind9-dnsutils tdb-tools
systemctl disable --now smbd.service nmbd.service winbind.service
if [[ ! -f "$LAB_ROOT/passwords.json" ]]; then
    python3 - <<'PY'
import json, secrets
from pathlib import Path
Path('/var/lib/smolofis-identity-machine-lab/passwords.json').write_text(json.dumps({
    name: 'Lab9!' + secrets.token_hex(18) for name in ('Administrator', 'alice')
}))
PY
fi
password() { python3 -c 'import json,sys; print(json.load(open("/var/lib/smolofis-identity-machine-lab/passwords.json"))[sys.argv[1]])' "$1"; }
if [[ ! -f /var/lib/samba/private/sam.ldb ]]; then
    machine_ip="$(ip -4 -o addr show dev lab0 | awk '{print $4}' | cut -d/ -f1)"
    [[ "$machine_ip" == 172.31.83.10 ]] || { echo 'Unexpected isolated directory interface.' >&2; exit 2; }
    : > /etc/samba/smb.conf
    samba-tool domain provision --realm=M0.SMOLOFIS.TEST --domain=M0 \
        --host-name=dc-m0 --host-ip="$machine_ip" --server-role=dc \
        --dns-backend=SAMBA_INTERNAL --use-rfc2307 \
        --option="interfaces=lo ${machine_ip}/32" --option='bind interfaces only=yes' \
        --option='old password allowed period=0' \
        --option='dns forwarder=10.0.2.3' \
        --adminpass="$(password Administrator)" > "$LAB_ROOT/provision.log" 2>&1
    samba-tool user create alice "$(password alice)" \
        --mail-address=alice@m0.smolofis.test >/dev/null
    touch "$LAB_ROOT/domain-ready"
fi
if [[ ! -f "$LAB_ROOT/tls-ready" ]]; then
    install -d -m 0700 /var/lib/samba/private/tls
    openssl req -x509 -newkey rsa:2048 -nodes -days 30 \
        -subj '/CN=SmolOfis native lab CA' -keyout "$LAB_ROOT/ca.key" \
        -out /var/lib/samba/private/tls/ca.pem 2>/dev/null
    openssl req -newkey rsa:2048 -nodes -subj '/CN=localhost' \
        -keyout /var/lib/samba/private/tls/key.pem -out "$LAB_ROOT/server.csr" 2>/dev/null
    printf 'subjectAltName=DNS:localhost,DNS:dc-m0.m0.smolofis.test,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' > "$LAB_ROOT/cert.ext"
    openssl x509 -req -in "$LAB_ROOT/server.csr" \
        -CA /var/lib/samba/private/tls/ca.pem -CAkey "$LAB_ROOT/ca.key" \
        -CAcreateserial -days 30 -extfile "$LAB_ROOT/cert.ext" \
        -out /var/lib/samba/private/tls/cert.pem 2>/dev/null
    touch "$LAB_ROOT/tls-ready"
fi
systemctl unmask samba-ad-dc.service
install -d -m 0755 /etc/systemd/resolved.conf.d
printf '[Resolve]\nDNS=172.31.83.10\nDomains=~m0.smolofis.test\n' > /etc/systemd/resolved.conf.d/m0-directory.conf
systemctl restart systemd-resolved
systemctl enable --now samba-ad-dc.service
python3 /opt/smolofis-identity-lab/verify-machine.py snapshot
