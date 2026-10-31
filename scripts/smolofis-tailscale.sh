#!/usr/bin/env bash
#
# smolofis-tailscale.sh — Remote access helper for the SmolOfis appliance.
#
# Connects the appliance to a private Tailnet with zero-config SSH and
# exposes the remote management URL.

set -euo pipefail

readonly C_INFO=$'\033[1;32m'
readonly C_WARN=$'\033[1;33m'
readonly C_OFF=$'\033[0m'

log() { printf '%s[smolofis-tailscale]%s %s\n' "${C_INFO}" "${C_OFF}" "$*"; }

[[ ${EUID} -eq 0 ]] || { echo "must be run with sudo: sudo smolofis-tailscale [up|status|down]"; exit 1; }

if ! command -v tailscale >/dev/null 2>&1; then
    echo "tailscale binary not installed on this system" >&2
    exit 1
fi

ACTION="${1:-status}"

case "${ACTION}" in
    up|connect)
        log "connecting appliance to Tailscale (hostname: smolofis, tailscale SSH enabled)..."
        tailscale up --ssh --hostname="smolofis" --accept-routes "${@:2}"
        IP=$(tailscale ip -4 2>/dev/null || true)
        if [[ -n "${IP}" ]]; then
            log "connected successfully!"
            printf '\nRemote Dashboard URL: %shttp://%s%s\n' "${C_INFO}" "${IP}" "${C_OFF}"
            printf 'Remote SSH Access:    %sssh smolofis@%s%s\n\n' "${C_INFO}" "${IP}" "${C_OFF}"
        fi
        ;;
    autoconnect)
        # Run at boot by smolofis-tailscale-connect.service. The key goes through
        # a private file so that it never appears in the process list.
        KEY="$(sed -n 's/^TAILSCALE_AUTHKEY=//p' /etc/smolofis/tailscale.env | head -n 1)"
        [[ -n "${KEY}" ]] || exit 0
        if tailscale status >/dev/null 2>&1; then
            log "already connected"
            exit 0
        fi
        KEY_FILE="$(mktemp)"
        trap 'rm -f "${KEY_FILE}"' EXIT
        printf '%s\n' "${KEY}" > "${KEY_FILE}"
        tailscale up --auth-key="file:${KEY_FILE}" --hostname="smolofis" --ssh --accept-routes --timeout=60s
        ;;
    down|disconnect)
        log "disconnecting from Tailscale..."
        tailscale down
        log "disconnected"
        ;;
    status)
        if tailscale status >/dev/null 2>&1; then
            tailscale status
            IP=$(tailscale ip -4 2>/dev/null || true)
            if [[ -n "${IP}" ]]; then
                printf '\nRemote Dashboard: %shttp://%s%s\n' "${C_INFO}" "${IP}" "${C_OFF}"
            fi
        else
            echo "Tailscale is currently offline."
            echo "Run 'sudo smolofis-tailscale up' to connect to your Tailnet."
        fi
        ;;
    *)
        tailscale "$@"
        ;;
esac
