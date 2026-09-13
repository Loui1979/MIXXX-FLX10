#!/usr/bin/env bash
# flx10-capture.sh — capture USB packets between the DDJ-FLX10 and rekordbox
# (running inside the tiny11 VMware guest) using the Linux host's usbmon.
#
# WHY THIS WORKS: usbmon taps the kernel USB stack BELOW VMware's USB
# passthrough, so it records every URB moving on the physical bus even when
# rekordbox in the VM is the thing driving the controller.
#
# Usage:
#   ./flx10-capture.sh            # auto-detect FLX10, capture to a timestamped file
#   ./flx10-capture.sh myname     # capture to captures/myname.pcapng
#
# Requires one-time setup (see WIRESHARK-GUIDE.md, "First-time setup").

set -euo pipefail

VID="2b73"                                  # AlphaTheta / Pioneer DJ
OUTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/captures"
mkdir -p "$OUTDIR"
NAME="${1:-flx10_$(date +%Y%m%d_%H%M%S)}"
OUT="$OUTDIR/${NAME}.pcapng"

# --- locate the FLX10 -------------------------------------------------------
LINE="$(lsusb | grep -i "$VID" || true)"
if [ -z "$LINE" ]; then
  echo "!! FLX10 (VID $VID) not found in lsusb."
  echo "   - Plug it into the USB-C / Thunderbolt port and power it on."
  echo "   - If already passed through to the VM, that's fine for capture,"
  echo "     but re-plug on the HOST first so this script can read its bus."
  exit 1
fi

# lsusb line: "Bus 001 Device 007: ID 2b73:001c ..."
BUS="$(echo "$LINE"    | sed 's/Bus \([0-9]*\).*/\1/' | sed 's/^0*//')"
DEVADDR="$(echo "$LINE" | sed 's/.*Device \([0-9]*\):.*/\1/' | sed 's/^0*//')"
IFACE="usbmon${BUS}"

echo "Found: $LINE"
echo "  bus=$BUS  device=$DEVADDR  ->  interface $IFACE"

# --- sanity: usbmon loaded? -------------------------------------------------
if [ ! -e "/sys/kernel/debug/usb/usbmon/${BUS}u" ]; then
  echo "!! usbmon node /sys/kernel/debug/usb/usbmon/${BUS}u missing."
  echo "   Run once:  sudo modprobe usbmon"
  exit 1
fi

echo
echo ">> Capturing on $IFACE (filtering to device $DEVADDR)."
echo ">> Now: connect the FLX10 to the VM (VM > Removable Devices > Connect),"
echo ">>      let rekordbox see it, then work the controls you want to record."
echo ">> Press Ctrl+C to stop."
echo

# Capture the whole bus (usbmon doesn't support per-device BPF capture
# filters reliably). Filter to this device later in Wireshark with the
# DISPLAY filter:   usb.device_address == DEVADDR
echo ">> After stopping, filter in Wireshark with:  usb.device_address == ${DEVADDR}"
echo ">> Saved device address ${DEVADDR} to ${OUT%.pcapng}.devaddr.txt"
echo "$DEVADDR" > "${OUT%.pcapng}.devaddr.txt"
echo
exec dumpcap -i "$IFACE" -w "$OUT"
