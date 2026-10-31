#!/usr/bin/env bash
#
# smolofis-support — collect a support bundle for diagnosing an appliance.
#
# Holds system state and logs, and never reads /etc/smolofis or container
# environments, where the secrets live. Logs can still name hosts and users, so
# read the bundle before sharing it.
#
# Usage:
#   sudo smolofis-support

set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "must run as root (use sudo)" >&2; exit 1; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT="/var/lib/smolofis/support-${STAMP}.tar.gz"
[[ -d /var/lib/smolofis ]] || OUTPUT="/tmp/support-${STAMP}.tar.gz"
BUNDLE="$(mktemp -d)"
trap 'rm -rf "${BUNDLE}"' EXIT

# capture <file> <command...>: a failing command must not lose the rest.
capture() {
    local file="$1"; shift
    { "$@" 2>&1 || echo "[exit $?]"; } | sed -E 's/tskey-[A-Za-z0-9-]+/tskey-REDACTED/g' > "${BUNDLE}/${file}"
}

capture version.txt cat /etc/smolofis-release
capture update-status.txt smolofis-update status
capture cmdline.txt cat /proc/cmdline
capture disks.txt lsblk -o NAME,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS
capture space.txt df -h
capture failed-units.txt systemctl --no-pager --failed
capture firewall.txt nft list ruleset
capture containers.txt docker ps -a
capture journal.txt journalctl -b --no-pager -n 2000
capture panel.txt journalctl -b --no-pager -u smolofis-panel.service
capture stack.txt journalctl -b --no-pager -u smolofis-infrastructure.service

for container in smolofis-gitea smolofis-gitea-runner smolofis-coolify smolofis-coolify-db smolofis-coolify-redis coolify-proxy; do
    capture "logs-${container}.txt" docker logs --tail 500 "${container}"
done

(umask 077 && tar -czf "${OUTPUT}" -C "${BUNDLE}" .)
echo "${OUTPUT}"
