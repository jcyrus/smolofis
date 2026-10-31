#!/usr/bin/env bash
#
# build-image.sh — SmolOfis appliance image builder.
#
# Produces a flashable, BIOS+UEFI hybrid live ISO from a minimal Debian
# rootfs via debootstrap. The resulting image boots headless, starts the
# smolofis-panel dashboard, announces itself as smolofis.local over
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
PRECACHE_IMAGES="${SMOLOFIS_PRECACHE_IMAGES:-0}"

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
        --precache-images) PRECACHE_IMAGES=1; shift ;;
        --skip-images|--no-precache-images) PRECACHE_IMAGES=0; shift ;;
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
    amd64-microcode \
    intel-microcode \
    firmware-linux-free \
    firmware-realtek \
    firmware-intel-graphics \
    systemd-timesyncd \
    curl \
    gnupg \
    iptables \
    network-manager \
    avahi-daemon \
    libnss-mdns \
    openssh-server \
    sudo \
    less \
    htop \
    parted \
    dosfstools \
    e2fsprogs \
    efibootmgr \
    grub-efi-amd64-bin \
    grub-pc-bin \
    grub2-common \
    iproute2 \
    rsync"

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
# Stage 3b — Tailscale Remote Access
# ---------------------------------------------------------------------------
log "stage 3b/8: tailscale remote access"

run_in_chroot "curl -fsSL https://pkgs.tailscale.com/stable/debian/${SUITE}.noarmor.gpg \
    -o /etc/apt/keyrings/tailscale.gpg 2>/dev/null || \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
    -o /etc/apt/keyrings/tailscale.gpg"

cat > "${ROOTFS}/etc/apt/sources.list.d/tailscale.list" <<EOF
deb [signed-by=/etc/apt/keyrings/tailscale.gpg] https://pkgs.tailscale.com/stable/debian ${SUITE} main
EOF

run_in_chroot "apt-get update && apt-get install -y --no-install-recommends tailscale"

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

# TTY login banner: ANSI-colored ASCII identity, no image assets. Raw SGR
# bytes pass through agetty untouched; agetty expands its own escapes at
# display time (\n hostname, \4 IPv4 of the default interface, \s/\r OS
# and kernel release, \l tty line) so a connected monitor immediately
# shows where the appliance lives on the network.
ansi_green=$'\033[1;32m' ansi_dim=$'\033[2m' ansi_reset=$'\033[0m'
cat > "${ROOTFS}/etc/issue" <<EOF

${ansi_green} ____   __  __   ___   _       ___   _____  ___  ____
/ ___| |  \/  | / _ \ | |     / _ \ |  ___||_ _|/ ___|
\___ \ | |\/| || | | || |    | | | || |_    | | \___ \\
 ___) || |  | || |_| || |___ | |_| ||  _|   | |  ___) |
|____/ |_|  |_| \___/ |_____| \___/ |_|    |___||____/ ${ansi_reset}
${ansi_dim} ------------------------------------------------------${ansi_reset}
  the small-office infrastructure appliance
${ansi_dim}  \s \r on \l${ansi_reset}

  ${ansi_green}panel${ansi_reset}     http://\n.local  --  http://\4
  ${ansi_dim}live usb  http://\4:8090 (the panel; the stack runs after install)
  gitea     http://\4:3000  --  coolify   http://\4:8000
  login     smolofis / smolofis (ssh on port 22, change required at first login)
  install   sudo smolofis-install (to write appliance to internal disk)
  remote    sudo smolofis-tailscale up (zero-config tailscale access)${ansi_reset}

EOF
chmod 0644 "${ROOTFS}/etc/issue"

# ---------------------------------------------------------------------------
# Stage 5 — SmolOfis payload (binary, units, configs)
# ---------------------------------------------------------------------------
log "stage 5/8: injecting smolofis payload"

