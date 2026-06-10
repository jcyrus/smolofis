#!/usr/bin/env bash
#
# build-image.sh — SmolOfis appliance image builder.
#
# Produces a flashable, BIOS+UEFI hybrid live ISO from a minimal Debian
# rootfs via debootstrap. The resulting image boots headless, starts the
# smolofis-panel dashboard on port 80, announces itself as smolofis.local over
# mDNS, and brings up the Gitea + Coolify docker stack.
#
# Pipeline:
#   1. debootstrap a minbase Debian rootfs
#   2. install kernel, live-boot, docker-ce, avahi, network-manager
#   3. inject the compiled smolofis-panel binary + all config payloads
#   4. enable the smolofis systemd units, set hostname to "smolofis"
#   5. compress the rootfs into a squashfs
#   6. assemble a GRUB hybrid ISO with grub-mkrescue
#
# Must run as root (debootstrap + chroot). Designed for Debian/Ubuntu hosts
# and the GitHub Actions ubuntu-latest runner.
#
# Usage:
#   sudo scripts/build-image.sh --binary path/to/smolofis-panel [options]
#
# Options:
#   --binary PATH    compiled smolofis-panel binary (required)
#   --output PATH    output ISO path           (default: dist/smolofis-<ver>.iso)
#   --suite NAME     Debian suite              (default: trixie)
#   --arch ARCH      target architecture       (default: amd64)
#   --mirror URL     Debian mirror             (default: http://deb.debian.org/debian)
#   --work DIR       scratch directory         (default: ./work)
#   --version VER    image version label       (default: 0.1.0 or $SMOLOFIS_VERSION)
#   --keep-work      do not delete the scratch directory on success

set -euo pipefail

# ---------------------------------------------------------------------------
# Logging & error handling
# ---------------------------------------------------------------------------
readonly C_INFO=$'\033[1;32m' C_WARN=$'\033[1;33m' C_ERR=$'\033[1;31m' C_OFF=$'\033[0m'

log()  { printf '%s[smolofis-build]%s %s\n' "${C_INFO}" "${C_OFF}" "$*"; }
warn() { printf '%s[smolofis-build]%s %s\n' "${C_WARN}" "${C_OFF}" "$*" >&2; }
die()  { printf '%s[smolofis-build] ERROR:%s %s\n' "${C_ERR}" "${C_OFF}" "$*" >&2; exit 1; }

on_error() {
    local line=$1
    warn "build failed at line ${line}; cleaning up chroot mounts"
}
trap 'on_error ${LINENO}' ERR

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

PANEL_BINARY=""
# Debian 13 "trixie" — current stable, kernel 6.12 LTS for modern mini-PC
# hardware. Override with --suite for older targets (e.g. bookworm).
SUITE="trixie"
ARCH="amd64"
MIRROR="http://deb.debian.org/debian"
WORK_DIR="${REPO_ROOT}/work"
VERSION="${SMOLOFIS_VERSION:-0.1.0}"
OUTPUT=""
KEEP_WORK=0

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --binary)   PANEL_BINARY="$2"; shift 2 ;;
        --output)   OUTPUT="$2"; shift 2 ;;
        --suite)    SUITE="$2"; shift 2 ;;
        --arch)     ARCH="$2"; shift 2 ;;
        --mirror)   MIRROR="$2"; shift 2 ;;
        --work)     WORK_DIR="$2"; shift 2 ;;
        --version)  VERSION="$2"; shift 2 ;;
        --keep-work) KEEP_WORK=1; shift ;;
        -h|--help)  usage 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

OUTPUT="${OUTPUT:-${REPO_ROOT}/dist/smolofis-${VERSION}-${ARCH}.iso}"
ROOTFS="${WORK_DIR}/rootfs"
ISO_TREE="${WORK_DIR}/iso"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[[ ${EUID} -eq 0 ]] || die "must run as root (debootstrap + chroot required)"
[[ -n "${PANEL_BINARY}" ]] || die "--binary is required (compiled smolofis-panel)"
[[ -f "${PANEL_BINARY}" ]] || die "panel binary not found: ${PANEL_BINARY}"

for tool in debootstrap chroot mksquashfs xorriso grub-mkrescue mformat; do
    command -v "${tool}" >/dev/null 2>&1 \
        || die "missing required tool: ${tool} (apt-get install debootstrap squashfs-tools xorriso grub-pc-bin grub-efi-amd64-bin grub2-common mtools dosfstools)"
done

# Verify the binary targets Linux; a macOS Mach-O binary is a silent
# appliance-killer that would otherwise only surface at first boot.
if command -v file >/dev/null 2>&1; then
    file "${PANEL_BINARY}" | grep -qi 'ELF' \
        || die "panel binary is not a Linux ELF executable: $(file -b "${PANEL_BINARY}")"
fi

log "SmolOfis image build starting"
log "  suite=${SUITE} arch=${ARCH} version=${VERSION}"
log "  binary=${PANEL_BINARY}"
log "  output=${OUTPUT}"

