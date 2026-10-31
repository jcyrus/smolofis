#!/usr/bin/env bash
#
# smoke-test-iso.sh — boot a SmolOfis ISO headless in QEMU and prove the
# control panel actually comes up and serves traffic.
#
# Strategy: instead of driving the GRUB menu interactively, extract the kernel
# and initrd from the ISO with xorriso and direct-boot them, attaching the ISO
# itself as a CD-ROM so live-boot finds the squashfs. A console=ttyS0 cmdline
# streams the whole boot to a serial log, and QEMU user-mode networking
# forwards a host port to the panel's port 8090 so curl can probe it from
# outside the guest.
#
# The Gitea + Coolify compose stack is masked for the test: pulling ~2 GB of
# images is out of scope for a boot smoke test and would make CI slow and
# flaky. The panel binds port 8090 independently of that stack, which is
# exactly the "reachable within seconds of boot" guarantee we want to prove.
#
# No root required: xorriso extraction avoids a loop-mount, and the run falls
# back to TCG software emulation when /dev/kvm is absent (as on GitHub-hosted
# runners), using hardware acceleration automatically when it is available.
#
# Usage:
#   scripts/smoke-test-iso.sh --iso dist/smolofis-<ver>-amd64.iso [options]
#
# Options:
#   --iso PATH       ISO image to boot                 (required)
#   --port N         host port forwarded to guest :8090 (default: 8650)
#   --proxy-port N   host port forwarded to guest :80   (default: 8652)
#   --timeout N      seconds to wait for the panel     (default: 600)
#   --mem N          guest RAM in MiB                   (default: 2560)
#   --smp N          guest vCPUs                        (default: 2)
#   --test-stack     unmask smolofis-infrastructure and test pre-cached containers
#
# Environment:
#   SERIAL_LOG       path for the captured serial console (default: smoke-serial.log)

set -euo pipefail

ISO=""
HOST_PORT=8650
PROXY_PORT=8652
TIMEOUT=600
MEM=2560
SMP=2
TEST_STACK=0
WORK="$(mktemp -d)"
SERIAL_LOG="${SERIAL_LOG:-smoke-serial.log}"

log()  { printf '\033[1;32m[smoke]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[smoke]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[smoke] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iso)        ISO="$2"; shift 2 ;;
    --port)       HOST_PORT="$2"; shift 2 ;;
    --proxy-port) PROXY_PORT="$2"; shift 2 ;;
    --timeout)    TIMEOUT="$2"; shift 2 ;;
    --mem)        MEM="$2"; shift 2 ;;
    --smp)        SMP="$2"; shift 2 ;;
    --test-stack) TEST_STACK=1; shift ;;
    -h|--help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ -n "$ISO" ]] || die "--iso is required"
[[ -f "$ISO" ]] || die "ISO not found: $ISO"
for tool in qemu-system-x86_64 xorriso curl jq; do
  command -v "$tool" >/dev/null 2>&1 || die "missing required tool: $tool"
done

