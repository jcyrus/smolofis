#!/usr/bin/env bash
#
# smolofis-identity — per-device SSH host keys and operator password.
#
# The root filesystem is a RAM overlay on a read-only squashfs, so anything
# written to /etc/ssh or /etc/shadow is gone after a reboot. This script keeps
# the device's identity under /etc/smolofis, which is on persistent storage.
#
# Usage:
#   smolofis-identity restore   # at boot, before sshd and any login prompt
#   smolofis-identity save      # after /etc/shadow changes

set -euo pipefail

readonly IDENTITY_ROOT="/etc/smolofis/identity"
readonly HOST_KEY_DIR="${IDENTITY_ROOT}/ssh"
readonly HASH_FILE="${IDENTITY_ROOT}/operator-password.hash"
readonly OPERATOR="smolofis"

log() { printf '[smolofis-identity] %s\n' "$*"; }

# The login banner stops advertising the default once it no longer works.
hide_default_credentials() {
    sed -i '/smolofis \/ smolofis/d' /etc/issue
}

restore_host_keys() {
    install -d -m 0700 "${IDENTITY_ROOT}" "${HOST_KEY_DIR}"
    local type key
    for type in ed25519 ecdsa rsa; do
        key="${HOST_KEY_DIR}/ssh_host_${type}_key"
        if [[ ! -s "${key}" || ! -s "${key}.pub" ]]; then
            log "generating ${type} SSH host key"
            rm -f "${key}" "${key}.pub"
            ssh-keygen -q -t "${type}" -N '' -C "root@smolofis" -f "${key}"
        fi
        install -o root -g root -m 0600 "${key}" /etc/ssh/
        install -o root -g root -m 0644 "${key}.pub" /etc/ssh/
    done
}

restore_operator_password() {
    [[ -s "${HASH_FILE}" ]] || return 0
    log "restoring operator password"
    printf '%s:%s\n' "${OPERATOR}" "$(<"${HASH_FILE}")" | chpasswd -e
    hide_default_credentials
}

save_operator_password() {
    local hash changed
    IFS=: read -r _ hash changed _ < <(getent shadow "${OPERATOR}")
    # A last-changed day of 0 is the expired default shipped in the image.
    [[ "${changed}" != "0" ]] || return 0
    if [[ -s "${HASH_FILE}" && "$(<"${HASH_FILE}")" == "${hash}" ]]; then
        return 0
    fi
    install -d -m 0700 "${IDENTITY_ROOT}"
    (umask 077 && printf '%s\n' "${hash}" > "${HASH_FILE}.tmp")
    mv -f "${HASH_FILE}.tmp" "${HASH_FILE}"
    hide_default_credentials
    log "saved operator password"
}

case "${1:-}" in
    restore)
        restore_host_keys
        restore_operator_password
        ;;
    save)
        save_operator_password
        ;;
    *)
        echo "usage: smolofis-identity restore|save" >&2
        exit 2
        ;;
esac
