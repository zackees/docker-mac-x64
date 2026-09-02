#!/usr/bin/env bash
# Headless-capable Launch.sh for etasdemir/osx-container.
#
# Deltas from the upstream script, all forced by `docker run -d` + our goal
# of shelling in to run a Mach-O binary:
#   1. -monitor stdio          -> unix socket. stdio has no tty when detached,
#                                 and the socket gives us `screendump` so boot
#                                 progress is verifiable without a viewer.
#   2. netdev user             -> + hostfwd=tcp::10022-:22. Upstream forgot this,
#                                 so the README's `-p 50922:10022` forwarded to
#                                 nothing and ssh could never work.
#   3. 9p /mnt/MacosShared     -> dropped. QEMU refuses to start if the path is
#                                 absent, and we ship files over scp anyway.
#   4. -display                -> selectable; defaults to VNC on 0.0.0.0:0 so the
#                                 installer is drivable without X11 passthrough.
#   5. OVMF_CODE.fd            -> auto-detect, current OSX-KVM ships _4M variants.
# CPU line is deliberately untouched: Penryn + vendor=GenuineIntel is what makes
# an AMD host work at all (macOS never takes its AMD paths), and Penryn predates
# PCID, so dockur/macos#268's single-core constraint does not apply here.
set -euo pipefail

MY_OPTIONS="+ssse3,+sse4.2,+popcnt,+avx,+aes,+xsave,+xsaveopt,check"

ALLOCATED_RAM="${1:-8}"000
CPU_SOCKETS="1"
CPU_CORES="${2:-2}"
CPU_THREADS="${3:-4}"
GPU="128"   # match upstream OpenCore-Boot.sh
EXTRA_ARGS="${4:-}"

REPO_PATH="."
cd /home/user/OSX-KVM

# OSX-KVM renamed these to the 4M variants; older images still carry the 2M ones.
if [[ -f OVMF_CODE_4M.fd ]]; then OVMF_CODE=OVMF_CODE_4M.fd; else OVMF_CODE=OVMF_CODE.fd; fi
OVMF_VARS=OVMF_VARS-1024x768.fd

# VNC (default) keeps this working on a headless/detached container; X11 only
# works if /tmp/.X11-unix is bind-mounted and the host ran `xhost +local:`.
case "${DISPLAY_MODE:-vnc}" in
  vnc)  DISPLAY_ARGS=(-display none -vnc 0.0.0.0:0) ;;
  x11)  DISPLAY_ARGS=(-display gtk) ;;
  none) DISPLAY_ARGS=(-display none) ;;
  *)    echo "DISPLAY_MODE must be vnc|x11|none" >&2; exit 2 ;;
esac

args=(
  -enable-kvm -m "$ALLOCATED_RAM"
  -cpu Penryn,kvm=on,vendor=GenuineIntel,+invtsc,vmware-cpuid-freq=on,"$MY_OPTIONS"
  -machine q35
  -usb
  -smp "$CPU_THREADS",cores="$CPU_CORES",sockets="$CPU_SOCKETS"
  -device usb-ehci,id=ehci
  -device usb-kbd,bus=ehci.0 -device usb-tablet,bus=ehci.0
  -device nec-usb-xhci,id=xhci
  -global nec-usb-xhci.msi=off
  -device isa-applesmc,osk="ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc"
  -drive if=pflash,format=raw,readonly=on,file="$REPO_PATH/$OVMF_CODE"
  -drive if=pflash,format=raw,file="$REPO_PATH/$OVMF_VARS"
  -smbios type=2
  -device ich9-intel-hda -device hda-duplex
  -device ich9-ahci,id=sata
  -drive id=OpenCoreBoot,if=none,snapshot=on,format=qcow2,file="$REPO_PATH/OpenCore/OpenCore.qcow2"
  -device ide-hd,bus=sata.2,drive=OpenCoreBoot
  -device ide-hd,bus=sata.3,drive=InstallMedia
  -drive id=InstallMedia,if=none,file="$REPO_PATH/BaseSystem.img",format=raw
  -drive id=MacHDD,if=none,file="$REPO_PATH/mac_hdd_ng.img",format=qcow2
  -device ide-hd,bus=sata.4,drive=MacHDD
  -netdev user,id=net0,hostfwd=tcp::10022-:22
  -device virtio-net-pci,netdev=net0,id=net0,mac=52:54:00:c9:18:27
  -monitor unix:/home/user/OSX-KVM/monitor.sock,server,nowait
  -device VGA,vgamem_mb="${GPU}"
  "${DISPLAY_ARGS[@]}"
  ${EXTRA_ARGS:-}
)

echo "launching: qemu-system-x86_64 (ram=${ALLOCATED_RAM}M cores=${CPU_CORES} threads=${CPU_THREADS} display=${DISPLAY_MODE:-vnc} ovmf=${OVMF_CODE})"
exec qemu-system-x86_64 "${args[@]}"
