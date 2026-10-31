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
    die "${container} did not become healthy"
}

provision_gitea() {
    wait_healthy smolofis-gitea
    # shellcheck disable=SC1090
    source "${ADMIN_ENV}"
    if docker exec -u git smolofis-gitea gitea admin user list --admin | grep -q "${ADMIN_USER}"; then
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
    wait_healthy smolofis-coolify
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
    [[ "$(docker inspect -f '{{.State.Running}}' coolify-proxy 2>/dev/null || true)" == "true" ]] || {
        log "Traefik is not running; leaving port 8090 open"
        return 0
    }
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

case "${1:-}" in
    provision)
        ensure_credentials
        provision_gitea
        provision_coolify
        close_panel_port
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
