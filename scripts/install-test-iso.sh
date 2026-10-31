#!/usr/bin/env bash
#
# install-test-iso.sh — prove a SmolOfis ISO installs and survives reboots.
#
# Unlike smoke-test-iso.sh, this boots through the firmware and GRUB exactly as
# hardware does, then drives the appliance over SSH:
#
#   1. boot the ISO, change the expired default password, run smolofis-install
#   2. boot the disk with the ISO removed, change the password again
#   3. reboot, then assert the password and SSH host key both persisted, and
#      that Coolify, the Gitea runner and Traefik routing all work
#
# Usage:
#   scripts/install-test-iso.sh --iso dist/smolofis-<ver>-amd64.iso [options]
#
# Options:
#   --iso PATH        ISO image to install           (required)
#   --firmware NAME   uefi or bios                   (default: uefi)
#   --timeout N       seconds to wait for each boot  (default: 600)
#   --mem N           guest RAM in MiB               (default: 6144)
#   --smp N           guest vCPUs                    (default: 2)
#   --ssh-port N      host port for guest :22        (default: 2223)
#   --panel-port N    host port for guest :8090      (default: 8651)
#   --proxy-port N    host port for guest :80        (default: 8653)

set -euo pipefail

ISO=""
FIRMWARE="uefi"
TIMEOUT=600
MEM=6144
SMP=2
SSH_PORT=2223
PANEL_PORT=8651
PROXY_PORT=8653
WORK="$(mktemp -d)"
DISK="${WORK}/disk.raw"
QEMU_PID=""

readonly DEFAULT_PASSWORD="smolofis"
readonly LIVE_PASSWORD="live-test-pw-41c7"
readonly FINAL_PASSWORD="final-test-pw-93ae"

log() { printf '\033[1;32m[install-test]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[install-test] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iso)        ISO="$2"; shift 2 ;;
    --firmware)   FIRMWARE="$2"; shift 2 ;;
    --timeout)    TIMEOUT="$2"; shift 2 ;;
    --mem)        MEM="$2"; shift 2 ;;
    --smp)        SMP="$2"; shift 2 ;;
    --ssh-port)   SSH_PORT="$2"; shift 2 ;;
    --panel-port) PANEL_PORT="$2"; shift 2 ;;
    --proxy-port) PROXY_PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ -n "$ISO" ]] || die "--iso is required"
[[ -f "$ISO" ]] || die "ISO not found: $ISO"
[[ "$FIRMWARE" == "uefi" || "$FIRMWARE" == "bios" ]] || die "--firmware must be uefi or bios"
for tool in qemu-system-x86_64 qemu-img expect sshpass ssh ssh-keyscan ssh-keygen curl; do
  command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
done

FIRMWARE_ARGS=()
if [[ "$FIRMWARE" == "uefi" ]]; then
  OVMF_CODE=""
  for candidate in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd; do
    if [[ -f "$candidate" ]]; then
      OVMF_CODE="$candidate"
      break
    fi
  done
  [[ -n "$OVMF_CODE" ]] || die "OVMF firmware not found (apt-get install ovmf)"
  cp "${OVMF_CODE/CODE/VARS}" "${WORK}/ovmf-vars.fd"
  FIRMWARE_ARGS=(
    -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}"
    -drive "if=pflash,format=raw,file=${WORK}/ovmf-vars.fd"
  )
fi

