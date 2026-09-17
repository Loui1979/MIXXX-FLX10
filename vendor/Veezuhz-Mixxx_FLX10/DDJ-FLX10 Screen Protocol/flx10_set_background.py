#!/usr/bin/env python3
"""
flx10_set_background.py v2 — upload a custom background to FLX10 jog wheels.

PROTOCOL (v2 — includes ACK handshake discovered from capture):
  1. Open /dev/hidrawN for READ+WRITE (must read EP4 IN ACKs)
  2. Write header packet:   00 D0 [slot] [size_LE16] [123 zeros]
  3. Read EP4 IN — device responds with 2x "00 D8 00..." reports (drain them)
  4. Write data chunks:     00 D1 [seg] 00 [total] [123 bytes]
       First chunk's body starts with 4-byte sub-header (00 SS SS 00) + JPEG
       Subsequent chunks are pure JPEG continuation bytes
  5. For DELETE: header with size=0, drain ACKs, then 00 D7 00 00 00

Usage:
  sudo python3 flx10_set_background.py <image_path> [--slot 1]
  sudo python3 flx10_set_background.py --clear
  sudo python3 flx10_set_background.py --replay <pcapng>   # diagnostic
"""

import argparse
import glob
import os
import select
import struct
import sys
import time
from io import BytesIO


def find_flx10_hidraw():
    for path in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            uevent = open(os.path.join(path, "device", "uevent")).read()
            if "2B73" in uevent.upper() and "0041" in uevent:
                return "/dev/" + os.path.basename(path)
        except OSError:
            continue
    return None


def make_jpeg(image_path, size=(240, 240), quality=60):
    from PIL import Image
    img = Image.open(image_path).convert("RGB")
    img = img.resize(size, Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def drain_acks(fd, timeout=0.2, expect_byte=0xD8):
    """Read any pending input reports until quiet for `timeout` seconds.
    Returns the list of reports received."""
    reports = []
    end_time = time.monotonic() + timeout
    while time.monotonic() < end_time:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            break
        try:
            data = os.read(fd, 64)
            if data:
                reports.append(data)
                # Got expected ACK — keep reading for a moment, then return
                if data[1] == expect_byte:
                    end_time = min(end_time, time.monotonic() + 0.02)
        except OSError:
            break
    return reports


def upload(fd, jpeg_bytes, slot=1):
    size = len(jpeg_bytes)
    first_jpeg = 119
    rest = jpeg_bytes[first_jpeg:]
    total_segs = 1 + (len(rest) + 122) // 123

    # Drain any leftover input first
    drain_acks(fd, timeout=0.05)

    # 1. Header
    hdr = bytes([0x00, 0xD0, slot, size & 0xFF, (size >> 8) & 0xFF])
    hdr += b"\x00" * (128 - len(hdr))
    os.write(fd, hdr)
    print(f"  -> header (size={size}, total_segs={total_segs})")

    # 2. Wait for and drain D8 ACKs
    acks = drain_acks(fd, timeout=0.3)
    print(f"  <- {len(acks)} ACK report(s)")
    for a in acks[:3]:
        print(f"     {a[:8].hex()}")
    if not acks:
        print("  WARNING: no ACK received. Device may not be in upload mode.")

    # 3. First data chunk
    sub_hdr = bytes([0x00, size & 0xFF, (size >> 8) & 0xFF, 0x00])
    pkt = bytes([0x00, 0xD1, 1, 0x00, total_segs]) + sub_hdr + jpeg_bytes[:first_jpeg]
    pkt += b"\x00" * (128 - len(pkt))
    os.write(fd, pkt)

    # 4. Subsequent chunks
    seg = 2
    for i in range(0, len(rest), 123):
        chunk = rest[i:i+123]
        pkt = bytes([0x00, 0xD1, seg, 0x00, total_segs]) + chunk
        pkt += b"\x00" * (128 - len(pkt))
        os.write(fd, pkt)
        seg += 1
        time.sleep(0.001)

    print(f"  -> sent {total_segs} data chunks")

    # Drain any post-upload ACKs
    final_acks = drain_acks(fd, timeout=0.2)
    if final_acks:
        print(f"  <- {len(final_acks)} post-upload report(s)")


def clear_bg(fd):
    drain_acks(fd, timeout=0.05)
    # Header with size=0
    hdr = bytes([0x00, 0xD0, 0x00, 0x00, 0x00]) + b"\x00" * 123
    os.write(fd, hdr)
    print("  -> D0 (clear)")
    acks = drain_acks(fd, timeout=0.3)
    print(f"  <- {len(acks)} ACK(s)")
    # Then D7 commit
    commit = bytes([0x00, 0xD7, 0x00, 0x00, 0x00]) + b"\x00" * 123
    os.write(fd, commit)
    print("  -> D7 (commit)")


def replay_pcap(fd, pcap_path):
    """Diagnostic: replay every EP5 OUT packet from a capture byte-for-byte,
    with proper read-between-writes to drain ACKs."""
    with open(pcap_path, "rb") as f:
        data = f.read()
    packets = []
    off = 0
    while off < len(data):
        if off + 8 > len(data): break
        btype, blen = struct.unpack("<II", data[off:off+8])
        if blen < 12 or off + blen > len(data): break
        if btype == 0x00000006:
            body = data[off+8:off+blen-4]
            if len(body) >= 20:
                iface, ts_h, ts_l, caplen, origlen = struct.unpack("<IIIII", body[:20])
                pkt = body[20:20+caplen]
                if len(pkt) >= 28 and pkt[0] in (0x1b, 0x1c):
                    if pkt[0x16] == 1 and pkt[0x15] == 0x05:
                        hdr_len = pkt[0]
                        payload = pkt[hdr_len:]
                        if len(payload) == 128:
                            packets.append((ts_h, ts_l, payload))
        off += blen

    print(f"Found {len(packets)} EP5 OUT packets in capture")
    drain_acks(fd, timeout=0.1)

    sent = 0
    for i, (_, _, p) in enumerate(packets):
        os.write(fd, p)
        sent += 1
        # After a D0 header, drain ACKs before continuing
        if p[1] == 0xD0 or p[1] == 0xD7:
            acks = drain_acks(fd, timeout=0.3)
            print(f"  [{i}] sent {p[:5].hex()}  <- {len(acks)} ack(s)")
        else:
            time.sleep(0.001)
    print(f"\nSent {sent} packets total")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?")
    ap.add_argument("--slot", type=int, default=1)
    ap.add_argument("--hidraw")
    ap.add_argument("--clear", action="store_true")
    ap.add_argument("--replay", help="Diagnostic: replay an EP5 OUT capture")
    ap.add_argument("--quality", type=int, default=60)
    args = ap.parse_args()

    hidraw = args.hidraw or find_flx10_hidraw()
    if not hidraw:
        sys.exit("FLX10 hidraw not found.")
    print(f"Using {hidraw}")

    try:
        fd = os.open(hidraw, os.O_RDWR | os.O_NONBLOCK)
    except PermissionError:
        sys.exit("Need sudo.")

    try:
        if args.replay:
            replay_pcap(fd, args.replay)
        elif args.clear:
            clear_bg(fd)
        elif args.image:
            jpeg = make_jpeg(args.image, quality=args.quality)
            print(f"JPEG: {len(jpeg)} bytes")
            upload(fd, jpeg, slot=args.slot)
        else:
            sys.exit("Need image path, --clear, or --replay")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
