#!/usr/bin/env bash
#
# smolofis-backup — back up and restore Gitea, Coolify and device secrets.
#
# Gitea is stopped briefly so that its SQLite database and repositories are
# copied consistently, which keeps a restore to a single tar extract. Backups
# include /etc/smolofis, because Coolify cannot decrypt its database without
# APP_KEY. They sit on the data partition, so to survive a disk failure set
# BACKUP_REMOTE=user@host:/path in /etc/smolofis/backup.env. Each backup is then
# copied there over SSH. The target receives secrets, so it must be trusted.
#
# Usage:
#   sudo smolofis-backup run
#   sudo smolofis-backup list
#   sudo smolofis-backup push
#   sudo smolofis-backup push-key
#   sudo smolofis-backup restore <name> --yes

set -euo pipefail

readonly STATE_ROOT="/var/lib/smolofis"
readonly BACKUP_ROOT="${STATE_ROOT}/backups"
readonly KEEP=7
readonly REMOTE_ENV="/etc/smolofis/backup.env"
readonly PUSH_KEY="/etc/smolofis/identity/backup_ed25519"

log() { printf '[smolofis-backup] %s\n' "$*"; }
die() { printf '[smolofis-backup] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "must run as root (use sudo)"

run_backup() {
    local name partial
    name="$(date -u +%Y%m%dT%H%M%SZ)"
    partial="${BACKUP_ROOT}/.partial-${name}"
    install -d -m 0700 "${BACKUP_ROOT}" "${partial}"
    trap 'docker start smolofis-gitea >/dev/null 2>&1 || true; rm -rf "${partial}"' EXIT

    log "dumping the Coolify database"
    docker exec smolofis-coolify-db pg_dump -U coolify -Fc coolify > "${partial}/coolify-db.dump"

    log "archiving Coolify files and device configuration"
    tar -czf "${partial}/coolify-files.tar.gz" --exclude=coolify/db --exclude=coolify/redis \
        --exclude=coolify/backups -C "${STATE_ROOT}" coolify
    tar -czf "${partial}/config.tar.gz" -C / etc/smolofis

    log "archiving Gitea (stopped briefly)"
    docker stop smolofis-gitea >/dev/null
    tar -czf "${partial}/gitea.tar.gz" -C "${STATE_ROOT}" gitea
    docker start smolofis-gitea >/dev/null

    mv "${partial}" "${BACKUP_ROOT}/${name}"
    trap - EXIT

    # Names sort by time, so everything before the last KEEP is expired.
    find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | head -n "-${KEEP}" \
        | xargs -r rm -rf
    log "backup complete: ${name}"
    if [[ -s "${REMOTE_ENV}" ]]; then
        run_push
    fi
}

ensure_push_key() {
    [[ -s "${PUSH_KEY}" ]] && return 0
    install -d -m 0700 "$(dirname "${PUSH_KEY}")"
    ssh-keygen -q -t ed25519 -N "" -C smolofis-backup -f "${PUSH_KEY}"
}

# The local copy stays complete, so a failed push never costs a backup.
run_push() {
    local remote
    remote="$(sed -n 's/^BACKUP_REMOTE=//p' "${REMOTE_ENV}" 2>/dev/null || true)"
    [[ -n "${remote}" ]] || die "set BACKUP_REMOTE=user@host:/path in ${REMOTE_ENV}"
    ensure_push_key
    log "copying backups to ${remote}"
    rsync -a --exclude='.partial-*' \
        -e "ssh -i ${PUSH_KEY} -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
        "${BACKUP_ROOT}/" "${remote}/"
    log "copy complete"
}

run_restore() {
    local name="${1:-}" source
    source="${BACKUP_ROOT}/${name}"
    [[ -n "${name}" && -f "${source}/gitea.tar.gz" ]] || die "no such backup: ${name:-<none>}"
    [[ "${2:-}" == "--yes" ]] || die "this replaces current data; pass --yes to confirm"

    log "stopping the services being restored"
    docker stop smolofis-gitea-runner smolofis-gitea smolofis-coolify >/dev/null

    log "restoring device configuration"
    tar -xzf "${source}/config.tar.gz" -C /

    log "restoring Gitea and Coolify files"
    rm -rf "${STATE_ROOT}/gitea"
    tar -xzf "${source}/gitea.tar.gz" -C "${STATE_ROOT}"
    tar -xzf "${source}/coolify-files.tar.gz" -C "${STATE_ROOT}"

    log "restoring the Coolify database"
    docker exec -i smolofis-coolify-db pg_restore -U coolify -d coolify --clean --if-exists \
        < "${source}/coolify-db.dump"
    # The restored secrets may hold a different password than this database.
    local db_password
    db_password="$(sed -n 's/^POSTGRES_PASSWORD=//p' /etc/smolofis/secrets.env)"
    docker exec smolofis-coolify-db psql -q -U coolify -d coolify \
        -c "ALTER ROLE coolify PASSWORD '${db_password}'"

    log "restarting the stack"
    systemctl restart smolofis-infrastructure.service
    log "restore complete: ${name}"
}

case "${1:-}" in
    run) run_backup ;;
    list) find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -name '20*' -printf '%f\n' 2>/dev/null | sort ;;
    restore) run_restore "${2:-}" "${3:-}" ;;
    push) run_push ;;
    push-key) ensure_push_key; cat "${PUSH_KEY}.pub" ;;
    *)
        echo "usage: smolofis-backup run|list|push|push-key|restore <name> --yes" >&2
        exit 2
        ;;
esac