# Control panel binary — root-owned, world-executable, nothing else.
install -o root -g root -m 0755 "${PANEL_BINARY}" "${ROOTFS}/usr/local/bin/smolofis-panel"
install -o root -g root -m 0755 "${SCRIPT_DIR}/smolofis-firstboot.sh" "${ROOTFS}/usr/local/bin/smolofis-firstboot"
install -o root -g root -m 0755 "${SCRIPT_DIR}/smolofis-install.sh" "${ROOTFS}/usr/local/bin/smolofis-install"
install -o root -g root -m 0755 "${SCRIPT_DIR}/smolofis-tailscale.sh" "${ROOTFS}/usr/local/bin/smolofis-tailscale"
install -o root -g root -m 0755 "${SCRIPT_DIR}/smolofis-identity.sh" "${ROOTFS}/usr/local/bin/smolofis-identity"

# systemd orchestration units.
install -o root -g root -m 0644 \
    "${REPO_ROOT}/config/systemd/smolofis-tailscale-connect.service" \
    "${REPO_ROOT}/config/systemd/smolofis-panel.service" \
    "${REPO_ROOT}/config/systemd/smolofis-infrastructure.service" \
    "${REPO_ROOT}/config/systemd/smolofis-identity.service" \
    "${REPO_ROOT}/config/systemd/smolofis-identity-save.service" \
    "${REPO_ROOT}/config/systemd/smolofis-identity-save.path" \
    "${ROOTFS}/etc/systemd/system/"

# Keep container logs from filling the data partition.
install -d -m 0755 "${ROOTFS}/etc/docker"
cat > "${ROOTFS}/etc/docker/daemon.json" <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF

# Application stack definition (installed to /etc/smolofis and system backup /usr/share/smolofis).
install -d -m 0755 "${ROOTFS}/etc/smolofis" "${ROOTFS}/usr/share/smolofis"
install -o root -g root -m 0644 \
    "${REPO_ROOT}/config/docker/docker-compose.yml" "${ROOTFS}/etc/smolofis/docker-compose.yml"
install -o root -g root -m 0644 \
    "${REPO_ROOT}/config/docker/docker-compose.yml" "${ROOTFS}/usr/share/smolofis/docker-compose.yml"

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
install -d -m 0700 "${ROOTFS}/var/lib/tailscale"

# Pre-cache core Docker stack images (enabled via --precache-images).
if [[ ${PRECACHE_IMAGES} -eq 1 ]]; then
    if ! command -v docker >/dev/null 2>&1; then
        warn "docker not found on host; skipping container image pre-caching"
    else
        log "pre-caching core Docker container images into rootfs..."
        readonly DOCKER_STACK_IMAGES=(
            "gitea/gitea:28.2.0"
            "gitea/act_runner:0.6.1"
            "docker.io/coollabsio/coolify:4.5.3"
            "postgres:15-alpine"
            "redis:7-alpine"
            "traefik:v3.7"
        )
        for img in "${DOCKER_STACK_IMAGES[@]}"; do
            log "  pulling ${img} (platform: linux/${ARCH})..."
            docker pull --platform "linux/${ARCH}" "${img}"
        done

        install -d -m 0755 "${ROOTFS}/usr/share/smolofis"
        log "  exporting images to ${ROOTFS}/usr/share/smolofis/core-images.tar..."
        docker save "${DOCKER_STACK_IMAGES[@]}" -o "${ROOTFS}/usr/share/smolofis/core-images.tar"
        chmod 0644 "${ROOTFS}/usr/share/smolofis/core-images.tar"
        log "  pre-cached images bundle: $(du -h "${ROOTFS}/usr/share/smolofis/core-images.tar" | cut -f1)"
    fi
fi

# ---------------------------------------------------------------------------
# Stage 6 — users & service enablement
# ---------------------------------------------------------------------------
log "stage 6/8: service user + unit enablement"

run_in_chroot "useradd --system --no-create-home --shell /usr/sbin/nologin smolofis-panel && \
    usermod -aG docker smolofis-panel"

