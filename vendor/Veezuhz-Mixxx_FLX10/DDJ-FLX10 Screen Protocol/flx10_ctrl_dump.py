#!/usr/bin/env python3
"""
flx10_ctrl_dump.py v2 — dump USB control transfers from a USBPcap pcapng.

USBPcap URB header layout (27 bytes):
  [0x00-01]  headerLen (LE16, always 0x001b)
  [0x02-09]  irpId (8 bytes)
  [0x0A-0D]  status (LE32)
  [0x0E-0F]  function (LE16)
  [0x10]     info byte (bit 0: 0=request/submit, 1=response/complete)
  [0x11-12]  bus (LE16)
  [0x13-14]  device (LE16)
  [0x15]     endpoint (bit 7 = IN/OUT, low 4 bits = ep number)
  [0x16]     transfer type (0=iso, 1=int, 2=ctrl, 3=bulk)
  [0x17-1A]  dataLength (LE32)

For control transfers, the 8-byte setup packet follows the URB header at
offset 0x1B in the SUBMIT event (info byte bit 0 = 0).
"""

import argparse
import struct
import sys
from collections import Counter


def parse_pcapng(path, t_start=None, t_end=None):
    out = []
    with open(path, "rb") as f:
        data = f.read()
    off = 0
    while off < len(data):
        if off + 8 > len(data):
            break
        btype, blen = struct.unpack("<II", data[off:off+8])
        if blen < 12 or off + blen > len(data):
            break
        if btype == 0x00000006:
            body = data[off+8:off+blen-4]
            if len(body) >= 20:
                iface, ts_h, ts_l, caplen, origlen = struct.unpack("<IIIII", body[:20])
                pkt = body[20:20+caplen]
                ts = ((ts_h << 32) | ts_l) / 1e6
                if (t_start is None or ts >= t_start) and (t_end is None or ts <= t_end):
                    if len(pkt) >= 27 and pkt[0] == 0x1b:
                        out.append((ts, pkt))
        off += blen
    return out


def decode_request(bm, br):
    typ = (bm >> 5) & 0x3
    direction = "IN" if (bm & 0x80) else "OUT"
    recipient_idx = bm & 0x1f
    recipients = ["device", "iface", "ep", "other"]
    recipient = recipients[recipient_idx] if recipient_idx < 4 else f"r{recipient_idx}"
    if typ == 0:
        names = {0: "GET_STATUS", 1: "CLEAR_FEATURE", 3: "SET_FEATURE",
                 5: "SET_ADDRESS", 6: "GET_DESCRIPTOR", 7: "SET_DESCRIPTOR",
                 8: "GET_CONFIG", 9: "SET_CONFIG",
                 10: "GET_INTERFACE", 11: "SET_INTERFACE", 12: "SYNCH_FRAME"}
        return f"STD/{direction}/{recipient} {names.get(br, f'b{br}')}"
    elif typ == 1:
        names = {0x01: "GET_REPORT", 0x02: "GET_IDLE", 0x03: "GET_PROTOCOL",
                 0x09: "SET_REPORT", 0x0a: "SET_IDLE", 0x0b: "SET_PROTOCOL"}
        return f"HID/{direction}/{recipient} {names.get(br, f'b{br}')}"
    elif typ == 2:
        return f"VEND/{direction}/{recipient} b{br}"
    return f"RSV/{direction}/{recipient} b{br}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("--window", nargs=2, type=float, metavar=("START", "END"),
                    default=(0, 200))
    ap.add_argument("--device-addr", type=int, default=None)
    ap.add_argument("--all-stages", action="store_true")
    args = ap.parse_args()

    packets = parse_pcapng(args.pcap, *args.window)
    print(f"Read {len(packets)} packets in window {args.window[0]}..{args.window[1]}s")

    if args.device_addr is None:
        addr_counts = Counter()
        for ts, pkt in packets:
            if len(pkt) >= 0x16 and pkt[0x16] == 2:
                addr = struct.unpack("<H", pkt[0x13:0x15])[0]
                addr_counts[addr] += 1
        if not addr_counts:
            print("No control transfers found at all.")
            return
        print(f"Device addresses with control transfers: {dict(addr_counts)}")
        candidates = {a: c for a, c in addr_counts.items() if a != 0}
        args.device_addr = max(candidates, key=candidates.get) if candidates else max(addr_counts, key=addr_counts.get)
        print(f"Auto-selected device address: {args.device_addr}")

    print(f"\nControl transfers for device {args.device_addr}:\n")
    print(f"  {'time':>11}  {'stage':>4}  {'bmReq':>5}  {'bReq':>5}  "
          f"{'wValue':>6}  {'wIndex':>6}  {'wLen':>5}  type")
    print(f"  {'-'*11}  {'-'*4}  {'-'*5}  {'-'*5}  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*4}")

    rows = []
    for ts, pkt in packets:
        if len(pkt) < 27: continue
        addr = struct.unpack("<H", pkt[0x13:0x15])[0]
        if addr != args.device_addr: continue
        if pkt[0x16] != 2: continue
        info = pkt[0x10]
        is_response = bool(info & 0x01)
        if is_response and not args.all_stages: continue
        if len(pkt) < 27 + 8: continue
        setup = pkt[27:35]
        bm, br = setup[0], setup[1]
        wValue = struct.unpack("<H", setup[2:4])[0]
        wIndex = struct.unpack("<H", setup[4:6])[0]
        wLength = struct.unpack("<H", setup[6:8])[0]
        name = decode_request(bm, br)
        stage = "RESP" if is_response else "SUB"
        rows.append((ts, stage, bm, br, wValue, wIndex, wLength, name))

    for r in rows:
        ts, stage, bm, br, wV, wI, wL, name = r
        print(f"  {ts:11.3f}  {stage:>4}  0x{bm:02x}  0x{br:02x}  "
              f"0x{wV:04x}  0x{wI:04x}  {wL:5d}  {name}")

    print(f"\nTotal: {len(rows)} control transfers shown\n")
    print("=== Summary by type ===")
    by_type = Counter(r[7] for r in rows)
    for name, n in by_type.most_common():
        print(f"  {n:4d}  {name}")

    print("\n=== Interesting transfers ===")
    interesting = [r for r in rows if
                   (r[7].startswith("HID/OUT") and r[3] == 0x09) or
                   (r[7].startswith("STD") and r[3] == 0x0b) or
                   (r[7].startswith("VEND"))]
    for r in interesting:
        ts, stage, bm, br, wV, wI, wL, name = r
        extra = ""
        if br == 0x0b:
            extra = f"  (alt={wV} on interface {wI})"
        elif br == 0x09:
            extra = f"  (report 0x{wV:04x} on iface {wI}, {wL} bytes)"
        print(f"  t={ts:7.3f}  {name}  wValue=0x{wV:04x}  wIndex=0x{wI:04x}  wLen={wL}{extra}")


if __name__ == "__main__":
    main()