QEMU_PID=""
cleanup() {
  if [[ -n "$QEMU_PID" ]] && kill -0 "$QEMU_PID" 2>/dev/null; then
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
  fi
  if [[ -s "$SERIAL_LOG" ]]; then
    echo "----- last 40 lines of guest serial console -----"
    tail -n 40 "$SERIAL_LOG" || true
    echo "-------------------------------------------------"
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

KERNEL="${WORK}/vmlinuz"
INITRD="${WORK}/initrd.img"

log "extracting kernel + initrd from $(basename "$ISO")"
xorriso -osirrox on -indev "$ISO" \
  -extract /live/vmlinuz "$KERNEL" \
  -extract /live/initrd.img "$INITRD" >/dev/null 2>&1 \
  || die "failed to extract /live/{vmlinuz,initrd.img} from the ISO"

ACCEL=(-machine accel=tcg -cpu max)
if [[ -e /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]; then
  log "/dev/kvm is usable — booting with hardware acceleration"
  ACCEL=(-machine accel=kvm -cpu host)
else
  log "no usable /dev/kvm — falling back to TCG software emulation (slower)"
fi

# components: live-boot config; console on ttyS0 so the whole boot lands in the
# serial log; optionally unmask compose stack when --test-stack is requested.
CMDLINE="boot=live components hostname=smolofis console=ttyS0,115200"
EXTRA_QEMU_ARGS=()

if [[ ${TEST_STACK} -eq 0 ]]; then
  CMDLINE="${CMDLINE} systemd.mask=smolofis-infrastructure.service"
else
  log "unmasking smolofis-infrastructure.service for end-to-end container test"
  if (( MEM < 3584 )); then
    MEM=3584
  fi

  SCRATCH_DISK="${WORK}/persistence.raw"
  PERSIST_DIR="${WORK}/persist_root"
  if command -v qemu-img >/dev/null 2>&1 && command -v mkfs.ext4 >/dev/null 2>&1; then
    log "creating 10GB persistent storage disk for smoke test..."
    mkdir -p "${PERSIST_DIR}/var/lib/docker" \
             "${PERSIST_DIR}/var/lib/containerd" \
             "${PERSIST_DIR}/var/lib/smolofis" \
             "${PERSIST_DIR}/var/lib/tailscale" \
             "${PERSIST_DIR}/etc/smolofis" \
             "${PERSIST_DIR}/home"
    chmod 0700 "${PERSIST_DIR}/var/lib/tailscale"

    cat > "${PERSIST_DIR}/persistence.conf" <<'EOF'
/var/lib/docker bind
/var/lib/containerd bind
/var/lib/smolofis bind
/var/lib/tailscale bind
/etc/smolofis bind
/home bind
EOF
    qemu-img create -f raw "${SCRATCH_DISK}" 10G >/dev/null
    mkfs.ext4 -F -q -L "persistence" -d "${PERSIST_DIR}" "${SCRATCH_DISK}"
    EXTRA_QEMU_ARGS+=(-drive "file=${SCRATCH_DISK},format=raw,if=virtio")
    CMDLINE="${CMDLINE} persistence"
  else
    warn "qemu-img or mkfs.ext4 missing; running without persistent scratch drive"
  fi
fi

NET_FORWARDS="hostfwd=tcp:127.0.0.1:${HOST_PORT}-:8090"
if [[ ${TEST_STACK} -eq 1 ]]; then
  NET_FORWARDS="${NET_FORWARDS},hostfwd=tcp:127.0.0.1:${PROXY_PORT}-:80,hostfwd=tcp:127.0.0.1:3000-:3000,hostfwd=tcp:127.0.0.1:8000-:8000"
fi

log "booting ISO (mem=${MEM}M smp=${SMP}; host port ${HOST_PORT} -> guest :8090)"
: > "$SERIAL_LOG"
qemu-system-x86_64 \
  "${ACCEL[@]}" \
  "${EXTRA_QEMU_ARGS[@]}" \
  -m "$MEM" -smp "$SMP" \
  -kernel "$KERNEL" -initrd "$INITRD" -append "$CMDLINE" \
  -cdrom "$ISO" \
  -netdev "user,id=net0,${NET_FORWARDS}" \
  -device virtio-net-pci,netdev=net0 \
  -display none -serial "file:${SERIAL_LOG}" -monitor none \
  -no-reboot &
QEMU_PID=$!

BASE="http://127.0.0.1:${HOST_PORT}"
log "waiting up to ${TIMEOUT}s for the panel to answer on ${BASE}/healthz"
start=$SECONDS
deadline=$(( start + TIMEOUT ))
until curl -fsS --max-time 3 "${BASE}/healthz" -o /dev/null 2>/dev/null; do
  kill -0 "$QEMU_PID" 2>/dev/null || die "QEMU exited before the panel came up"
  (( SECONDS < deadline )) || die "timed out after ${TIMEOUT}s waiting for the panel"
  sleep 3
done
log "panel answered after ~$(( SECONDS - start ))s of boot"

# ---------------------------------------------------------------------------
# Assertions — run them all, then fail once with a tally so every regression
# is visible in a single run rather than one-at-a-time.
# ---------------------------------------------------------------------------
fail=0

health="$(curl -fsS --max-time 5 "${BASE}/healthz" || true)"
if [[ "$health" == "ok" ]]; then
  log "PASS  /healthz -> ok"
else
  warn "FAIL  /healthz returned: ${health:-<nothing>}"; fail=1
fi

state="$(curl -fsS --max-time 5 "${BASE}/api/state" || true)"
phase="$(jq -er '.phase' <<<"$state" 2>/dev/null || true)"
case "$phase" in
  initializing|ready|degraded|live)
    log "PASS  /api/state is valid JSON, phase=${phase}" ;;
  *)
    warn "FAIL  /api/state phase invalid or missing: ${state:0:200}"; fail=1 ;;
esac

ver="$(jq -er '.panel.version' <<<"$state" 2>/dev/null || true)"
if [[ -n "$ver" && "$ver" != "null" ]]; then
  log "PASS  panel reports version ${ver}"
