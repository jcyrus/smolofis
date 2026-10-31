#!/usr/bin/env bash
#
# smolofis-admin — per-device admin accounts and LAN lockdown.
#
# Gitea and Coolify both hand their first account to whoever registers first,
# so the appliance claims them before the LAN can. The passwords live in a
# root-only file, which makes console or SSH access the proof of ownership.
#
# Usage:
#   smolofis-admin provision   # run by smolofis-infrastructure.service
#   sudo smolofis-admin show   # print the admin credentials

set -euo pipefail

readonly ADMIN_ENV="/etc/smolofis/admin.env"
readonly ADMIN_USER="smolofis"
readonly ADMIN_EMAIL="admin@smolofis.local"

log() { printf '[smolofis-admin] %s\n' "$*"; }
die() { printf '[smolofis-admin] ERROR: %s\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "must run as root (use sudo)"

rand_password() { printf '%s%s' "$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')" 'Aa1-'; }

ensure_credentials() {
    [[ -s "${ADMIN_ENV}" ]] && return 0
    log "generating admin credentials"
    (
        umask 077
        cat > "${ADMIN_ENV}" <<EOF
SMOLOFIS_ADMIN_USER=${ADMIN_USER}
SMOLOFIS_ADMIN_EMAIL=${ADMIN_EMAIL}
GITEA_ADMIN_PASSWORD=$(rand_password)
COOLIFY_ADMIN_PASSWORD=$(rand_password)
EOF
    )
}

wait_healthy() {
    local container="$1" status
    for _ in {1..120}; do
        status="$(docker inspect -f '{{.State.Health.Status}}' "${container}" 2>/dev/null || true)"
        [[ "${status}" == "healthy" ]] && return 0
        sleep 5
    done
    log "${container} did not become healthy"
    return 1
}

provision_gitea() {
    local admins
    wait_healthy smolofis-gitea || return 1
    # shellcheck disable=SC1090
    source "${ADMIN_ENV}"
    # Read it all first: under pipefail, grep -q exiting early fails the pipe.
    admins="$(docker exec -u git smolofis-gitea gitea admin user list --admin)" || return 1
    if grep -q "${ADMIN_USER}" <<<"${admins}"; then
        return 0
    fi
    log "creating the Gitea admin account"
    docker exec -u git --env-file "${ADMIN_ENV}" smolofis-gitea sh -c \
        'gitea admin user create --admin --username "$SMOLOFIS_ADMIN_USER" --email "$SMOLOFIS_ADMIN_EMAIL" --password "$GITEA_ADMIN_PASSWORD" --must-change-password=false' \
        >/dev/null
}

# Coolify's ROOT_USER_* seeder requires an email domain with DNS records, so it
# fails silently offline and leaves registration open. This mirrors the seeder
# without that check.
provision_coolify() {
    wait_healthy smolofis-coolify || return 1
    log "claiming the Coolify root account and closing registration"
    docker exec --env-file "${ADMIN_ENV}" smolofis-coolify php artisan tinker --execute='
        $user = App\Models\User::find(0);
        if (! $user) {
            $user = (new App\Models\User)->forceFill([
                "id" => 0,
                "name" => getenv("SMOLOFIS_ADMIN_USER"),
                "email" => getenv("SMOLOFIS_ADMIN_EMAIL"),
                "password" => Illuminate\Support\Facades\Hash::make(getenv("COOLIFY_ADMIN_PASSWORD")),
            ]);
            $user->save();
            $team = App\Models\Team::find(0);
            if ($team && ! $user->teams()->where("team_id", 0)->exists()) {
                $user->teams()->attach($team, ["role" => "owner"]);
            }
        }
        App\Models\InstanceSettings::updateOrCreate(["id" => 0], ["is_registration_enabled" => false]);
    ' >/dev/null
}

# Traefik reaches the panel through a Docker bridge. Rules are not persisted, so
# a boot where the stack fails leaves port 8090 open for diagnosis.
close_panel_port() {
    # Coolify recreates the proxy when it first connects, so one look can land in
    # the gap between its stop and start.
    local running=""
    for _ in {1..24}; do
        running="$(docker inspect -f '{{.State.Running}}' coolify-proxy 2>/dev/null || true)"
        [[ "${running}" == "true" ]] && break
        sleep 5
    done
    if [[ "${running}" != "true" ]]; then
        log "Traefik is not running; leaving port 8090 open"
        return 0
    fi
    log "closing port 8090 to the LAN"
    nft -f - <<'EOF'
table inet smolofis
flush table inet smolofis
table inet smolofis {
    chain input {
        type filter hook input priority filter; policy accept;
        tcp dport 8090 iifname { "lo", "docker0", "br-*" } accept
        tcp dport 8090 drop
    }
}
EOF
}

# Removes images shipped by an earlier version and nothing else: a blanket prune
# would also delete images that stopped apps still need.
prune_old_images() {
    local shipped="/usr/share/smolofis/images.txt" record="/var/lib/smolofis/.images-shipped" image
    [[ -f "${shipped}" ]] || return 0
    if [[ -f "${record}" ]]; then
        while read -r image; do
            grep -qxF "${image}" "${shipped}" && continue
            log "removing old image ${image}"
            docker image rm "${image}" >/dev/null 2>&1 || log "keeping ${image}: still in use"
        done < "${record}"
    fi
    cp "${shipped}" "${record}"
}

case "${1:-}" in
    provision)
        # The firewall goes first: a slow or failing service must not leave it open.
        close_panel_port
        ensure_credentials
        failed=0
        provision_gitea || { log "could not claim the Gitea admin account"; failed=1; }
        provision_coolify || { log "could not claim the Coolify admin account"; failed=1; }
        prune_old_images || log "could not prune old images"
        [[ ${failed} -eq 0 ]] || die "lockdown incomplete"
        log "lockdown complete"
        ;;
    show)
        [[ -s "${ADMIN_ENV}" ]] || die "no credentials yet; the stack has not finished its first start"
        # shellcheck disable=SC1090
        source "${ADMIN_ENV}"
        printf 'Gitea    (port 3000)  %s / %s\n' "${SMOLOFIS_ADMIN_USER}" "${GITEA_ADMIN_PASSWORD}"
        printf 'Coolify  (port 8000)  %s / %s\n' "${SMOLOFIS_ADMIN_EMAIL}" "${COOLIFY_ADMIN_PASSWORD}"
        ;;
    *)
        echo "usage: smolofis-admin provision|show" >&2
        exit 2
        ;;
esac