# Default operator account for console and SSH access.
ADMIN_USER="smolofis"
ADMIN_PASSWORD="${SMOLOFIS_ADMIN_PASSWORD:-smolofis}"
run_in_chroot "useradd -m -s /bin/bash -G sudo,docker ${ADMIN_USER} && \
    echo '${ADMIN_USER}:${ADMIN_PASSWORD}' | chpasswd && \
    chage -d 0 ${ADMIN_USER}"

# Grant sudo access without password prompt on the appliance.
install -d -m 0755 "${ROOTFS}/etc/sudoers.d"
echo "${ADMIN_USER} ALL=(ALL:ALL) NOPASSWD: ALL" > "${ROOTFS}/etc/sudoers.d/99-smolofis-admin"
chmod 0440 "${ROOTFS}/etc/sudoers.d/99-smolofis-admin"

# Ensure SSH permits password login for the operator user.
install -d -m 0755 "${ROOTFS}/etc/ssh/sshd_config.d"
cat > "${ROOTFS}/etc/ssh/sshd_config.d/smolofis.conf" <<'EOF'
PasswordAuthentication yes
KbdInteractiveAuthentication yes
EOF
chmod 0644 "${ROOTFS}/etc/ssh/sshd_config.d/smolofis.conf"

# Lock the root account unless the builder explicitly provides a password
# (useful for debug images: SMOLOFIS_ROOT_PASSWORD=... sudo scripts/build-image.sh ...).
if [[ -n "${SMOLOFIS_ROOT_PASSWORD:-}" ]]; then
    warn "setting root password from SMOLOFIS_ROOT_PASSWORD (debug image)"
    echo "root:${SMOLOFIS_ROOT_PASSWORD}" | chroot "${ROOTFS}" chpasswd
else
    run_in_chroot "passwd -l root"
    log "root account locked (operator user '${ADMIN_USER}' is provisioned)"
fi

systemctl --root="${ROOTFS}" enable \
    smolofis-panel.service \
    smolofis-infrastructure.service \
    smolofis-identity.service \
    smolofis-identity-save.path \
    smolofis-tailscale-connect.service \
    systemd-timesyncd.service \
    docker.service \
    NetworkManager.service \
    avahi-daemon.service \
    ssh.service \
    tailscaled.service

# ---------------------------------------------------------------------------
# Stage 7 — slim down the rootfs
# ---------------------------------------------------------------------------
log "stage 7/8: cleaning apt caches and build residue"

run_in_chroot "apt-get clean && rm -rf /var/lib/apt/lists/*"
rm -rf "${ROOTFS}/tmp/"* "${ROOTFS}/var/tmp/"*
# The ISO is public: host keys generated by the openssh-server package would be
# shared by every device. smolofis-identity creates them per device at boot.
rm -f "${ROOTFS}"/etc/ssh/ssh_host_*
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
# SmolOfis boot menu — themed entirely with GRUB's native text attributes;
# no background images, no external theme assets.
set default=0
set timeout=3

# High-resolution graphical terminal (the font ships inside the
# grub-mkrescue memdisk; fall back to plain text mode if it is missing).
insmod all_video
insmod gfxterm
if loadfont unicode; then
    set gfxmode=1024x768,auto
    set gfxpayload=keep
    terminal_output gfxterm
fi

# Appliance palette, matching the dashboard: signal green on deep black,
# with an inverted green bar as the selection highlight.
set color_normal=light-green/black
set color_highlight=black/light-green

search --no-floppy --set=root --file /SMOLOFIS

menuentry "SmolOfis Appliance" {
    linux  /live/vmlinuz boot=live components quiet splash loglevel=3 vga=current hostname=smolofis
    initrd /live/initrd.img
}

menuentry "SmolOfis Appliance (persistence)" {
    linux  /live/vmlinuz boot=live components persistence quiet splash loglevel=3 vga=current hostname=smolofis
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
