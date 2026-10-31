#!/usr/bin/env bash
#
# smolofis-update — signed A/B system updates with automatic rollback.
#
# The system partition holds two slots. An update goes into the unused one and
# GRUB tries it once. smolofis-update-verify promotes it after the stack reports
# ready; otherwise the next boot returns to the active slot. Database migrations
# are not rolled back, so apply takes a backup first.
#
# Usage:
#   smolofis-update check [--channel stable|nightly]
#   sudo smolofis-update apply [--channel stable|nightly] [--no-reboot]
#   sudo smolofis-update apply --from DIR [--signers FILE] [--no-reboot]
#   smolofis-update status

set -euo pipefail

readonly RELEASES="https://github.com/jcyrus/smolofis/releases"
readonly MEDIUM="/run/live/medium"
readonly GRUBENV="${MEDIUM}/boot/grub/grubenv"
readonly WORK="/var/lib/smolofis/update"
readonly NAMESPACE="smolofis-release"
readonly VERIFY_TIMEOUT=900

log() { printf '[smolofis-update] %s\n' "$*"; }
die() { printf '[smolofis-update] ERROR: %s\n' "$*" >&2; exit 1; }

manifest_url() {
    case "$1" in
        stable)  echo "${RELEASES}/latest/download/smolofis-manifest.txt" ;;
        nightly) echo "${RELEASES}/download/nightly/smolofis-manifest.txt" ;;
        *) die "unknown channel: $1" ;;
    esac
}

booted_slot() { sed -n 's/.*live-media-path=\([^ ]*\).*/\1/p' /proc/cmdline; }
active_slot() { grub-editenv "${GRUBENV}" list | sed -n 's/^active_slot=//p'; }
field() { sed -n "s/^$2=//p" "$1"; }
is_newer() { [[ "$1" != "$2" && "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" == "$1" ]]; }

remount() { mount -o "remount,$1" "${MEDIUM}"; }

verify_signature() {
    ssh-keygen -q -Y verify -f "$1" -I "${NAMESPACE}" -n "${NAMESPACE}" \
        -s "${WORK}/manifest.txt.sig" < "${WORK}/manifest.txt" >/dev/null \
        || die "manifest signature is not valid"
}

cmd_check() {
    local channel="stable" remote
    [[ "${1:-}" != "--channel" ]] || channel="$2"
    remote="$(curl -fsSL "$(manifest_url "${channel}")" | sed -n 's/^VERSION=//p')"
    [[ -n "${remote}" ]] || die "could not read the ${channel} release"
    printf 'current: %s\nlatest:  %s (%s)\n' "$(cat /etc/smolofis-release)" "${remote}" "${channel}"
    is_newer "${remote}" "$(cat /etc/smolofis-release)" && echo "update available" || echo "up to date"
}

cmd_status() {
    printf 'version %s\nbooted  %s\nactive  %s\n' "$(cat /etc/smolofis-release)" "$(booted_slot)" "$(active_slot)"
}

cmd_apply() {
    local channel="stable" from="" signers="/etc/smolofis-release-signers" reboot=1
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --channel)   channel="$2"; shift 2 ;;
            --from)      from="$2"; shift 2 ;;
            --signers)   signers="$2"; shift 2 ;;
            --no-reboot) reboot=0; shift ;;
            *) die "unknown argument: $1" ;;
        esac
    done
    [[ ${EUID} -eq 0 ]] || die "must run as root (use sudo)"
    [[ -n "$(booted_slot)" ]] || die "not running from an installed disk"

    install -d -m 0700 "${WORK}"
    rm -f "${WORK}"/*
    local url version iso sum slot mnt
    if [[ -n "${from}" ]]; then
        cp "${from}/smolofis-manifest.txt" "${WORK}/manifest.txt"
        cp "${from}/smolofis-manifest.txt.sig" "${WORK}/manifest.txt.sig"
    else
        url="$(manifest_url "${channel}")"
        curl -fsSL "${url}" -o "${WORK}/manifest.txt"
        curl -fsSL "${url}.sig" -o "${WORK}/manifest.txt.sig"
    fi
    verify_signature "${signers}"

    version="$(field "${WORK}/manifest.txt" VERSION)"
    iso="$(basename "$(field "${WORK}/manifest.txt" ISO)")"
    sum="$(field "${WORK}/manifest.txt" SHA256)"
    is_newer "${version}" "$(cat /etc/smolofis-release)" || die "${version} is not newer than the running system"

    if [[ -n "${from}" ]]; then
        cp "${from}/${iso}" "${WORK}/update.iso"
    else
        log "downloading ${iso}"
        curl -fL --retry 3 "$(dirname "${url}")/${iso}" -o "${WORK}/update.iso"
    fi
    printf '%s  %s\n' "${sum}" "${WORK}/update.iso" | sha256sum -c --quiet - || die "image checksum does not match"

    log "taking a backup before the update"
    smolofis-backup run

    slot="system-a"
    [[ "$(active_slot)" != "system-a" ]] || slot="system-b"
    mnt="$(mktemp -d)"
    trap 'umount "${mnt}" 2>/dev/null || true; rmdir "${mnt}" 2>/dev/null || true; remount ro || true; rm -f "${WORK}"/*' EXIT
    mount -o loop,ro "${WORK}/update.iso" "${mnt}"
    remount rw

    log "writing ${version} to ${slot}"
    rm -rf "${MEDIUM:?}/${slot}"
    mkdir -p "${MEDIUM}/${slot}"
    cp "${mnt}/live/vmlinuz" "${mnt}/live/initrd.img" "${mnt}/live/filesystem.squashfs" "${MEDIUM}/${slot}/"
    sync
    grub-editenv "${GRUBENV}" set "next_slot=${slot}"
    sync

    if [[ ${reboot} -eq 1 ]]; then
        log "rebooting into ${slot}"
        systemctl reboot
    else
        log "${slot} boots next; reboot when ready"
    fi
}

# Run at boot by smolofis-update-verify.service.
cmd_verify() {
    local booted deadline
    booted="$(booted_slot)"
    [[ -n "${booted}" && "${booted}" != "$(active_slot)" ]] || exit 0
    log "verifying ${booted}"
    deadline=$(( SECONDS + VERIFY_TIMEOUT ))
    while (( SECONDS < deadline )); do
        if curl -fsS --max-time 5 http://127.0.0.1:8090/api/state | grep -q '"phase":"ready"'; then
            remount rw
            grub-editenv "${GRUBENV}" set "active_slot=${booted}"
            sync
            remount ro
            log "${booted} is now the active slot"
            exit 0
        fi
        sleep 10
    done
    log "${booted} did not become ready; returning to the previous slot"
    systemctl reboot
}

case "${1:-}" in
    check)  shift; cmd_check "$@" ;;
    status) cmd_status ;;
    apply)  shift; cmd_apply "$@" ;;
    verify) cmd_verify ;;
    *)
        echo "usage: smolofis-update check|apply|status" >&2
        exit 2
        ;;
esac