# shellcheck disable=SC2054  # commas are part of QEMU's option syntax
ACCEL=(-machine q35,accel=tcg -cpu max)
if [[ -e /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]; then
  # shellcheck disable=SC2054
  ACCEL=(-machine q35,accel=kvm -cpu host)
fi

stop_vm() {
  if [[ -n "$QEMU_PID" ]] && kill -0 "$QEMU_PID" 2>/dev/null; then
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
  fi
  QEMU_PID=""
}

cleanup() {
  stop_vm
  rm -rf "$WORK"
}
trap cleanup EXIT

# shellcheck disable=SC2054
SSH_OPTS=(
  -p "$SSH_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o ConnectTimeout=10
  -o PreferredAuthentications=password,keyboard-interactive
)

# boot_vm <label> [extra qemu args...]
boot_vm() {
  local label="$1"; shift
  log "booting ${label} (${FIRMWARE})"
  qemu-system-x86_64 \
    "${ACCEL[@]}" \
    "${FIRMWARE_ARGS[@]}" \
    -m "$MEM" -smp "$SMP" \
    -drive "file=${DISK},format=raw,if=virtio" \
    "$@" \
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22,hostfwd=tcp:127.0.0.1:${PANEL_PORT}-:8090,hostfwd=tcp:127.0.0.1:${PROXY_PORT}-:80" \
    -device virtio-net-pci,netdev=net0 \
    -display none -serial null -monitor none \
    -no-reboot &
  QEMU_PID=$!

  local deadline=$(( SECONDS + TIMEOUT ))
  until ssh-keyscan -T 3 -p "$SSH_PORT" 127.0.0.1 2>/dev/null | grep -q .; do
    kill -0 "$QEMU_PID" 2>/dev/null || die "QEMU exited during boot: ${label}"
    (( SECONDS < deadline )) || die "timed out waiting for SSH: ${label}"
    sleep 3
  done
  log "SSH is up: ${label}"
}

host_key_fingerprint() {
  ssh-keyscan -T 5 -t ed25519 -p "$SSH_PORT" 127.0.0.1 2>/dev/null \
    | ssh-keygen -lf - | awk '{print $2}'
}

# change_expired_password <old> <new>
change_expired_password() {
  OLD="$1" NEW="$2" expect -f - -- "${SSH_OPTS[@]}" smolofis@127.0.0.1 <<'EOF' >/dev/null
set timeout 60
spawn ssh -tt {*}$argv
expect -re {[Pp]assword: ?$}
send "$env(OLD)\r"
expect {
  -re {[Cc]urrent.*[Pp]assword: ?$} { send "$env(OLD)\r"; exp_continue }
  -re {[Rr]etype [Nn]ew [Pp]assword: ?$} { send "$env(NEW)\r"; exp_continue }
  -re {[Nn]ew [Pp]assword: ?$} { send "$env(NEW)\r"; exp_continue }
  timeout { exit 1 }
  eof
}
EOF
}

# guest <password> <command>
guest() {
  SSHPASS="$1" sshpass -e ssh "${SSH_OPTS[@]}" smolofis@127.0.0.1 "$2"
}

# shutdown_vm <password>
shutdown_vm() {
  guest "$1" "sudo systemctl poweroff" || true
  local deadline=$(( SECONDS + 240 ))
  while kill -0 "$QEMU_PID" 2>/dev/null; do
    if (( SECONDS >= deadline )); then
      log "guest did not power off in time; stopping QEMU"
      break
    fi
    sleep 2
  done
  stop_vm
}

qemu-img create -f raw "$DISK" 24G >/dev/null

# ---------------------------------------------------------------------------
# Stage 1 — live boot and install
# ---------------------------------------------------------------------------
boot_vm "live ISO" -cdrom "$ISO" -boot order=d

change_expired_password "$DEFAULT_PASSWORD" "$LIVE_PASSWORD"
guest "$LIVE_PASSWORD" true || die "forced password change did not take effect on the live image"
log "PASS  default password is expired and must be changed"

live_key="$(host_key_fingerprint)"
guest "$LIVE_PASSWORD" "sudo smolofis-install --disk /dev/vda --yes" \
  || die "smolofis-install failed"
log "PASS  smolofis-install completed"

shutdown_vm "$LIVE_PASSWORD"

# ---------------------------------------------------------------------------
# Stage 2 — first boot from disk
# ---------------------------------------------------------------------------
boot_vm "installed disk (first boot)"

change_expired_password "$DEFAULT_PASSWORD" "$FINAL_PASSWORD"
installed_key="$(host_key_fingerprint)"
[[ -n "$installed_key" ]] || die "no SSH host key on the installed disk"
[[ "$installed_key" != "$live_key" ]] || die "SSH host key was shared between boots of different identity"

deadline=$(( SECONDS + 30 ))
until guest "$FINAL_PASSWORD" "sudo test -s /etc/smolofis/identity/operator-password.hash"; do
  (( SECONDS < deadline )) || die "operator password was not saved to persistent storage"
  sleep 2
done
log "PASS  operator password saved to persistent storage"

shutdown_vm "$FINAL_PASSWORD"

# ---------------------------------------------------------------------------
# Stage 3 — reboot and assert persistence
# ---------------------------------------------------------------------------
boot_vm "installed disk (second boot)"

guest "$FINAL_PASSWORD" true || die "changed password did not survive a reboot"
log "PASS  changed password survived a reboot"

[[ "$(host_key_fingerprint)" == "$installed_key" ]] || die "SSH host key changed across a reboot"
log "PASS  SSH host key survived a reboot"

guest "$FINAL_PASSWORD" "systemctl is-active systemd-timesyncd" >/dev/null \
  || die "systemd-timesyncd is not running"
log "PASS  time sync is running"

curl -fsS --max-time 10 "http://127.0.0.1:${PANEL_PORT}/healthz" >/dev/null \
  || die "panel did not answer on the installed system"
log "PASS  panel serves from the installed disk"

deadline=$(( SECONDS + TIMEOUT ))
until curl -fsS --max-time 5 "http://127.0.0.1:${PANEL_PORT}/api/state" | grep -q '"phase":"ready"'; do
  (( SECONDS < deadline )) || die "stack did not reach phase=ready on the installed system"
  sleep 5
done
log "PASS  stack is ready on the installed disk"

curl -fsS --max-time 10 -H "Host: unclaimed.example.com" "http://127.0.0.1:${PROXY_PORT}/healthz" | grep -qx ok \
  || die "Traefik did not route an unclaimed host to the panel"
log "PASS  Traefik routes port 80 to the panel"

guest "$FINAL_PASSWORD" 'docker run -d --name whoami --network coolify -l traefik.enable=true -l traefik.http.routers.whoami.entrypoints=http -l "traefik.http.routers.whoami.rule=Host(\`whoami.test\`)" traefik/whoami:v1.10' >/dev/null \
  || die "could not start the routing test app"
deadline=$(( SECONDS + 30 ))
until curl -fsS --max-time 5 -H "Host: whoami.test" "http://127.0.0.1:${PROXY_PORT}/" | grep -q '^Hostname:'; do
  (( SECONDS < deadline )) || die "Traefik did not route to an app on the coolify network"
  sleep 2
done
log "PASS  Traefik routes a Host rule to an app"

guest "$FINAL_PASSWORD" 'docker exec smolofis-coolify sh -c "ssh -i \$(ls /var/www/html/storage/app/ssh/keys/ssh_key@* | grep -v \.lock\$ | head -n 1) -o StrictHostKeyChecking=no -o BatchMode=yes root@host.docker.internal true"' \
  || die "Coolify could not reach this machine over SSH"
log "PASS  Coolify can manage this machine over SSH"

deadline=$(( SECONDS + 120 ))
until guest "$FINAL_PASSWORD" 'docker exec smolofis-gitea-runner test -s /data/.runner'; do
  (( SECONDS < deadline )) || die "Gitea runner did not register"
  sleep 5
done
log "PASS  Gitea runner registered itself"

log "install test PASSED (${FIRMWARE})"
