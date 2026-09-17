#!/usr/bin/env bash
# Capture the FLX10 mode-switch test on the wire, then decode ep4/ep5.
# Run: sudo bash capture-modeswitch.sh [--then-rebind]
# Loads usbmon, captures bus 3 (FLX10), runs flx10_modeswitch.py, stops.
set -uo pipefail

PROJECT="/home/Lou/Desktop/Hermes Projects/MIXXX-FLX10"
MODESWITCH="$PROJECT/HID/flx10_modeswitch.py"
PCAP="/tmp/flx10-modeswitch-$(date +%Y%m%d_%H%M%S).pcapng"
EXTRA="${1:-}"

echo "== FLX10 mode-switch capture =="

# FLX10 bus
LINE="$(lsusb | grep -i '2b73:0041' || true)"
if [ -z "$LINE" ]; then echo "!! FLX10 not found"; exit 1; fi
BUS="$(echo "$LINE" | sed 's/Bus \([0-9]*\).*/\1/' | sed 's/^0*//')"
echo "FLX10 on bus $BUS  ($LINE)"

# usbmon
modprobe usbmon 2>/dev/null || true
if [ ! -e "/sys/kernel/debug/usb/usbmon/${BUS}u" ]; then
    echo "!! usbmon${BUS} not available after modprobe"; exit 1
fi

echo "Capturing usbmon${BUS} -> $PCAP"
dumpcap -i "usbmon${BUS}" -w "$PCAP" >/tmp/flx10-modeswitch-dumpcap.log 2>&1 &
DPID=$!
sleep 1

echo "--- running mode switch ---"
python3 -u "$MODESWITCH" $EXTRA

sleep 1
echo "--- stopping capture ---"
kill "$DPID" 2>/dev/null || true
wait "$DPID" 2>/dev/null || true
chown Lou:Lou "$PCAP" 2>/dev/null || true

echo
echo "CAPTURE: $PCAP  ($(du -h "$PCAP" | cut -f1))"
echo "Decode:  python3 \"$PROJECT/tools/flx10-parse.py\" \"$PCAP\""
