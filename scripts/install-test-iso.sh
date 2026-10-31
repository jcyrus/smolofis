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
#      that Coolify, the Gitea runner, Traefik routing and backups all work
#   4. boot the ISO again and run smolofis-install --upgrade
#   5. boot the disk and assert that the upgrade kept the data
#   6. apply a signed A/B update, then prove rollback from a corrupted slot
#
# Usage:
#   scripts/install-test-iso.sh --iso dist/smolofis-<ver>-amd64.iso [options]
#
# Options:
#   --iso PATH        ISO image to test              (required)
#   --base-iso PATH   install this first, then upgrade to --iso
#   --firmware NAME   uefi or bios                   (default: uefi)
#   --timeout N       seconds to wait for each boot  (default: 600)
#   --mem N           guest RAM in MiB               (default: 6144)
#   --smp N           guest vCPUs                    (default: 2)
#   --ssh-port N      host port for guest :22        (default: 2223)
#   --panel-port N    host port for guest :8090      (default: 8651)
#   --proxy-port N    host port for guest :80        (default: 8653)

set -euo pipefail

ISO=""
BASE_ISO=""
FIRMWARE="uefi"
TIMEOUT=600
MEM=6144
SMP=2
SSH_PORT=2223
PANEL_PORT=8651
PROXY_PORT=8653
WORK="$(mktemp -d)"
DISK="${WORK}/disk.raw"
SERIAL_LOG="${WORK}/serial.log"
QEMU_PID=""

readonly DEFAULT_PASSWORD="smolofis"
readonly LIVE_PASSWORD="live-test-pw-41c7"
readonly FINAL_PASSWORD="final-test-pw-93ae"

log() { printf '\033[1;32m[install-test]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[install-test] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iso)        ISO="$2"; shift 2 ;;
    --base-iso)   BASE_ISO="$2"; shift 2 ;;
    --firmware)   FIRMWARE="$2"; shift 2 ;;
    --timeout)    TIMEOUT="$2"; shift 2 ;;
    --mem)        MEM="$2"; shift 2 ;;
    --smp)        SMP="$2"; shift 2 ;;
    --ssh-port)   SSH_PORT="$2"; shift 2 ;;
    --panel-port) PANEL_PORT="$2"; shift 2 ;;
    --proxy-port) PROXY_PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ -n "$ISO" ]] || die "--iso is required"
[[ -f "$ISO" ]] || die "ISO not found: $ISO"
[[ "$FIRMWARE" == "uefi" || "$FIRMWARE" == "bios" ]] || die "--firmware must be uefi or bios"
for tool in qemu-system-x86_64 qemu-img expect sshpass ssh scp ssh-keyscan ssh-keygen sha256sum curl; do
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

REBOOT_ARGS=(-no-reboot)

# shellcheck disable=SC2054
SSH_OPTS=(
  -p "$SSH_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o ConnectTimeout=10
  -o PreferredAuthentications=password,keyboard-interactive
)

boot_failed() {
  echo "----- last 40 lines of guest serial console -----"
  tail -n 40 "$SERIAL_LOG" 2>/dev/null || true
  echo "-------------------------------------------------"
  die "$1"
}

# boot_vm <label> [extra qemu args...]
boot_vm() {
  local label="$1"; shift
  log "booting ${label} (${FIRMWARE})"
  qemu-system-x86_64 \
    "${ACCEL[@]}" \
    "${FIRMWARE_ARGS[@]}" \
    -m "$MEM" -smp "$SMP" \
    -drive "file=${DISK},format=raw,if=none,id=disk0" \
    -device virtio-blk-pci,drive=disk0,bootindex=1 \
    "$@" \
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22,hostfwd=tcp:127.0.0.1:${PANEL_PORT}-:8090,hostfwd=tcp:127.0.0.1:${PROXY_PORT}-:80" \
    -device virtio-net-pci,netdev=net0 \
    -display none -serial "file:${SERIAL_LOG}" -monitor none \
    "${REBOOT_ARGS[@]}" &
  QEMU_PID=$!

  local deadline=$(( SECONDS + TIMEOUT ))
  until ssh-keyscan -T 3 -p "$SSH_PORT" 127.0.0.1 2>/dev/null | grep -q .; do
    kill -0 "$QEMU_PID" 2>/dev/null || boot_failed "QEMU exited during boot: ${label}"
    (( SECONDS < deadline )) || boot_failed "timed out waiting for SSH: ${label}"
    sleep 3
  done
  log "SSH is up: ${label}"
}

