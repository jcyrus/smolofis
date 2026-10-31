#!/usr/bin/env bash
#
# smolofis-register-runner.sh — Register act_runner with local Gitea instance.
#
# Executed by an administrator or system hook once Gitea is initialized.
# Queries Gitea for a fresh registration token and configures act_runner.

set -euo pipefail

log()  { printf '\033[1;32m[smolofis-runner]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[smolofis-runner]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[smolofis-runner] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "must run as root or with sudo"

log "checking Gitea availability..."
if ! curl -fsS http://127.0.0.1:3000/api/healthz >/dev/null 2>&1; then
    die "Gitea is not online yet (http://127.0.0.1:3000/api/healthz failed)"
fi

if [[ -s /var/lib/smolofis/gitea-runner/.runner ]]; then
    log "runner already registered at /var/lib/smolofis/gitea-runner/.runner"
    exit 0
fi

log "generating runner registration token from Gitea container..."
TOKEN=$(docker exec -u 1000 smolofis-gitea gitea actions generate-runner-token 2>/dev/null || true)

if [[ -z "${TOKEN}" ]]; then
    die "failed to generate runner token from Gitea (has the initial Gitea admin user been configured?)"
fi

log "registering runner..."
docker exec -i smolofis-gitea-runner act_runner register \
    --no-interactive \
    --instance "http://gitea:3000" \
    --token "${TOKEN}" \
    --name "smolofis-runner" \
    --labels "ubuntu-latest:docker://node:18-bullseye,ubuntu-22.04:docker://node:18-bullseye"

log "restarting smolofis-gitea-runner to start polling jobs..."
docker restart smolofis-gitea-runner >/dev/null

log "Gitea Actions runner successfully registered!"