else
  warn "FAIL  /api/state missing panel.version"; fail=1
fi

home="$(curl -fsS --max-time 5 "${BASE}/" || true)"
if grep -qi 'smolofis' <<<"$home"; then
  log "PASS  GET / renders the dashboard"
else
  warn "FAIL  GET / did not contain the SmolOfis dashboard"; fail=1
fi

css_code="$(curl -fsS --max-time 5 -o /dev/null -w '%{http_code}' "${BASE}/assets/app.css" || true)"
if [[ "$css_code" == "200" ]]; then
  log "PASS  embedded stylesheet served (HTTP 200)"
else
  warn "FAIL  /assets/app.css returned HTTP ${css_code}"; fail=1
fi

if [[ ${TEST_STACK} -eq 1 ]]; then
  stack_timeout=420
  log "waiting up to ${stack_timeout}s for pre-cached compose stack to reach operational phase (phase=ready)..."
  stack_start=$SECONDS
  stack_deadline=$(( stack_start + stack_timeout ))
  current_phase=""
  current_state=""
  last_report=0
  while (( SECONDS < stack_deadline )); do
    kill -0 "$QEMU_PID" 2>/dev/null || die "QEMU exited unexpectedly while waiting for compose stack"
    # Through Traefik: provisioning closes port 8090 once the stack is up.
    current_state="$(curl -fsS --max-time 3 "http://127.0.0.1:${PROXY_PORT}/api/state" 2>/dev/null || true)"
    current_phase="$(jq -er '.phase' <<<"$current_state" 2>/dev/null || true)"
    if [[ "$current_phase" == "ready" ]]; then
      break
    fi
    if (( SECONDS - last_report >= 15 )); then
      last_report=$SECONDS
      d_st="$(jq -r '.services[] | select(.id=="docker") | .health' <<<"$current_state" 2>/dev/null || echo "?")"
      g_st="$(jq -r '.services[] | select(.id=="gitea") | .health' <<<"$current_state" 2>/dev/null || echo "?")"
      c_st="$(jq -r '.services[] | select(.id=="coolify") | .health' <<<"$current_state" 2>/dev/null || echo "?")"
      log "  [+$(( SECONDS - stack_start ))s] phase=${current_phase:-unknown} (docker:${d_st} gitea:${g_st} coolify:${c_st})"
    fi
    sleep 4
  done

  if [[ "$current_phase" == "ready" ]]; then
    log "PASS  compose stack reached operational state in ~$(( SECONDS - stack_start ))s (phase=ready)"

    docker_status="$(jq -er '.services[] | select(.id=="docker") | .health' <<<"$current_state" 2>/dev/null || true)"
    gitea_status="$(jq -er '.services[] | select(.id=="gitea") | .health' <<<"$current_state" 2>/dev/null || true)"
    coolify_status="$(jq -er '.services[] | select(.id=="coolify") | .health' <<<"$current_state" 2>/dev/null || true)"

    if [[ "$docker_status" == "online" ]]; then
      log "PASS  docker engine probe -> online"
    else
      warn "FAIL  docker engine health probe returned: ${docker_status}"; fail=1
    fi

    if [[ "$gitea_status" == "online" ]]; then
      log "PASS  gitea health probe -> online"
    else
      warn "FAIL  gitea health probe returned: ${gitea_status}"; fail=1
    fi

    if [[ "$coolify_status" == "online" ]]; then
      log "PASS  coolify health probe -> online"
    else
      warn "FAIL  coolify health probe returned: ${coolify_status}"; fail=1
    fi

    # Traefik serves port 80, and an unclaimed host falls through to the panel.
    proxied="$(curl -fsS --max-time 5 -H "Host: unclaimed.example.com" "http://127.0.0.1:${PROXY_PORT}/healthz" || true)"
    if [[ "$proxied" == "ok" ]]; then
      log "PASS  Traefik on port 80 routes to the panel"
    else
      warn "FAIL  Traefik did not reach the panel: ${proxied:-<nothing>}"; fail=1
    fi
  else
    warn "FAIL  compose stack timed out waiting for ready (last phase: ${current_phase:-unknown})"
    if [[ -n "${current_state}" ]]; then
      warn "last reported state: ${current_state}"
    fi
    fail=1
  fi
fi

if (( fail == 0 )); then
  log "ISO smoke test PASSED"
else
  die "ISO smoke test FAILED (${fail} check(s) failed)"
fi
