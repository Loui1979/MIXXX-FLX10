#!/usr/bin/env python3
"""
flx10_replay_serato.py — replay Serato's exact EP5 OUT bytes from a pcapng
capture, after doing the SysEx handshake (03 01 + 50 31 keepalive) that we
know lights up the LCDs.

This is the decisive byte-for-byte test: if Serato's actual captured packets
don't render on your FLX10, the gate is firmware-side (different unit,
different firmware build, region SKU) and no amount of protocol-decoding will
fix it.  If they DO render, we have a byte-level bug somewhere in our
synthesized packets that we can find by diffing.

Usage (with Mixxx CLOSED, FLX10 plugged in):
  sudo python3 flx10_replay_serato.py \\
      /home/vpinedax/Downloads/flx10-driverrutil-then-serato.pcapng
"""

import argparse
import glob
import os
import struct
import subprocess
import sys
import threading
import time

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("pyusb not found.  pip install pyusb")

VID = 0x2B73
PID = 0x0041
SCREEN_INTERFACE = 5

VENDOR_UNLOCK_CMDS = [
    (0x0100, 0xC028), (0x0000, 0xC029), (0x0200, 0xC013), (0x0000, 0xC02B),
    (0x0100, 0xC026), (0x0000, 0xC01D), (0x0100, 0xC027),
]


# ---------------------------------------------------------------------------
# pcapng parsing — extract EP5 OUT packets with timestamps
# ---------------------------------------------------------------------------

