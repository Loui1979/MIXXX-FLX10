#!/usr/bin/env python3
"""
flx10_replay.py — replay captured EP5 OUT packets to the FLX10 screen.

Reads a pcapng file, extracts every 128-byte HID output report sent to the
FLX10 during the captured session, and writes them back through /dev/hidrawN
in original timing order.

Prerequisites:
  - FLX10 plugged in and unlocked (run flx10_unlock_v2.py first)
  - Mixxx, rekordbox, and any other software using the FLX10 closed
  - /dev/hidrawN identified for the FLX10 (default: auto-detect)
  - Run as root

Usage:
  sudo python3 flx10_replay.py <capture.pcapng>
  sudo python3 flx10_replay.py <capture.pcapng> --window 4 12   # t=4s to 12s
  sudo python3 flx10_replay.py <capture.pcapng> --hidraw /dev/hidraw6

This replays everything the host sent to the FLX10's screen interface, at
original cadence. If the protocol decoding is correct, the screen should
show whatever it showed when the capture was made.
"""

import argparse
import glob
import os
import struct
import sys
import time


def find_flx10_hidraw():
    """Locate the FLX10 hidraw device by walking sysfs."""
    for path in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            uevent = open(os.path.join(path, "device", "uevent")).read()
            if "2B73" in uevent.upper() and "0041" in uevent:
                return "/dev/" + os.path.basename(path)
        except OSError:
            continue
    return None


def parse_pcapng_ep5_out(pcap_path, device_addr=None):
    """Parse pcapng, yield (timestamp, 128-byte-payload) for EP5 OUT packets.

    USBPcap frames are 27-byte URB header + payload. We want host->device
    interrupt transfers on endpoint 5 (length 155 total on the wire, 128 payload).
    """
    packets = []
    with open(pcap_path, "rb") as f:
        data = f.read()

    offset = 0
    while offset < len(data):
        if offset + 8 > len(data):
            break
        block_type, block_len = struct.unpack("<II", data[offset:offset+8])
        if block_len < 12 or offset + block_len > len(data):
            break

        if block_type == 0x00000006:  # Enhanced Packet Block
            body = data[offset+8:offset+block_len-4]
            # iface(4) + ts_h(4) + ts_l(4) + cap_len(4) + orig_len(4) + pkt data
            if len(body) >= 20:
                iface, ts_h, ts_l, cap_len, orig_len = struct.unpack("<IIIII", body[:20])
                pkt = body[20:20+cap_len]
                ts = ((ts_h << 32) | ts_l) / 1e6

                # USBPcap URB header layout: first byte is headerLen (typically 0x1b = 27)
                # We want: host->device, interrupt transfer, endpoint 5, length 128
                if len(pkt) >= 155 and pkt[0] == 0x1b:
                    # URB header offsets (USBPcap format):
                    #   [0x14] = device address (1 byte)
                    #   [0x15] = endpoint+direction byte (low 4 bits = ep, top bit = dir)
                    #   [0x16] = transfer type (0=iso, 1=int, 2=ctrl, 3=bulk)
                    #   [0x17] = data length (4 bytes LE at 0x18?)
                    addr = pkt[0x14]
                    ep_byte = pkt[0x15]
                    xfer_type = pkt[0x16]

                    # Host->device EP 5 interrupt
                    if (ep_byte & 0x7F) == 5 and (ep_byte & 0x80) == 0 \
                       and xfer_type == 1 and len(pkt) - 27 == 128:
                        if device_addr is None or addr == device_addr:
                            payload = pkt[27:27+128]
                            packets.append((ts, payload, addr))

        offset += block_len

    return packets


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pcap", help="Path to the .pcapng capture")
    ap.add_argument("--hidraw", help="Path to FLX10 hidraw device (auto-detect if omitted)")
    ap.add_argument("--window", nargs=2, type=float, metavar=("START", "END"),
                    help="Only replay packets in this time window (seconds)")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="Replay speed multiplier (1.0 = original, 2.0 = 2x faster)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and report what would be sent, don't actually write")
    ap.add_argument("--device-addr", type=int, default=None,
                    help="Filter to only this USB device address (rare; usually auto)")
    args = ap.parse_args()

    # Locate hidraw
    if args.hidraw:
        hidraw = args.hidraw
    else:
        hidraw = find_flx10_hidraw()
        if not hidraw:
            sys.exit("Could not find FLX10 hidraw. Pass --hidraw /dev/hidrawN explicitly.")
    print(f"Using {hidraw}")

    # Parse capture
    print(f"Parsing {args.pcap} ...")
    pkts = parse_pcapng_ep5_out(args.pcap, args.device_addr)
    print(f"  Extracted {len(pkts)} EP5 OUT packets")

    if not pkts:
        sys.exit("No EP5 OUT packets found. Check device address with --device-addr.")

    # If no device-addr filter was set, pick the addr with the most packets
    if args.device_addr is None:
        from collections import Counter
        addr_counts = Counter(p[2] for p in pkts)
        chosen_addr = addr_counts.most_common(1)[0][0]
        pkts = [(t, p) for t, p, a in pkts if a == chosen_addr]
        print(f"  Auto-selected device address {chosen_addr}: {len(pkts)} packets")
    else:
        pkts = [(t, p) for t, p, _ in pkts]

    # Apply window filter
    if args.window:
        s, e = args.window
        pkts = [(t, p) for t, p in pkts if s <= t <= e]
        print(f"  Filtered to window [{s}, {e}]: {len(pkts)} packets")

    if not pkts:
        sys.exit("No packets after filtering.")

    # Normalize timestamps to start at 0
    t0 = pkts[0][0]
    pkts = [(t - t0, p) for t, p in pkts]
    duration = pkts[-1][0]
    print(f"  Replay duration: {duration:.2f}s ({len(pkts)} packets)")

    # Show packet type breakdown
    from collections import Counter
    types = Counter((p[0], p[1]) for _, p in pkts)
    print(f"  Packet type breakdown:")
    for (b0, b1), n in types.most_common():
        print(f"    {b0:02x} {b1:02x}: {n} packets")

    if args.dry_run:
        print("\nDry run — not writing.")
        return

    # Open hidraw and replay
    print(f"\nReplaying to {hidraw} at speed {args.speed}x ...")
    try:
        fd = os.open(hidraw, os.O_WRONLY)
    except PermissionError:
        sys.exit(f"Cannot open {hidraw} — run with sudo.")

    start = time.perf_counter()
    sent = 0
    errors = 0
    try:
        for ts, payload in pkts:
            target = start + (ts / args.speed)
            now = time.perf_counter()
            if target > now:
                time.sleep(target - now)
            try:
                os.write(fd, payload)
                sent += 1
            except OSError as e:
                errors += 1
                if errors <= 3:
                    print(f"  write failed at t={ts:.3f}s: {e}")
    finally:
        os.close(fd)

    print(f"\nDone. Sent {sent}/{len(pkts)} packets, {errors} errors.")
    if errors == 0:
        print("All writes accepted. If the screen didn't change, the protocol")
        print("likely needs additional setup beyond what this capture contains")
        print("(e.g., a class-specific control transfer before the bulk burst).")


if __name__ == "__main__":
    main()
