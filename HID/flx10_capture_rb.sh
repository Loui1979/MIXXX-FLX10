#!/bin/bash
# Capture rekordbox <-> FLX10 USB traffic from the Linux host while the
# tinywin11 VM drives the deck. usbmon sees VMware's passthrough URBs.
# Run via pkexec; hard-stops after 150s (or kill it).
#   pkexec bash .../flx10_capture_rb.sh
set -u
OUT=/tmp/rb_wave_capture.pcapng
rm -f "$OUT"
modprobe usbmon 2>/dev/null || true
VFILE=$(grep -l 2b73 /sys/bus/usb/devices/*/idVendor 2>/dev/null | head -1)
if [ -z "$VFILE" ]; then echo "FLX10 (2b73) not on host bus — plug it in on the host first"; exit 1; fi
BUS=$(basename "$(dirname "$VFILE")"); BUS=${BUS%%-*}
echo "capturing usbmon$BUS -> $OUT  (auto-stop 150s, or kill dumpcap)"
dumpcap -i "usbmon$BUS" -w "$OUT" -a duration:300 -q
chown Lou:Lou "$OUT" 2>/dev/null || true
echo "=== capture saved: $OUT ==="
