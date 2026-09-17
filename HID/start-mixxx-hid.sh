#!/usr/bin/env bash
# Unlock FLX10, start Mixxx (developer), then the HID screen daemon.
# MIDI mapping stays on. Mixxx "DDJ-FLX10 Screen" stays OFF.
# You will be prompted for sudo (unlock + daemon). Agent cannot sudo.
#
# Also logs USB (usbmon → pcapng) from before unlock until Ctrl+C.
# Open the file in Wireshark when done. Filter: usb.idVendor == 0x2b73
#
# Chris (Lougazi): we capture-matched rekordbox. That session had the 7 vendor
# CTRL unlock + lots of MIDI SysEx (keepalive / 03 01 / LED banks) and almost
# NO HID OUT. The daemon used to hammer Serato xx 30/39 on hidraw at start —
# that is NOT rekordbox, and those writes timed out (Errno 110). Default here
# is NO --serato-init (daemon only tails mixxx.log). Rekordbox-like OUT is
# Mixxx JS enableRekordboxSysex. To get the old Serato HID init:
#   SERATO_INIT=1 ./start-mixxx-hid.sh
set -euo pipefail

LOG="/tmp/flx10-launcher.log"
exec > >(tee -a "$LOG") 2>&1
echo "==== launcher $(date) ===="

PROJECT="/home/Lou/Desktop/Hermes Projects/MIXXX-FLX10"
UNLOCK="$PROJECT/HID/flx10_unlock_v2.py"
DAEMON="$PROJECT/HID/flx10_rekordbox_daemon.py"   # rekordbox-native (no serato xx30/39)
MIXXX_LOG="/home/Lou/.var/app/org.mixxx.Mixxx/.mixxx/mixxx.log"

DUMPCAP_PID=""
PCAP=""
STATE="/tmp/flx10-mixxx-hid.state"

write_state() {
    cat > "$STATE" <<EOF
PCAP="$PCAP"
DUMPCAP_PID="$DUMPCAP_PID"
DEVADDR="${DEVADDR:-}"
EOF
}

stop_capture() {
    if [ -n "${DUMPCAP_PID}" ] && kill -0 "$DUMPCAP_PID" 2>/dev/null; then
        echo "Stopping USB capture (pid $DUMPCAP_PID)..."
        kill "$DUMPCAP_PID" 2>/dev/null || true
        wait "$DUMPCAP_PID" 2>/dev/null || true
    fi
    if [ -n "$PCAP" ] && [ -f "$PCAP" ]; then
        if [ "$(stat -c %u "$PCAP" 2>/dev/null || echo 0)" = "0" ]; then
            sudo chown Lou:Lou "$PCAP" 2>/dev/null || true
        fi
        echo
        echo "USB log: $PCAP  ($(du -h "$PCAP" | cut -f1))"
        echo "Open:    wireshark \"$PCAP\""
        echo "Filter:  usb.idVendor == 0x2b73"
        if [ -n "${DEVADDR:-}" ]; then
            echo "         usb.device_address == ${DEVADDR}  (address at capture start; unlock may re-enumerate)"
        fi
    fi
}

trap stop_capture EXIT INT TERM

echo "== FLX10 Mixxx + HID daemon =="
echo "Waiting for DDJ-FLX10 (2b73:0041)..."
LINE=""
for i in $(seq 1 30); do
    LINE="$(lsusb | grep -i '2b73:0041' || true)"
    if [ -n "$LINE" ]; then
        echo "$LINE"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "!! FLX10 not found. Plug it in (any USB port — was a bad cord, not USB-C-only)."
        exit 1
    fi
    sleep 1
done

BUS="$(echo "$LINE" | sed 's/Bus \([0-9]*\).*/\1/' | sed 's/^0*//')"
DEVADDR="$(echo "$LINE" | sed 's/.*Device \([0-9]*\):.*/\1/' | sed 's/^0*//')"
IFACE="usbmon${BUS}"
PCAP="/tmp/flx10-mixxx-$(date +%Y%m%d_%H%M%S).pcapng"

echo "USB bus=$BUS device=$DEVADDR  capture iface=$IFACE"

if [ ! -e "/dev/${IFACE}" ] && [ ! -e "/sys/kernel/debug/usb/usbmon/${BUS}u" ]; then
    echo "usbmon not loaded. Run once:  sudo modprobe usbmon"
    echo "Continuing without USB log."
else
    echo "Starting USB capture (dumpcap $IFACE → $PCAP) before unlock..."
    echo "Keep Mixxx audio OFF the FLX10 if you can — ISO audio bloats the file."
    if dumpcap -i "$IFACE" -w "$PCAP" >/tmp/flx10-dumpcap.log 2>&1 & then
        DUMPCAP_PID=$!
        sleep 0.4
        if ! kill -0 "$DUMPCAP_PID" 2>/dev/null; then
            echo "dumpcap as user failed; trying sudo dumpcap → /tmp ..."
            sudo dumpcap -i "$IFACE" -w "$PCAP" >/tmp/flx10-dumpcap.log 2>&1 &
            DUMPCAP_PID=$!
            sleep 0.4
        fi
        if kill -0 "$DUMPCAP_PID" 2>/dev/null; then
            echo "dumpcap pid $DUMPCAP_PID"
        else
            echo "!! dumpcap did not start. See /tmp/flx10-dumpcap.log"
            DUMPCAP_PID=""
        fi
    fi
fi
write_state

echo "Closing Mixxx if it is running... "
flatpak kill org.mixxx.Mixxx 2>/dev/null || true
sleep 1

# REKORDBOX path: daemon does unlock, then tails mixxx.log watching for your JS's
# rekordbox mode-enable SysEx (f0…0301f7) and fires the ep5 replay AFTER it.
# No pre-flight ep5 — firmware must see mode-enable first (else Errno 110).
if [ "${SERATO_INIT:-0}" != "1" ]; then
    echo "Unlock + ep5-on-mode-enable via rekordbox daemon (sudo), then Mixxx after 3s..."
    sudo python3 -u "$DAEMON" --unlock --replay-ep5 --tail-log --log "$MIXXX_LOG" &
    DAEMON_PID=$!
    sleep 3
else
    echo "Unlock (sudo)..."
    sudo python3 "$UNLOCK"
    sleep 1
    echo "Starting HID screen daemon (sudo) [serato xx30/39 init]..."
    sudo python3 -u "$DAEMON" --tail-log --log "$MIXXX_LOG" --serato-init &
    DAEMON_PID=$!
fi

echo "Starting Mixxx (developer)..."
flatpak run --branch=nightly --arch=x86_64 --command=mixxx org.mixxx.Mixxx --developer &
sleep 6

echo "HID daemon pid=$DAEMON_PID — load a track and watch the log/socket."
echo "Preferences: Pioneer DDJ-FLX10 MIDI ON. DDJ-FLX10 Screen / Dummy Device Screen OFF."
echo "Ctrl+C this terminal: daemon + USB capture off, Mixxx stays up."
echo "Full abort (Mixxx + daemon + capture, finish pcap):"
echo "  $PROJECT/HID/stop-mixxx-hid.sh"

wait "$DAEMON_PID" 2>/dev/null || true
