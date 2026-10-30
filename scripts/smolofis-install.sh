#!/usr/bin/env bash
#
# smolofis-install.sh — Bare-metal disk installer for SmolOfis.
#
# Wipes and partitions an internal disk (NVMe/SATA/eMMC), installs the
# immutable appliance system image, provisions GRUB bootloader (UEFI & BIOS),
# and initializes persistent data storage across reboots.
#
# Usage:
#   sudo smolofis-install                   # Interactive wizard
#   sudo smolofis-install --disk /dev/nvme0n1 --yes   # Non-interactive

set -euo pipefail

readonly C_INFO=$'\033[1;32m'
readonly C_WARN=$'\033[1;33m'
readonly C_ERR=$'\033[1;31m'
readonly C_BOLD=$'\033[1m'
readonly C_OFF=$'\033[0m'

log()  { printf '%s[smolofis-install]%s %s\n' "${C_INFO}" "${C_OFF}" "$*"; }
warn() { printf '%s[smolofis-install] WARNING:%s %s\n' "${C_WARN}" "${C_OFF}" "$*" >&2; }
die()  { printf '%s[smolofis-install] ERROR:%s %s\n' "${C_ERR}" "${C_OFF}" "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "must be run as root (use sudo)"

TARGET_DISK=""
ASSUME_YES=0
SOURCE_DIR=""
MOUNT_DIR="$(mktemp -d -t smolofis-install-XXXXXX)"

cleanup() {
    log "cleaning up mount points..."
    umount -R "${MOUNT_DIR}" 2>/dev/null || true
    rm -rf "${MOUNT_DIR}"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# CLI Argument Parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --disk) TARGET_DISK="$2"; shift 2 ;;
        --source) SOURCE_DIR="$2"; shift 2 ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help)
            echo "Usage: sudo smolofis-install [OPTIONS]"
            echo "  --disk PATH    Target drive to wipe and install to (e.g. /dev/nvme0n1, /dev/sda)"
            echo "  --source PATH  Custom path to directory containing live {vmlinuz, initrd.img, filesystem.squashfs}"
            echo "  -y, --yes      Skip confirmation prompts"
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
done

# ---------------------------------------------------------------------------
# Preflight checks & tool validation
# ---------------------------------------------------------------------------
for tool in parted mkfs.vfat mkfs.ext4 grub-install lsblk findmnt blkid; do
    command -v "${tool}" >/dev/null 2>&1 || die "missing required utility: ${tool}"
done

# Locate live OS assets
find_live_assets() {
    if [[ -n "${SOURCE_DIR}" && -f "${SOURCE_DIR}/filesystem.squashfs" ]]; then
        echo "${SOURCE_DIR}"
        return 0
    fi
    for path in \
        "/run/live/medium/live" \
        "/lib/live/mount/medium/live" \
        "/live"; do
        if [[ -f "${path}/filesystem.squashfs" && -f "${path}/vmlinuz" && -f "${path}/initrd.img" ]]; then
            echo "${path}"
            return 0
        fi
    done
    local alt
    alt=$(find /run /lib/live -name "filesystem.squashfs" 2>/dev/null | head -n 1 || true)
    if [[ -n "${alt}" ]]; then
        dirname "${alt}"
        return 0
    fi
    return 1
}

LIVE_SRC="$(find_live_assets)" || die "could not locate SmolOfis live assets (filesystem.squashfs, vmlinuz, initrd.img)"
log "found appliance live assets at: ${LIVE_SRC}"

# Determine live boot device to avoid offering to overwrite the installer media itself
BOOT_MEDIUM_DEV="$(findmnt -n -o SOURCE /run/live/medium 2>/dev/null || findmnt -n -o SOURCE /lib/live/mount/medium 2>/dev/null || true)"
BOOT_DISK=""
if [[ -n "${BOOT_MEDIUM_DEV}" ]]; then
    BOOT_DISK="$(lsblk -n -o PKNAME "${BOOT_MEDIUM_DEV}" 2>/dev/null || true)"
    if [[ -n "${BOOT_DISK}" ]]; then
        BOOT_DISK="/dev/${BOOT_DISK}"
    fi
fi