host_key_fingerprint() {
  ssh-keyscan -T 5 -t ed25519 -p "$SSH_PORT" 127.0.0.1 2>/dev/null \
    | ssh-keygen -lf - | awk '{print $2}'
}

# expired_password_dialog <old> <new>
expired_password_dialog() {
  OLD="$1" NEW="$2" expect -f - -- "${SSH_OPTS[@]}" smolofis@127.0.0.1 <<'EOF' >/dev/null
set timeout 20
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

# sshd opens before logins are allowed, so a login right after boot can be
# refused. wait_login <password>
wait_login() {
  local deadline=$(( SECONDS + 60 ))
  until guest "$1" true 2>/dev/null; do
    (( SECONDS < deadline )) || return 1
    sleep 3
  done
}

# change_expired_password <old> <new>
change_expired_password() {
  local deadline=$(( SECONDS + 90 ))
  until guest "$2" true 2>/dev/null; do
    (( SECONDS < deadline )) || die "could not change the expired password"
    expired_password_dialog "$1" "$2" || true
    sleep 3
  done
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

# An explicit boot index: on the upgrade boot, the firmware must pick the USB
# image over the installed disk.
# shellcheck disable=SC2054
ISO_ARGS=(-drive "file=${ISO},if=none,id=usb,media=cdrom,readonly=on" -device ide-cd,drive=usb,bootindex=0)

# shellcheck disable=SC2054
BASE_ISO_ARGS=(-drive "file=${BASE_ISO:-$ISO},if=none,id=usb,media=cdrom,readonly=on" -device ide-cd,drive=usb,bootindex=0)

assert_lockdown() {
  local deadline
  deadline=$(( SECONDS + 300 ))
  until guest "$FINAL_PASSWORD" "sudo nft list table inet smolofis" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      diagnose_guest
      die "lockdown did not complete"
    fi
    sleep 5
  done
  if curl -fsS --max-time 5 "http://127.0.0.1:${PANEL_PORT}/healthz" >/dev/null 2>&1; then
    die "port 8090 is still reachable from the LAN"
  fi
  log "PASS  port 8090 is closed to the LAN"

  # The firewall closes first, so the accounts can land a little later.
  deadline=$(( SECONDS + 300 ))
  until guest "$FINAL_PASSWORD" "sudo sh -c '. /etc/smolofis/admin.env; curl -fsS -u \"\$SMOLOFIS_ADMIN_USER:\$GITEA_ADMIN_PASSWORD\" http://127.0.0.1:3000/api/v1/user'" 2>/dev/null \
    | grep -q '"is_admin":true'; do
    if (( SECONDS >= deadline )); then
      diagnose_guest
      die "Gitea admin login failed"
    fi
    sleep 5
  done
  log "PASS  Gitea admin account works"

  deadline=$(( SECONDS + 300 ))
  until [[ "$(guest "$FINAL_PASSWORD" "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/register")" != "200" ]]; do
    if (( SECONDS >= deadline )); then
      diagnose_guest
      die "Coolify registration is still open"
    fi
    sleep 5
  done
  log "PASS  Coolify registration is closed"

  guest "$FINAL_PASSWORD" "sudo smolofis-admin show" | grep -q '^Coolify' || die "smolofis-admin show failed"
  guest "$FINAL_PASSWORD" "id smolofis-panel" | grep -qv docker || die "panel user is still in the docker group"
  log "PASS  panel has no Docker access"
}

diagnose_guest() {
  echo "----- guest diagnostics -----"
  guest "$FINAL_PASSWORD" "ls -la /data/coolify/proxy/dynamic; docker ps -a; \
    sudo journalctl -b --no-pager -n 60 -u smolofis-infrastructure -u smolofis-lockdown; \
    docker logs --tail 20 coolify-proxy" 2>&1 || true
  echo "-----------------------------"
}

qemu-img create -f raw "$DISK" 24G >/dev/null

# ---------------------------------------------------------------------------
# Stage 1 — live boot and install
# ---------------------------------------------------------------------------
boot_vm "live ISO" "${BASE_ISO_ARGS[@]}"

change_expired_password "$DEFAULT_PASSWORD" "$LIVE_PASSWORD"
wait_login "$LIVE_PASSWORD" || die "forced password change did not take effect on the live image"
log "PASS  default password is expired and must be changed"

deadline=$(( SECONDS + 60 ))
until curl -fsS --max-time 5 "http://127.0.0.1:${PANEL_PORT}/api/state" | grep -q '"phase":"live"'; do
  (( SECONDS < deadline )) || die "panel did not report live mode without a data partition"
  sleep 3
done
log "PASS  panel reports live mode on the USB image"

if guest "$LIVE_PASSWORD" "sudo smolofis-install --disk /dev/sr0 --yes" 2>/dev/null; then
  die "smolofis-install accepted a device that is not a whole disk"
fi
log "PASS  installer refuses the boot media"

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

wait_login "$FINAL_PASSWORD" || die "changed password did not survive a reboot"
log "PASS  changed password survived a reboot"

[[ "$(host_key_fingerprint)" == "$installed_key" ]] || die "SSH host key changed across a reboot"
log "PASS  SSH host key survived a reboot"


guest "$FINAL_PASSWORD" "systemctl is-active systemd-timesyncd" >/dev/null \
  || die "systemd-timesyncd is not running"
log "PASS  time sync is running"

deadline=$(( SECONDS + TIMEOUT ))
until curl -fsS --max-time 5 "http://127.0.0.1:${PROXY_PORT}/api/state" | grep -q '"phase":"ready"'; do
  (( SECONDS < deadline )) || die "stack did not reach phase=ready on the installed system"
  sleep 5
done
log "PASS  stack is ready on the installed disk"

curl -fsS --max-time 5 "http://127.0.0.1:${PROXY_PORT}/api/state" | grep -q '"persistent":true' \
  || die "panel did not detect the data partition"
log "PASS  panel reads the data partition"

guest "$FINAL_PASSWORD" "docker inspect smolofis-gitea --format '{{.HostConfig.LogConfig.Config}}'" | grep -q 'max-size:10m' \
  || die "container log rotation is not configured"
log "PASS  container logs are rotated"

guest "$FINAL_PASSWORD" "sudo ip addr add 100.64.0.1/32 dev tailscale0" \
  || die "could not assign a test address to tailscale0"
deadline=$(( SECONDS + 30 ))
until curl -fsS --max-time 5 "http://127.0.0.1:${PROXY_PORT}/api/state" | grep -q '"tailscale_ip":"100.64.0.1"'; do
  (( SECONDS < deadline )) || die "panel did not report the tailscale0 address"
  sleep 3
done
log "PASS  panel reads the Tailscale address"

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

# A --base-iso build is an older release, so its lockdown is not under test.
if [[ -z "$BASE_ISO" ]]; then
  assert_lockdown
fi

# ---------------------------------------------------------------------------
# Backup and restore
# ---------------------------------------------------------------------------
gitea_api() {
  guest "$FINAL_PASSWORD" "sudo sh -c '. /etc/smolofis/admin.env; curl -fsS -u \"\$SMOLOFIS_ADMIN_USER:\$GITEA_ADMIN_PASSWORD\" -H content-type:application/json $1'"
}

wait_gitea() {
  local deadline=$(( SECONDS + 180 ))
  until guest "$FINAL_PASSWORD" "curl -fsS http://127.0.0.1:3000/api/healthz" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || die "Gitea did not come back"
    sleep 3
  done
}

gitea_api "-d {\\\"name\\\":\\\"keepme\\\"} http://127.0.0.1:3000/api/v1/user/repos" >/dev/null \
  || die "could not create the test repository"
guest "$FINAL_PASSWORD" "sudo smolofis-backup run" >/dev/null || die "backup failed"
wait_gitea
backup="$(guest "$FINAL_PASSWORD" "sudo smolofis-backup list" | tail -n 1)"
[[ -n "$backup" ]] || die "backup was not listed"
log "PASS  backup created: ${backup}"


gitea_api "-X DELETE http://127.0.0.1:3000/api/v1/repos/smolofis/keepme" >/dev/null \
  || die "could not delete the test repository"
guest "$FINAL_PASSWORD" "sudo smolofis-backup restore ${backup} --yes" >/dev/null || die "restore failed"
wait_gitea
gitea_api "http://127.0.0.1:3000/api/v1/repos/smolofis/keepme" >/dev/null \
  || die "restore did not bring the repository back"
log "PASS  restore brought a deleted repository back"

deadline=$(( SECONDS + 300 ))
until curl -fsS --max-time 5 "http://127.0.0.1:${PROXY_PORT}/api/state" | grep -q '"phase":"ready"'; do
  (( SECONDS < deadline )) || die "stack did not recover after the restore"
  sleep 5
done
log "PASS  stack recovered after the restore"

shutdown_vm "$FINAL_PASSWORD"

# ---------------------------------------------------------------------------
# Stage 4 — upgrade from the USB image
# ---------------------------------------------------------------------------
boot_vm "live ISO (upgrade)" "${ISO_ARGS[@]}"

change_expired_password "$DEFAULT_PASSWORD" "$LIVE_PASSWORD"
guest "$LIVE_PASSWORD" "grep -q '<port>8090</port>' /etc/avahi/services/smolofis-panel.service" \
  || die "the USB image does not advertise the panel on port 8090"
log "PASS  USB image advertises port 8090 over mDNS"
guest "$LIVE_PASSWORD" "sudo smolofis-install --upgrade --disk /dev/vda --yes" \
  || die "smolofis-install --upgrade failed"
log "PASS  upgrade completed from the USB image"

shutdown_vm "$LIVE_PASSWORD"

# ---------------------------------------------------------------------------
# Stage 5 — the upgraded disk kept its data
# ---------------------------------------------------------------------------
boot_vm "installed disk (after upgrade)"

wait_login "$FINAL_PASSWORD" || die "the password did not survive the upgrade"
[[ "$(host_key_fingerprint)" == "$installed_key" ]] || die "the SSH host key did not survive the upgrade"
log "PASS  password and SSH host key survived the upgrade"

wait_gitea
gitea_api "http://127.0.0.1:3000/api/v1/repos/smolofis/keepme" >/dev/null \
  || die "the repository did not survive the upgrade"
log "PASS  repository survived the upgrade"

# ---------------------------------------------------------------------------
# Stage 6 — signed A/B update and rollback
# ---------------------------------------------------------------------------
wait_ready() {
  local deadline=$(( SECONDS + TIMEOUT ))
  until curl -fsS --max-time 5 "http://127.0.0.1:${PROXY_PORT}/api/state" | grep -q '"phase":"ready"'; do
    if (( SECONDS >= deadline )); then
      diagnose_guest
      die "stack did not reach phase=ready: $1"
    fi
    sleep 5
  done
}

slot_status() { guest "$FINAL_PASSWORD" "sudo smolofis-update status" | awk -v k="$1" '$1 == k { print $2 }'; }

# sshd opens before logins are allowed, so an early read can come back empty.
booted_slot() {
  local deadline=$(( SECONDS + 60 )) slot=""
  until [[ -n "$slot" ]]; do
    (( SECONDS < deadline )) || die "could not read the booted slot"
    slot="$(slot_status booted || true)"
    [[ -n "$slot" ]] || sleep 3
  done
  echo "$slot"
}

wait_ready "before the A/B update"

# These run after the upgrade because --base-iso may predate them.
assert_lockdown

[[ "$(guest "$FINAL_PASSWORD" "systemctl show -p RuntimeWatchdogUSec --value")" == "30s" ]] \
  || die "the runtime watchdog is not active"
guest "$FINAL_PASSWORD" "test -e /dev/watchdog0" || die "no watchdog device"
log "PASS  hang watchdog is active"

guest "$FINAL_PASSWORD" "sudo smolofis-backup push-key | install -D -m 0600 /dev/stdin ~/.ssh/authorized_keys" \
  || die "could not authorize the backup key"
guest "$FINAL_PASSWORD" "echo BACKUP_REMOTE=smolofis@127.0.0.1:/home/smolofis/backup-copy | sudo tee /etc/smolofis/backup.env >/dev/null; mkdir -p /home/smolofis/backup-copy; sudo smolofis-backup push" >/dev/null \
  || die "smolofis-backup push failed"
guest "$FINAL_PASSWORD" "test -s /home/smolofis/backup-copy/${backup}/gitea.tar.gz; rc=\$?; sudo rm -f /etc/smolofis/backup.env; exit \$rc" \
  || die "the backup did not reach the remote"
log "PASS  backup copied off the machine"

guest "$FINAL_PASSWORD" "docker pull -q hello-world:latest >/dev/null && echo hello-world:latest | sudo tee -a /var/lib/smolofis/.images-shipped >/dev/null && sudo smolofis-admin provision >/dev/null" \
  || die "could not rerun provisioning"
if guest "$FINAL_PASSWORD" "docker image inspect hello-world:latest" >/dev/null 2>&1; then
  die "an image from an earlier version was not removed"
fi
guest "$FINAL_PASSWORD" "docker image inspect traefik/whoami:v1.10" >/dev/null 2>&1 \
  || die "pruning removed an image SmolOfis did not ship"
log "PASS  old shipped images are pruned, and other images are kept"

BUNDLE="${WORK}/bundle"
mkdir -p "$BUNDLE"
ssh-keygen -q -t ed25519 -N '' -C test -f "${WORK}/test-key"
printf 'smolofis-release %s\n' "$(cut -d' ' -f1,2 "${WORK}/test-key.pub")" > "${BUNDLE}/signers"
cat > "${BUNDLE}/smolofis-manifest.txt" <<EOF
VERSION=9999.0.0
ISO=update.iso
SHA256=$(sha256sum "$ISO" | cut -d' ' -f1)
EOF
ssh-keygen -q -Y sign -f "${WORK}/test-key" -n smolofis-release "${BUNDLE}/smolofis-manifest.txt"
ln "$ISO" "${BUNDLE}/update.iso" 2>/dev/null || cp "$ISO" "${BUNDLE}/update.iso"

guest "$FINAL_PASSWORD" "sudo install -d -o smolofis /var/lib/smolofis/update-test"
SSHPASS="$FINAL_PASSWORD" sshpass -e scp -P "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o LogLevel=ERROR "${BUNDLE}"/* "smolofis@127.0.0.1:/var/lib/smolofis/update-test/"
readonly APPLY="sudo smolofis-update apply --from /var/lib/smolofis/update-test --signers /var/lib/smolofis/update-test/signers --no-reboot"

guest "$FINAL_PASSWORD" "cp /var/lib/smolofis/update-test/smolofis-manifest.txt /tmp/manifest && sed -i s/9999.0.0/9999.0.1/ /var/lib/smolofis/update-test/smolofis-manifest.txt"
if guest "$FINAL_PASSWORD" "$APPLY" 2>/dev/null; then
  die "a tampered manifest was accepted"
fi
guest "$FINAL_PASSWORD" "cp /tmp/manifest /var/lib/smolofis/update-test/smolofis-manifest.txt"
log "PASS  tampered manifest rejected"

if guest "$FINAL_PASSWORD" "sudo smolofis-update apply --from /var/lib/smolofis/update-test --no-reboot" 2>/dev/null; then
  die "a manifest from an untrusted key was accepted"
fi
log "PASS  untrusted signing key rejected"

guest "$FINAL_PASSWORD" "$APPLY" >/dev/null || die "smolofis-update apply failed"
shutdown_vm "$FINAL_PASSWORD"

boot_vm "updated slot"
slot="$(booted_slot)"
[[ "$slot" == "system-b" ]] || die "the update booted ${slot}, not system-b"
wait_ready "after the A/B update"
deadline=$(( SECONDS + 120 ))
until [[ "$(slot_status active)" == "system-b" ]]; do
  (( SECONDS < deadline )) || die "system-b was not promoted"
  sleep 5
done
log "PASS  update booted and was promoted to the active slot"

guest "$FINAL_PASSWORD" "$APPLY" >/dev/null || die "second smolofis-update apply failed"
guest "$FINAL_PASSWORD" "sudo mount -o remount,rw /run/live/medium && sudo truncate -s 4096 /run/live/medium/system-a/filesystem.squashfs && sync" \
  || die "could not corrupt system-a"
shutdown_vm "$FINAL_PASSWORD"

# The failed slot panics and reboots, so this boot must allow a reset.
REBOOT_ARGS=()
boot_vm "corrupted slot, expecting rollback"
slot="$(booted_slot)"
[[ "$slot" == "system-b" ]] || die "rollback booted ${slot}, not system-b"
log "PASS  a failed update rolled back to the previous slot"

bundle="$(guest "$FINAL_PASSWORD" "sudo smolofis-support" | tail -n 1)"
guest "$FINAL_PASSWORD" "sudo sh -c '. /etc/smolofis/admin.env; . /etc/smolofis/runner.env; mkdir /tmp/support && tar -xzf ${bundle} -C /tmp/support && test -s /tmp/support/journal.txt && ! grep -rqF -e \"\$GITEA_ADMIN_PASSWORD\" -e \"\$COOLIFY_ADMIN_PASSWORD\" -e \"\$GITEA_RUNNER_REGISTRATION_TOKEN\" /tmp/support'" \
  || die "support bundle is missing data or contains a secret"
log "PASS  support bundle works and contains no secrets"

log "install test PASSED (${FIRMWARE})"
