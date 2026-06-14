#!/usr/bin/env bash
#
# smoke-test-iso.sh — boot a SmolOfis ISO headless in QEMU and prove the
# control panel actually comes up and serves traffic.
#
# Strategy: instead of driving the GRUB menu interactively, extract the kernel
# and initrd from the ISO with xorriso and direct-boot them, attaching the ISO
# itself as a CD-ROM so live-boot finds the squashfs. A console=ttyS0 cmdline
# streams the whole boot to a serial log, and QEMU user-mode networking
# forwards a host port to the panel's port 80 so curl can probe it from
# outside the guest.
#
# The Gitea + Coolify compose stack is masked for the test: pulling ~2 GB of
# images is out of scope for a boot smoke test and would make CI slow and
# flaky. The panel binds port 80 independently of that stack, which is exactly
# the "reachable within seconds of boot" guarantee we want to prove.
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
#   --port N         host port forwarded to guest :80  (default: 8650)
#   --timeout N      seconds to wait for the panel     (default: 600)
#   --mem N          guest RAM in MiB                   (default: 2560)
#   --smp N          guest vCPUs                        (default: 2)
#
# Environment:
#   SERIAL_LOG       path for the captured serial console (default: smoke-serial.log)

set -euo pipefail

ISO=""
HOST_PORT=8650
TIMEOUT=600
MEM=2560
SMP=2
WORK="$(mktemp -d)"
SERIAL_LOG="${SERIAL_LOG:-smoke-serial.log}"

log()  { printf '\033[1;32m[smoke]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[smoke]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[smoke] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iso)     ISO="$2"; shift 2 ;;
    --port)    HOST_PORT="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --mem)     MEM="$2"; shift 2 ;;
    --smp)     SMP="$2"; shift 2 ;;
    -h|--help) sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
# serial log; mask the heavyweight image-pulling unit (see header).
CMDLINE="boot=live components hostname=smolofis console=ttyS0,115200 systemd.mask=smolofis-infrastructure.service"

log "booting ISO (mem=${MEM}M smp=${SMP}; host port ${HOST_PORT} -> guest :80)"
: > "$SERIAL_LOG"
qemu-system-x86_64 \
  "${ACCEL[@]}" \
  -m "$MEM" -smp "$SMP" \
  -kernel "$KERNEL" -initrd "$INITRD" -append "$CMDLINE" \
  -cdrom "$ISO" \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${HOST_PORT}-:80" \
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
  initializing|ready|degraded)
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

if (( fail == 0 )); then
  log "ISO smoke test PASSED"
else
  die "ISO smoke test FAILED (${fail} check(s) failed)"
fi