# ---------------------------------------------------------------------------
# Chroot mount management
# ---------------------------------------------------------------------------
CHROOT_MOUNTED=0

mount_chroot() {
    mount -t proc  proc  "${ROOTFS}/proc"
    mount -t sysfs sysfs "${ROOTFS}/sys"
    mount --bind /dev      "${ROOTFS}/dev"
    mount --bind /dev/pts  "${ROOTFS}/dev/pts"
    CHROOT_MOUNTED=1
}

unmount_chroot() {
    [[ ${CHROOT_MOUNTED} -eq 1 ]] || return 0
    for m in dev/pts dev sys proc; do
        umount -lf "${ROOTFS}/${m}" 2>/dev/null || true
    done
    CHROOT_MOUNTED=0
}

cleanup() {
    unmount_chroot
    if [[ ${KEEP_WORK} -eq 0 && -d "${WORK_DIR}" ]]; then
        rm -rf "${WORK_DIR}"
    fi
}
trap cleanup EXIT

run_in_chroot() {
    DEBIAN_FRONTEND=noninteractive LC_ALL=C \
        chroot "${ROOTFS}" /usr/bin/env bash -euo pipefail -c "$*"
}

# ---------------------------------------------------------------------------
# Stage 1 — bootstrap minimal rootfs
# ---------------------------------------------------------------------------
log "stage 1/8: debootstrap ${SUITE}/${ARCH} (minbase)"
rm -rf "${WORK_DIR}"
mkdir -p "${ROOTFS}" "${ISO_TREE}" "$(dirname "${OUTPUT}")"

debootstrap \
    --arch="${ARCH}" \
    --variant=minbase \
    --include=ca-certificates,systemd,systemd-sysv,dbus \
    "${SUITE}" "${ROOTFS}" "${MIRROR}"

mount_chroot

# ---------------------------------------------------------------------------
# Stage 2 — apt sources & base system packages
# ---------------------------------------------------------------------------
log "stage 2/8: base packages (kernel, live-boot, networking)"

cat > "${ROOTFS}/etc/apt/sources.list" <<EOF
deb ${MIRROR} ${SUITE} main contrib non-free-firmware
deb ${MIRROR} ${SUITE}-updates main contrib non-free-firmware
deb http://security.debian.org/debian-security ${SUITE}-security main contrib non-free-firmware
EOF

run_in_chroot "apt-get update"
run_in_chroot "apt-get install -y --no-install-recommends \
    linux-image-${ARCH} \
    live-boot \
    curl \
    gnupg \
    iptables \
    network-manager \
    avahi-daemon \
    libnss-mdns \
    openssh-server \
    sudo \
    less \
    htop"

# ---------------------------------------------------------------------------
# Stage 3 — Docker Engine from the official repository
# ---------------------------------------------------------------------------
log "stage 3/8: docker-ce + compose plugin"

run_in_chroot "install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc"

cat > "${ROOTFS}/etc/apt/sources.list.d/docker.list" <<EOF
deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${SUITE} stable
EOF

run_in_chroot "apt-get update && apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-compose-plugin"

# ---------------------------------------------------------------------------
# Stage 4 — appliance identity
# ---------------------------------------------------------------------------
log "stage 4/8: hostname + mDNS identity (smolofis.local)"

echo "smolofis" > "${ROOTFS}/etc/hostname"
cat > "${ROOTFS}/etc/hosts" <<'EOF'
127.0.0.1   localhost
127.0.1.1   smolofis.local smolofis

::1         localhost ip6-localhost ip6-loopback
ff02::1     ip6-allnodes
ff02::2     ip6-allrouters
EOF

# Each flashed device must generate its own machine-id on first boot.
: > "${ROOTFS}/etc/machine-id"
rm -f "${ROOTFS}/var/lib/dbus/machine-id"

# ---------------------------------------------------------------------------
# Stage 5 — SmolOfis payload (binary, units, configs)
# ---------------------------------------------------------------------------
log "stage 5/8: injecting smolofis payload"

# Control panel binary — root-owned, world-executable, nothing else.
install -o root -g root -m 0755 "${PANEL_BINARY}" "${ROOTFS}/usr/local/bin/smolofis-panel"
install -o root -g root -m 0755 "${SCRIPT_DIR}/smolofis-firstboot.sh" "${ROOTFS}/usr/local/bin/smolofis-firstboot"

# systemd orchestration units.
install -o root -g root -m 0644 \
    "${REPO_ROOT}/config/systemd/smolofis-panel.service" \
    "${REPO_ROOT}/config/systemd/smolofis-infrastructure.service" \
    "${ROOTFS}/etc/systemd/system/"

# Application stack definition.
install -d -m 0755 "${ROOTFS}/etc/smolofis"
install -o root -g root -m 0644 \
    "${REPO_ROOT}/config/docker/docker-compose.yml" "${ROOTFS}/etc/smolofis/docker-compose.yml"

