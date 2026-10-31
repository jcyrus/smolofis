#!/usr/bin/env bash
#
# smolofis-firstboot — idempotent per-device initialization.
#
# Installed into the image as /usr/local/bin/smolofis-firstboot and executed by
# smolofis-infrastructure.service (ExecStartPre) on every boot. It creates the
# persistent storage tracks under /var/lib/smolofis/ and generates the secrets
# the Coolify stack needs exactly once per device, so flashed images never
# share credentials.

set -euo pipefail

readonly STATE_ROOT="/var/lib/smolofis"
readonly CONF_ROOT="/etc/smolofis"
readonly SECRETS_FILE="${CONF_ROOT}/secrets.env"

log() { printf '[smolofis-firstboot] %s\n' "$*"; }

rand_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
rand_b64() { head -c "$1" /dev/urandom | base64 | tr -d '\n'; }

# ---------------------------------------------------------------------------
# Storage auto-detection & persistent tracks
# ---------------------------------------------------------------------------
# If /var/lib/smolofis is currently unmounted and a partition labeled
# SMOLOFIS_DATA exists (or an unmounted persistence partition), mount it.
if ! mountpoint -q "${STATE_ROOT}" 2>/dev/null; then
    PERSIST_DEV=""
    if [[ -b /dev/disk/by-label/SMOLOFIS_DATA ]]; then
        PERSIST_DEV="/dev/disk/by-label/SMOLOFIS_DATA"
    elif [[ -b /dev/disk/by-label/persistence ]]; then
        # Only attempt direct mount if not already in use by live-boot persistence layer
        if ! grep -qsE 'persistence|live' /proc/mounts; then
            PERSIST_DEV="/dev/disk/by-label/persistence"
        fi
    fi

    if [[ -n "${PERSIST_DEV}" ]]; then
        log "mounting persistent data partition (${PERSIST_DEV}) at ${STATE_ROOT}"
        install -d -m 0755 "${STATE_ROOT}"
        mount "${PERSIST_DEV}" "${STATE_ROOT}" || log "NOTICE: partition ${PERSIST_DEV} already mounted or busy"
    else
        log "NOTICE: persistent storage already active or operating in memory mode"
    fi
fi

log "ensuring persistent storage tracks under ${STATE_ROOT}"
install -d -m 0755 "${STATE_ROOT}"

# Gitea runs as UID 1000:GID 1000 inside the container.
install -d -m 0750 "${STATE_ROOT}/gitea"
chown -R 1000:1000 "${STATE_ROOT}/gitea"

# Gitea Actions Runner persistent session data.
install -d -m 0755 "${STATE_ROOT}/gitea-runner"
if [[ ! -s "${STATE_ROOT}/gitea-runner/config.yaml" ]]; then
    cat > "${STATE_ROOT}/gitea-runner/config.yaml" <<'EOF'
runner:
  file: .runner
container:
  options: --add-host=smolofis.local:host-gateway
EOF
fi

# Coolify uses UID 9999 (www-data) and GID 0 (root).
install -d -m 0750 "${STATE_ROOT}/coolify"
install -d -m 0700 "${STATE_ROOT}/coolify/db"
install -d -m 0700 "${STATE_ROOT}/coolify/ssh" "${STATE_ROOT}/coolify/ssh/keys" "${STATE_ROOT}/coolify/ssh/mux"
install -d -m 0750 "${STATE_ROOT}/coolify/applications"
install -d -m 0750 "${STATE_ROOT}/coolify/databases"
install -d -m 0750 "${STATE_ROOT}/coolify/services"
install -d -m 0750 "${STATE_ROOT}/coolify/backups"
install -d -m 0750 "${STATE_ROOT}/coolify/images"
install -d -m 0750 "${STATE_ROOT}/coolify/redis"
install -d -m 0750 "${STATE_ROOT}/coolify/proxy" "${STATE_ROOT}/coolify/proxy/dynamic"

