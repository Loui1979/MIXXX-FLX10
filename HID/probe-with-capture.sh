#!/bin/bash
# Run under sudo. Captures USB traffic on the FLX10's bus while running the
# ep5 probe, so we can see NAK/STALL/timeout on the wire.
#   sudo ~/Desktop/Hermes\ Projects/MIXXX-FLX10/HID/probe-with-capture.sh
# Afterwards: /tmp/ep5probe.txt (probe stdout) + /tmp/ep5probe.pcapng (capture)
set -u
# (probe+capture helper — see also render2 which logs to /tmp/render2.txt)
HID="/home/Lou/Desktop/Hermes Projects/MIXXX-FLX10/HID"
PY="$HID/.venv-relay/bin/python"
OUT_TXT=/tmp/ep5probe.txt
OUT_PCAP=/tmp/ep5probe.pcapng

modprobe usbmon 2>/dev/null || true

# locate the FLX10 (VID 2b73) and derive its bus number
VFILE=$(grep -l 2b73 /sys/bus/usb/devices/*/idVendor 2>/dev/null | head -1)
if [ -z "$VFILE" ]; then echo "FLX10 (2b73) not found in sysfs — plugged in? on host, not VM?"; exit 1; fi
DEVDIR=$(dirname "$VFILE")
BUSNAME=$(basename "$DEVDIR")      # e.g. 3-9
BUS=${BUSNAME%%-*}                 # e.g. 3
echo "FLX10 at sysfs $BUSNAME  ->  capturing on usbmon$BUS"

# start USB capture (root; ~few seconds, small file)
dumpcap -i "usbmon$BUS" -w "$OUT_PCAP" -q &
DPID=$!
sleep 1

# run the ep5 probe, tee output to a file
"$PY" "$HID/flx10_ep5_probe.py" 2>&1 | tee "$OUT_TXT"

sleep 0.5
kill "$DPID" 2>/dev/null || true
wait "$DPID" 2>/dev/null || true

chown Lou:Lou "$OUT_TXT" "$OUT_PCAP" 2>/dev/null || true
echo "=== wrote $OUT_TXT and $OUT_PCAP — tell the agent 'done' ==="
