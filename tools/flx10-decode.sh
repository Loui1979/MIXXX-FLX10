#!/usr/bin/env bash
# flx10-decode.sh — turn a usbmon pcapng into a human-readable FLX10 packet log.
#
# Usage:
#   ./flx10-decode.sh captures/flx10_handshake.pcapng
#   ./flx10-decode.sh captures/flx10_handshake.pcapng 6   # force device addr 6
#
# Auto-detects the FLX10 device address (the one that appears most), then dumps:
#   - USB control transfers (the handshake / SETUP packets + unlock)
#   - USB-MIDI events (decoded)
#   - bulk/interrupt payloads (jog screens, LEDs) as hex
# grouped by direction (IN = controller->host, OUT = host->controller).

set -euo pipefail
PCAP="${1:?usage: flx10-decode.sh <file.pcapng> [device_address]}"
command -v tshark >/dev/null || { echo "!! tshark not installed: sudo apt install tshark"; exit 1; }

# --- pick the busy device address (FLX10) unless one was given ---------------
ADDR="${2:-}"
if [ -z "$ADDR" ]; then
  ADDR="$(tshark -r "$PCAP" -T fields -e usb.device_address 2>/dev/null \
    | grep -E '^[0-9]+$' | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')"
fi
echo "==================================================================="
echo " FLX10 decode of: $PCAP   (device address = $ADDR)"
echo "==================================================================="

echo
echo "### 1. CONTROL / SETUP transfers (handshake, unlock, descriptors) ###"
tshark -r "$PCAP" -Y "usb.device_address==${ADDR} && usb.transfer_type==0x02" \
  -T fields -e frame.number -e usb.endpoint_address.direction \
  -e usb.bmRequestType -e usb.setup.bRequest -e usb.setup.wValue \
  -e usb.setup.wIndex -e usb.capdata \
  -E header=y -E separator=' | ' 2>/dev/null || echo "(none)"

echo
echo "### 2. USB-MIDI events (status data1 data2) ###"
tshark -r "$PCAP" -Y "usb.device_address==${ADDR} && usbaudio" \
  -T fields -e frame.number -e usb.endpoint_address.direction -e usbaudio.midi.event \
  -E header=y -E separator=' | ' 2>/dev/null || echo "(none)"

echo
echo "### 3. INTERRUPT payloads (buttons / knobs / LEDs) ###"
tshark -r "$PCAP" -Y "usb.device_address==${ADDR} && usb.transfer_type==0x01 && usb.capdata" \
  -T fields -e frame.number -e usb.endpoint_address.direction -e usb.capdata \
  -E header=y -E separator=' | ' 2>/dev/null || echo "(none)"

echo
echo "### 4. BULK payloads (jog screens / graphics) ###"
tshark -r "$PCAP" -Y "usb.device_address==${ADDR} && usb.transfer_type==0x03 && usb.capdata" \
  -T fields -e frame.number -e usb.endpoint_address.direction -e frame.len -e usb.capdata \
  -E header=y -E separator=' | ' 2>/dev/null || echo "(none)"

echo
echo "direction: 0 = OUT (host->controller: LEDs/screens)   1 = IN (controller->host: controls)"