# Coolify manages this machine as root over SSH. Its seeder imports any key
# named for host.docker.internal into its database, then keeps its own copy, so
# the master copy lives outside Coolify's directory and stays stable. /root is
# on the RAM overlay, so authorize the key on every boot.
COOLIFY_KEY="${CONF_ROOT}/identity/coolify_ed25519"
install -d -m 0700 "${CONF_ROOT}/identity"
if [[ ! -s "${COOLIFY_KEY}" ]]; then
    log "generating Coolify SSH key for this machine"
    ssh-keygen -q -t ed25519 -a 100 -N "" -C coolify -f "${COOLIFY_KEY}"
fi
install -m 0600 "${COOLIFY_KEY}" "${STATE_ROOT}/coolify/ssh/keys/id.root@host.docker.internal"
install -d -m 0700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 0600 /root/.ssh/authorized_keys
grep -qxFf "${COOLIFY_KEY}.pub" /root/.ssh/authorized_keys \
    || cat "${COOLIFY_KEY}.pub" >> /root/.ssh/authorized_keys

chown -R 9999:0 "${STATE_ROOT}/coolify/ssh" \
    "${STATE_ROOT}/coolify/applications" \
    "${STATE_ROOT}/coolify/databases" \
    "${STATE_ROOT}/coolify/services" \
    "${STATE_ROOT}/coolify/backups" \
    "${STATE_ROOT}/coolify/images"

# Ensure /data/coolify on host symlinks to persistent state
install -d -m 0755 /data
if [[ ! -e /data/coolify ]]; then
    log "linking /data/coolify to persistent storage at ${STATE_ROOT}/coolify"
    ln -s "${STATE_ROOT}/coolify" /data/coolify
fi

# Coolify reads this file on first use and manages the proxy from then on, so
# it is only seeded once and matches Coolify's default Traefik configuration.
if [[ ! -s "${STATE_ROOT}/coolify/proxy/docker-compose.yml" ]]; then
    log "provisioning Coolify proxy (Traefik on ports 80 and 443)"
    cat > "${STATE_ROOT}/coolify/proxy/docker-compose.yml" <<'EOF'
name: coolify-proxy
networks:
  coolify:
    external: true
services:
  traefik:
    container_name: coolify-proxy
    image: traefik:v3.7
    restart: unless-stopped
    extra_hosts:
      - host.docker.internal:host-gateway
    networks:
      - coolify
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    healthcheck:
      test: wget -qO- http://localhost:80/ping || exit 1
      interval: 4s
      timeout: 2s
      retries: 5
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /data/coolify/proxy:/traefik
    command:
      - --ping=true
      - --ping.entrypoint=http
      - --api.dashboard=true
      - --api.insecure=false
      - --entrypoints.http.address=:80
      - --entrypoints.https.address=:443
      - --entrypoints.http.http.encodequerysemicolons=true
      - --entryPoints.http.http2.maxConcurrentStreams=250
      - --entrypoints.https.http.encodequerysemicolons=true
      - --entryPoints.https.http2.maxConcurrentStreams=250
      - --entrypoints.https.http3
      - --providers.file.directory=/traefik/dynamic/
      - --providers.file.watch=true
      - --certificatesresolvers.letsencrypt.acme.httpchallenge=true
      - --certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=http
      - --certificatesresolvers.letsencrypt.acme.storage=/traefik/acme.json
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
    labels:
      - traefik.enable=false
      - coolify.managed=true
      - coolify.proxy=true
EOF
fi

# Lowest-priority catch-all: IPs, MagicDNS names and renamed mDNS hosts reach
# the panel, and any app with a Host rule wins. Coolify leaves this file alone.
cat > "${STATE_ROOT}/coolify/proxy/dynamic/smolofis-panel.yaml" <<'EOF'
http:
  routers:
    smolofis-panel:
      entryPoints:
        - http
      rule: PathPrefix(`/`)
      priority: 1
      service: smolofis-panel
  services:
    smolofis-panel:
      loadBalancer:
        servers:
          - url: http://host.docker.internal:8090
EOF

# ---------------------------------------------------------------------------
# Per-device secrets (generated once, then immutable)
# ---------------------------------------------------------------------------
install -d -m 0755 "${CONF_ROOT}"