def parse_pcapng_ep5_out(pcap_path):
    """Use tshark to extract (timestamp_seconds, 128-byte payload) for every
    host→device EP5 OUT write in the pcapng. Robust against pcapng variants
    that my hand-rolled parser bungled."""
    out = []
    cmd = [
        "tshark", "-r", pcap_path,
        "-Y", 'usb.endpoint_address == 0x05 && usb.src == "host"',
        "-T", "fields",
        "-e", "frame.time_relative",
        "-e", "usbhid.data",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        sys.exit(f"tshark failed: {proc.stderr[:500]}")

    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        hx = parts[1].strip()
        if len(hx) != 256:    # 128 bytes = 256 hex chars
            continue
        try:
            pkt = bytes.fromhex(hx)
            out.append((ts, pkt))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Device + USB helpers (copied from flx10_rekordbox_proto.py)
# ---------------------------------------------------------------------------

def find_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("DDJ-FLX10 not found")
    print(f"Found DDJ-FLX10 — bus {dev.bus}, device {dev.address}")
    return dev


def vendor_unlock(dev):
    print("Vendor unlock: 7 commands …")
    for i, (wValue, wIndex) in enumerate(VENDOR_UNLOCK_CMDS, 1):
        try:
            dev.ctrl_transfer(0x40, 3, wValue, wIndex, None, timeout=200)
        except usb.core.USBError as e:
            print(f"  [{i}/7] FAIL: {e}")
        time.sleep(0.005)
    time.sleep(0.2)


def get_screen_endpoint_out(dev):
    cfg = dev.get_active_configuration()
    intf = cfg[(SCREEN_INTERFACE, 0)]
    ep = usb.util.find_descriptor(
        intf,
        custom_match=lambda e: (
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR
        ))
    if ep is None:
        sys.exit("Could not find EP5 OUT")
    return ep


# ---------------------------------------------------------------------------
# SysEx handshake thread
# ---------------------------------------------------------------------------

SYSEX_ENTER_HID = "F0 00 40 05 00 00 04 01 00 03 01 F7"
SYSEX_KEEPALIVE = "F0 00 40 05 00 00 04 01 00 50 31 F7"


def find_flx10_midi_port():
    try:
        out = subprocess.check_output(["amidi", "-l"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    import re
    for line in out.splitlines():
        if "FLX10" in line and "hw:" in line:
            m = re.search(r"(hw:[0-9,]+)", line)
            if m:
                return m.group(1)
    return None


class KeepaliveThread(threading.Thread):
    def __init__(self, port, interval_s=0.2):
        super().__init__(daemon=True)
        self.port = port
        self.interval = interval_s
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                subprocess.run(["amidi", "-p", self.port, "-S", SYSEX_KEEPALIVE],
                               check=False, timeout=1,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcapng", help="Path to the Serato pcapng capture")
    ap.add_argument("--speed", type=float, default=10.0,
                    help="Replay speed multiplier (default 10x — capture is ~2min of packets, "
                         "10x makes it ~12s of actual replay).")
    ap.add_argument("--no-unlock", action="store_true",
                    help="Skip vendor unlock (use if Mixxx or rekordbox is providing audio).")
    ap.add_argument("--start", type=float, default=0.0,
                    help="Start replay at this capture timestamp (seconds).")
    ap.add_argument("--end", type=float, default=None,
                    help="End replay at this capture timestamp (seconds).")
    ap.add_argument("--hold", type=float, default=15.0,
                    help="Seconds to keep SysEx keepalive alive after replay (default 15).")
    args = ap.parse_args()

    print(f"Parsing {args.pcapng} …")
    packets = parse_pcapng_ep5_out(args.pcapng)
    print(f"Extracted {len(packets)} EP5 OUT packets total")

    # Filter by time window
    if packets:
        t0_capture = packets[0][0]
        filtered = [(t - t0_capture, p) for t, p in packets
                    if (t - t0_capture) >= args.start
                    and (args.end is None or (t - t0_capture) <= args.end)]
    else:
        filtered = []
    print(f"Replaying {len(filtered)} packets after time-window filter "
          f"({args.start:.1f}..{args.end if args.end else 'end'} sec)")

    dev = find_device()
    if not args.no_unlock:
        vendor_unlock(dev)

    try:
        dev.detach_kernel_driver(SCREEN_INTERFACE)
    except usb.core.USBError as e:
        if e.errno != 61:
            print(f"detach warning: {e}")

    usb.util.claim_interface(dev, SCREEN_INTERFACE)
    ep_out = get_screen_endpoint_out(dev)

    # Send SysEx handshake
    midi_port = find_flx10_midi_port()
    keepalive = None
    if midi_port is None:
        print("WARNING: FLX10 MIDI port not found — SysEx handshake skipped")
    else:
        print(f"SysEx handshake: 03 01 one-shot on {midi_port}")
        subprocess.run(["amidi", "-p", midi_port, "-S", SYSEX_ENTER_HID],
                       check=False, timeout=1,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.1)
        keepalive = KeepaliveThread(midi_port)
        keepalive.start()
        time.sleep(1.0)

    try:
        # Replay packets at original timing / speed
        print(f"\nReplaying {len(filtered)} packets at {args.speed}× speed …")
        t_start_wall = time.time()
        last_print = 0
        for i, (ts_rel, pkt) in enumerate(filtered):
            target_wall = t_start_wall + ts_rel / args.speed
            now = time.time()
            if target_wall > now:
                time.sleep(target_wall - now)
            try:
                ep_out.write(bytes(pkt), timeout=1000)
            except usb.core.USBError as e:
                print(f"  packet {i} send error: {e}")
            if i - last_print >= 500:
                last_print = i
                print(f"  ... {i}/{len(filtered)} packets sent (cmd={pkt[0]:02x} {pkt[1]:02x})")

        print(f"\nReplay complete. Holding for {args.hold:.1f}s (watch the jog wheels) …")
        time.sleep(args.hold)
    finally:
        if keepalive is not None:
            keepalive.stop()
            keepalive.join(timeout=1)
        usb.util.release_interface(dev, SCREEN_INTERFACE)
        try:
            dev.attach_kernel_driver(SCREEN_INTERFACE)
        except Exception:
            pass
        usb.util.dispose_resources(dev)
        print("Done.")


if __name__ == "__main__":
    main()