# mDNS / network discovery.
install -o root -g root -m 0644 \
    "${REPO_ROOT}/config/network/avahi-daemon.conf" "${ROOTFS}/etc/avahi/avahi-daemon.conf"
install -d -m 0755 "${ROOTFS}/etc/avahi/services"
install -o root -g root -m 0644 \
    "${REPO_ROOT}/config/network/avahi-services/smolofis-panel.service" \
    "${ROOTFS}/etc/avahi/services/smolofis-panel.service"

# NetworkManager default wired profile (NM refuses profiles wider than 0600).
install -d -m 0755 "${ROOTFS}/etc/NetworkManager/system-connections"
install -o root -g root -m 0600 \
    "${REPO_ROOT}/config/network/wired-auto.nmconnection" \
    "${ROOTFS}/etc/NetworkManager/system-connections/wired-auto.nmconnection"

# Persistent storage root (populated by smolofis-firstboot at runtime).
install -d -m 0755 "${ROOTFS}/var/lib/smolofis"

# ---------------------------------------------------------------------------
# Stage 6 — users & service enablement
# ---------------------------------------------------------------------------
log "stage 6/8: service user + unit enablement"

run_in_chroot "useradd --system --no-create-home --shell /usr/sbin/nologin smolofis-panel && \
    usermod -aG docker smolofis-panel"

# Lock the root account unless the builder explicitly provides a password
# (useful for debug images: SMOLOFIS_ROOT_PASSWORD=... sudo scripts/build-image.sh ...).
if [[ -n "${SMOLOFIS_ROOT_PASSWORD:-}" ]]; then
    warn "setting root password from SMOLOFIS_ROOT_PASSWORD (debug image)"
    echo "root:${SMOLOFIS_ROOT_PASSWORD}" | chroot "${ROOTFS}" chpasswd
else
    run_in_chroot "passwd -l root"
    log "root account locked (headless appliance; set SMOLOFIS_ROOT_PASSWORD to override)"
fi

systemctl --root="${ROOTFS}" enable \
    smolofis-panel.service \
    smolofis-infrastructure.service \
    docker.service \
    NetworkManager.service \
    avahi-daemon.service \
    ssh.service

# ---------------------------------------------------------------------------
# Stage 7 — slim down the rootfs
# ---------------------------------------------------------------------------
log "stage 7/8: cleaning apt caches and build residue"

run_in_chroot "apt-get clean && rm -rf /var/lib/apt/lists/*"
rm -rf "${ROOTFS}/tmp/"* "${ROOTFS}/var/tmp/"*
find "${ROOTFS}/var/log" -type f -exec truncate -s 0 {} +

unmount_chroot

# ---------------------------------------------------------------------------
# Stage 8 — squashfs + hybrid ISO
# ---------------------------------------------------------------------------
log "stage 8/8: squashfs + GRUB hybrid ISO"

mkdir -p "${ISO_TREE}/live" "${ISO_TREE}/boot/grub"

# live-boot reads the kernel/initrd from /live on the ISO.
cp "${ROOTFS}"/boot/vmlinuz-*    "${ISO_TREE}/live/vmlinuz"
cp "${ROOTFS}"/boot/initrd.img-* "${ISO_TREE}/live/initrd.img"

mksquashfs "${ROOTFS}" "${ISO_TREE}/live/filesystem.squashfs" \
    -comp zstd -Xcompression-level 19 -noappend -quiet

# Marker file lets GRUB locate the ISO filesystem on any drive.
echo "${VERSION}" > "${ISO_TREE}/SMOLOFIS"

cat > "${ISO_TREE}/boot/grub/grub.cfg" <<'EOF'
set default=0
set timeout=2

insmod all_video
insmod gfxterm

search --no-floppy --set=root --file /SMOLOFIS

menuentry "SmolOfis Appliance" {
    linux  /live/vmlinuz boot=live components quiet hostname=smolofis
    initrd /live/initrd.img
}

menuentry "SmolOfis Appliance (persistence)" {
    linux  /live/vmlinuz boot=live components persistence quiet hostname=smolofis
    initrd /live/initrd.img
}

menuentry "SmolOfis Appliance (debug console)" {
    linux  /live/vmlinuz boot=live components hostname=smolofis systemd.log_level=info
    initrd /live/initrd.img
}
EOF

grub-mkrescue -o "${OUTPUT}" "${ISO_TREE}" -- -volid "SMOLOFIS_${VERSION//./_}"

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
# Hash from inside the output directory so the .sha256 records the bare
# filename and `shasum -c` works wherever the two files are downloaded.
(cd "$(dirname "${OUTPUT}")" && sha256sum "$(basename "${OUTPUT}")" > "$(basename "${OUTPUT}").sha256")
log "build complete"
log "  iso:      ${OUTPUT} ($(du -h "${OUTPUT}" | cut -f1))"
log "  checksum: $(cut -d' ' -f1 "${OUTPUT}.sha256")"
log "flash with: dd if=${OUTPUT} of=/dev/sdX bs=4M status=progress conv=fsync"