# If /etc/smolofis is a fresh persistent bind mount, restore the compose definition from system image
if [[ ! -s "${CONF_ROOT}/docker-compose.yml" && -f /usr/share/smolofis/docker-compose.yml ]]; then
    log "restoring docker-compose.yml from /usr/share/smolofis/docker-compose.yml"
    cp /usr/share/smolofis/docker-compose.yml "${CONF_ROOT}/docker-compose.yml"
fi

if [[ -s "${SECRETS_FILE}" ]]; then
    log "secrets already provisioned at ${SECRETS_FILE} — leaving untouched"
else
    log "generating per-device secrets at ${SECRETS_FILE}"
    umask 077
    # compose env_file values are literal (no interpolation), so the shared
    # database password is expanded here and written to both keys.
    db_password="$(rand_hex 24)"
    cat > "${SECRETS_FILE}" <<EOF
# Generated by smolofis-firstboot on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# Unique to this device. Deleting this file resets the Coolify stack's
# credentials and will orphan the existing database.
APP_KEY=base64:$(rand_b64 32)
APP_ID=$(rand_hex 16)
DB_PASSWORD=${db_password}
POSTGRES_PASSWORD=${db_password}
REDIS_PASSWORD=
PUSHER_APP_ID=$(rand_hex 8)
PUSHER_APP_KEY=$(rand_hex 16)
PUSHER_APP_SECRET=$(rand_hex 16)
EOF
    chmod 0600 "${SECRETS_FILE}"
fi

# Shared by Gitea and its runner, separate from secrets.env so that Gitea does
# not receive the Coolify credentials.
RUNNER_ENV="${CONF_ROOT}/runner.env"
if [[ ! -s "${RUNNER_ENV}" ]]; then
    log "generating Gitea runner registration token"
    (umask 077 && printf 'GITEA_RUNNER_REGISTRATION_TOKEN=%s\n' "$(rand_hex 32)" > "${RUNNER_ENV}")
fi

# ---------------------------------------------------------------------------
# Pre-cached Docker image ingestion
# ---------------------------------------------------------------------------
CACHE_TAR="/usr/share/smolofis/core-images.tar"
LOAD_MARKER="${STATE_ROOT}/.images-loaded"

DOCKER_READY=0
for _ in {1..20}; do
    if docker info >/dev/null 2>&1; then
        DOCKER_READY=1
        break
    fi
    sleep 1
done

# Coolify, its proxy and deployed apps share this network.
if [[ ${DOCKER_READY} -eq 1 ]]; then
    docker network inspect coolify >/dev/null 2>&1 \
        || docker network create --attachable coolify >/dev/null
fi

if [[ -f "${CACHE_TAR}" && ! -f "${LOAD_MARKER}" ]]; then
    log "found pre-cached container image bundle at ${CACHE_TAR}"
    if [[ ${DOCKER_READY} -eq 1 ]]; then
        log "loading pre-cached Docker images into local container engine..."
        if docker load -i "${CACHE_TAR}"; then
            touch "${LOAD_MARKER}"
            log "pre-cached Docker images successfully loaded"
        else
            log "WARNING: docker load failed; will fall back to compose network pull"
        fi
    else
        log "WARNING: docker daemon not responding in time; will fall back to compose network pull"
    fi
fi

# ---------------------------------------------------------------------------
# Zero-touch Tailscale auto-connect (if key provided via env file)
# ---------------------------------------------------------------------------
TAILSCALE_ENV="${CONF_ROOT}/tailscale.env"
if [[ -f "${TAILSCALE_ENV}" ]]; then
    # shellcheck disable=SC1090
    source "${TAILSCALE_ENV}"
fi

if [[ -n "${TAILSCALE_AUTHKEY:-}" ]]; then
    log "found TAILSCALE_AUTHKEY; attempting automated Tailscale connection..."
    if command -v tailscale >/dev/null 2>&1; then
        tailscale up --authkey="${TAILSCALE_AUTHKEY}" --hostname="smolofis" --ssh --accept-routes 2>&1 | log || true
    fi
fi

log "first-boot initialization complete"