# ---------------------------------------------------------------------------
# Interactive disk selection
# ---------------------------------------------------------------------------
if [[ -z "${TARGET_DISK}" ]]; then
    printf '\n%s=== SmolOfis Bare-Metal Appliance Installer ===%s\n\n' "${C_BOLD}" "${C_OFF}"
    echo "Available storage disks on this machine:"
    echo "---------------------------------------------------------"
    lsblk -d -o NAME,SIZE,TYPE,TRAN,MODEL | grep -E "disk|NAME" || true
    echo "---------------------------------------------------------"
    if [[ -n "${BOOT_DISK}" ]]; then
        printf 'Note: Installer media detected at %s (excluded)\n\n' "${BOOT_DISK}"
    fi

    read -rp "Enter the target disk device to install SmolOfis onto (e.g. /dev/nvme0n1 or /dev/sda): " TARGET_DISK
fi

[[ -b "${TARGET_DISK}" ]] || die "target disk device '${TARGET_DISK}' does not exist or is not a block device"

if [[ -n "${BOOT_DISK}" && "${TARGET_DISK}" == "${BOOT_DISK}" ]]; then
    die "refusing to install to '${TARGET_DISK}' because it contains the active live installer medium"
fi

# ---------------------------------------------------------------------------
# Safety confirmation
# ---------------------------------------------------------------------------
DISK_INFO="$(lsblk -d -n -o MODEL,SIZE "${TARGET_DISK}" | xargs)"
if [[ ${ASSUME_YES} -eq 0 ]]; then
    printf '\n%s⚠️  WARNING: ALL DATA ON %s (%s) WILL BE PERMANENTLY ERASED!%s\n' "${C_ERR}" "${TARGET_DISK}" "${DISK_INFO}" "${C_OFF}"
    read -rp "Type 'YES' to proceed with wiping and installing SmolOfis: " CONFIRM
    if [[ "${CONFIRM}" != "YES" ]]; then
        log "installation cancelled by user"
        exit 0
    fi
fi

log "commencing installation on ${TARGET_DISK} (${DISK_INFO})"

# Unmount any existing partitions on the target disk
for part in $(lsblk -n -l -o NAME "${TARGET_DISK}" | tail -n +2); do
    umount -f "/dev/${part}" 2>/dev/null || true
done

# Helper to format partition device path (e.g. /dev/nvme0n1p1 vs /dev/sda1)
part_path() {
    local dev="$1"
    local num="$2"
    if [[ "${dev}" =~ [0-9]$ ]]; then
        printf "%sp%d" "${dev}" "${num}"
    else
        printf "%s%d" "${dev}" "${num}"
    fi
}

PART_EFI="$(part_path "${TARGET_DISK}" 1)"
PART_SYS="$(part_path "${TARGET_DISK}" 2)"
PART_DATA="$(part_path "${TARGET_DISK}" 3)"

# ---------------------------------------------------------------------------
# Partitioning (GPT)
# ---------------------------------------------------------------------------
# Partition 1: EFI System Partition (512 MiB)
# Partition 2: SmolOfis OS System (8 GiB) — holds squashfs and kernels
# Partition 3: SmolOfis Persistent Data (100% remaining space)
log "partitioning ${TARGET_DISK} (GPT)..."
parted -s "${TARGET_DISK}" mklabel gpt
parted -s -a optimal "${TARGET_DISK}" mkpart "ESP" fat32 1MiB 513MiB
parted -s "${TARGET_DISK}" set 1 esp on
parted -s -a optimal "${TARGET_DISK}" mkpart "SMOLOFIS_SYS" ext4 513MiB 8705MiB
parted -s -a optimal "${TARGET_DISK}" mkpart "SMOLOFIS_DATA" ext4 8705MiB 100%

# Re-read partition table
udevadm settle || sleep 2

# ---------------------------------------------------------------------------
# Filesystem Formatting
# ---------------------------------------------------------------------------
log "formatting EFI System Partition (${PART_EFI})..."
mkfs.vfat -F 32 -n "SMOLOFIS_EFI" "${PART_EFI}" >/dev/null

log "formatting System Partition (${PART_SYS})..."
mkfs.ext4 -F -q -L "SMOLOFIS_SYS" "${PART_SYS}"

log "formatting Persistent Data Partition (${PART_DATA})..."
mkfs.ext4 -F -q -L "persistence" "${PART_DATA}"

