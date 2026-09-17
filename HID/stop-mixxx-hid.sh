#!/usr/bin/env bash
# Abort Mixxx HID session: daemon, USB capture, Mixxx. Finish pcap ownership.
# Safe to run from another terminal while start-mixxx-hid.sh is in the foreground.
#
#   "/home/Lou/Desktop/Hermes Projects/MIXXX-FLX10/HID/stop-mixxx-hid.sh"
set -u

STATE="/tmp/flx10-mixxx-hid.state"
PCAP=""
DUMPCAP_PID=""
DEVADDR=""

if [ -f "$STATE" ]; then
    # shellcheck disable=SC1090
    . "$STATE"
fi

echo "== abort FLX10 Mixxx + HID =="

echo "Stopping HID daemon..."
sudo pkill -f '/HID/flx10_screen_daemon.py' 2>/dev/null || true
sleep 0.3
if pgrep -f '/HID/flx10_screen_daemon.py' >/dev/null 2>&1; then
    sudo pkill -9 -f '/HID/flx10_screen_daemon.py' 2>/dev/null || true
fi

echo "Stopping USB capture..."
if [ -n "${DUMPCAP_PID}" ] && kill -0 "$DUMPCAP_PID" 2>/dev/null; then
    kill "$DUMPCAP_PID" 2>/dev/null || sudo kill "$DUMPCAP_PID" 2>/dev/null || true
    sleep 0.3
    kill -9 "$DUMPCAP_PID" 2>/dev/null || sudo kill -9 "$DUMPCAP_PID" 2>/dev/null || true
else
    pkill -f 'dumpcap -i usbmon' 2>/dev/null || sudo pkill -f 'dumpcap -i usbmon' 2>/dev/null || true
fi

echo "Stopping Mixxx..."
flatpak kill org.mixxx.Mixxx 2>/dev/null || true

sleep 0.5

if [ -n "$PCAP" ] && [ -f "$PCAP" ]; then
    if [ "$(stat -c %u "$PCAP" 2>/dev/null || echo 0)" = "0" ]; then
        sudo chown Lou:Lou "$PCAP" 2>/dev/null || true
    fi
    echo
    echo "USB log: $PCAP  ($(du -h "$PCAP" | cut -f1))"
    echo "Open:    wireshark \"$PCAP\""
    echo "Filter:  usb.idVendor == 0x2b73"
    if [ -n "${DEVADDR:-}" ]; then
        echo "         usb.device_address == ${DEVADDR}  (start address; unlock may re-enumerate)"
    fi
else
    echo
    echo "No pcap in $STATE — look in /tmp/flx10-mixxx-*.pcapng"
    ls -1t /tmp/flx10-mixxx-*.pcapng 2>/dev/null | head -3 || true
fi

rm -f "$STATE"
echo "Done."
