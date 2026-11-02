#!/usr/bin/env bash
#
# smolofis-backup — back up and restore Gitea, Coolify and device secrets.
#
# Gitea and deployed apps are stopped briefly so that their databases are copied
# consistently, which keeps a restore to a single tar extract. Backups
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
readonly VOLUME_ROOT="/var/lib/docker/volumes"
readonly REMOTE_ENV="/etc/smolofis/backup.env"
readonly PUSH_KEY="/etc/smolofis/identity/backup_ed25519"

log() { printf '[smolofis-backup] %s\n' "$*"; }
die() { printf '[smolofis-backup] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "must run as root (use sudo)"

# Running containers deployed through Coolify, not its proxy or sentinel.
app_containers() {
    local container project
    docker ps -q --filter label=coolify.managed=true | while read -r container; do
        project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "${container}")"
        if [[ -n "${project}" && "${project}" != "coolify-proxy" ]]; then
            echo "${container}"
        fi
    done
}

start_apps() {
    if [[ -n "${STOPPED_APPS:-}" ]]; then
        # shellcheck disable=SC2086
        docker start ${STOPPED_APPS} >/dev/null || log "could not restart every app"
    fi
    STOPPED_APPS=""
}

archive_app_volumes() {
    local output="$1" volumes
    STOPPED_APPS="$(app_containers | tr '\n' ' ')"
    [[ -n "${STOPPED_APPS// /}" ]] || return 0
    # shellcheck disable=SC2086
    volumes="$(docker inspect -f '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\n"}}{{end}}{{end}}' ${STOPPED_APPS} | sort -u | grep . || true)"
    if [[ -z "${volumes}" ]]; then
        STOPPED_APPS=""
        return 0
    fi
    log "archiving deployed apps' data (stopped briefly)"
    # shellcheck disable=SC2086
    docker stop ${STOPPED_APPS} >/dev/null
    # shellcheck disable=SC2086
    tar -czf "${output}" -C "${VOLUME_ROOT}" ${volumes}
    start_apps
}

restore_app_volumes() {
    local archive="$1" volume
    [[ -f "${archive}" ]] || return 0
    log "restoring deployed apps' data"
    STOPPED_APPS="$(app_containers | tr '\n' ' ')"
    if [[ -n "${STOPPED_APPS// /}" ]]; then
        # shellcheck disable=SC2086
        docker stop ${STOPPED_APPS} >/dev/null
    fi
    for volume in $(tar -tzf "${archive}" | cut -d/ -f1 | sort -u); do
        docker volume create "${volume}" >/dev/null
        rm -rf "${VOLUME_ROOT:?}/${volume}/_data"
    done
    tar -xzf "${archive}" -C "${VOLUME_ROOT}"
    start_apps
}

run_backup() {
    local name partial
    name="$(date -u +%Y%m%dT%H%M%SZ)"
    partial="${BACKUP_ROOT}/.partial-${name}"
    install -d -m 0700 "${BACKUP_ROOT}" "${partial}"
    STOPPED_APPS=""
    trap 'start_apps; docker start smolofis-gitea >/dev/null 2>&1 || true; rm -rf "${partial}"' EXIT

    log "dumping the Coolify database"
    docker exec smolofis-coolify-db pg_dump -U coolify -Fc coolify > "${partial}/coolify-db.dump"

    log "archiving Coolify files and device configuration"
    tar -czf "${partial}/coolify-files.tar.gz" --exclude=coolify/db --exclude=coolify/redis \
        --exclude=coolify/backups -C "${STATE_ROOT}" coolify
    tar -czf "${partial}/config.tar.gz" -C / etc/smolofis

    archive_app_volumes "${partial}/app-volumes.tar.gz"

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

    restore_app_volumes "${source}/app-volumes.tar.gz"

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