# ---------------------------------------------------------------------------
# Copying System Payloads
# ---------------------------------------------------------------------------
SYS_MNT="${MOUNT_DIR}/sys"
DATA_MNT="${MOUNT_DIR}/data"
mkdir -p "${SYS_MNT}" "${DATA_MNT}"

log "mounting system target at ${SYS_MNT}..."
mount "${PART_SYS}" "${SYS_MNT}"
mkdir -p "${SYS_MNT}/live" "${SYS_MNT}/boot/grub"

log "copying appliance system image (filesystem.squashfs, vmlinuz, initrd.img)..."
cp -v "${LIVE_SRC}/vmlinuz" "${SYS_MNT}/live/vmlinuz"
cp -v "${LIVE_SRC}/initrd.img" "${SYS_MNT}/live/initrd.img"
cp -v "${LIVE_SRC}/filesystem.squashfs" "${SYS_MNT}/live/filesystem.squashfs"

# Create marker file for GRUB search
echo "smolofis-appliance" > "${SYS_MNT}/SMOLOFIS"

# Configure persistent data partition
log "configuring persistence on ${PART_DATA}..."
mount "${PART_DATA}" "${DATA_MNT}"
cat > "${DATA_MNT}/persistence.conf" <<'EOF'
/var/lib/smolofis bind
/var/lib/docker bind
/etc/smolofis bind
/var/lib/tailscale bind
/etc/NetworkManager/system-connections bind
/home bind
EOF
mkdir -p "${DATA_MNT}/var/lib/smolofis/gitea" "${DATA_MNT}/var/lib/smolofis/coolify" "${DATA_MNT}/var/lib/tailscale" "${DATA_MNT}/var/lib/docker"
chmod 0700 "${DATA_MNT}/var/lib/tailscale"
chown -R 1000:1000 "${DATA_MNT}/var/lib/smolofis/gitea" 2>/dev/null || true
sync

# ---------------------------------------------------------------------------
# Bootloader Installation (GRUB)
# ---------------------------------------------------------------------------
mkdir -p "${SYS_MNT}/boot/efi"
mount "${PART_EFI}" "${SYS_MNT}/boot/efi"

if [[ -d /sys/firmware/efi ]]; then
    log "UEFI firmware detected; installing GRUB x86_64-efi..."
    grub-install \
        --target=x86_64-efi \
        --efi-directory="${SYS_MNT}/boot/efi" \
        --boot-directory="${SYS_MNT}/boot" \
        --bootloader-id="SmolOfis" \
        --removable \
        --recheck
else
    log "legacy BIOS detected; installing GRUB i386-pc to ${TARGET_DISK}..."
    grub-install \
        --target=i386-pc \
        --boot-directory="${SYS_MNT}/boot" \
        --recheck \
        "${TARGET_DISK}" || warn "BIOS grub-install had warnings (system will still boot on UEFI)"
fi

log "generating GRUB configuration..."
cat > "${SYS_MNT}/boot/grub/grub.cfg" <<'EOF'
set default=0
set timeout=3

insmod all_video
insmod gfxterm
if loadfont unicode; then
    set gfxmode=1024x768,auto
    terminal_output gfxterm
fi

set color_normal=light-green/black
set color_highlight=black/light-green

search --no-floppy --label --set=root SMOLOFIS_SYS

menuentry "SmolOfis Appliance" {
    linux  /live/vmlinuz boot=live components persistence quiet splash loglevel=3 vga=current hostname=smolofis
    initrd /live/initrd.img
}

menuentry "SmolOfis Appliance (debug console)" {
    linux  /live/vmlinuz boot=live components persistence hostname=smolofis systemd.log_level=info
    initrd /live/initrd.img
}
EOF

sync
log "installation completed successfully!"
printf '\n%sSmolOfis has been successfully installed to %s.%s\n' "${C_INFO}${C_BOLD}" "${TARGET_DISK}" "${C_OFF}"
echo "You can now safely remove the USB installation drive and reboot the appliance."

if [[ ${ASSUME_YES} -eq 0 ]]; then
    read -rp "Reboot now? (y/N): " REBOOT
    if [[ "${REBOOT}" =~ ^[Yy]$ ]]; then
        reboot
    fi
fi
