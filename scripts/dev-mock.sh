#!/usr/bin/env bash
#
# dev-mock.sh — run the smolofis-panel dashboard locally against mock services.
#
# Spins up a real Gitea and a faked Coolify health endpoint via docker
# compose, then launches the panel with `cargo run` pointed at them on an
# unprivileged port. Watch the dashboard walk Initializing -> Ready as the
# containers come up; `docker stop smolofis-dev-gitea` demonstrates Degraded.
#
# Usage:
#   scripts/dev-mock.sh           # start mocks + run the panel (Ctrl-C stops)
#   scripts/dev-mock.sh down      # tear down the mock stack

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
COMPOSE_FILE="${SCRIPT_DIR}/dev/docker-compose.dev.yml"

log() { printf '\033[1;32m[smolofis-dev]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[smolofis-dev] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is required"
command -v cargo  >/dev/null 2>&1 || die "rust toolchain (cargo) is required"
docker info >/dev/null 2>&1       || die "docker daemon is not running"

if [[ "${1:-}" == "down" ]]; then
    log "tearing down mock stack"
    docker compose -f "${COMPOSE_FILE}" down --volumes
    exit 0
fi

log "starting mock services (gitea + coolify health mock)"
docker compose -f "${COMPOSE_FILE}" up --detach

cleanup() {
    log "stopping mock stack (volumes preserved; 'scripts/dev-mock.sh down' wipes them)"
    docker compose -f "${COMPOSE_FILE}" stop
}
trap cleanup EXIT

log "launching smolofis-panel on http://127.0.0.1:8080 (Ctrl-C to stop)"
SMOLOFIS_BIND="127.0.0.1:8080" \
SMOLOFIS_GITEA_URL="http://127.0.0.1:3000" \
SMOLOFIS_COOLIFY_URL="http://127.0.0.1:8000" \
SMOLOFIS_POLL_INTERVAL_SECS=2 \
RUST_LOG="info" \
    cargo run --manifest-path "${REPO_ROOT}/src-dashboard/Cargo.toml"
