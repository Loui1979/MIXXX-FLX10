#!/usr/bin/env python3
"""flx10-parse.py — decode a usbmon pcapng (linktype 220) without tshark/root.

Pure stdlib. Reassembles USB-MIDI SysEx and dumps per-endpoint traffic for the
DDJ-FLX10 (or any USB device) so we can reverse-engineer the rekordbox protocol.

Usage:
    python3 flx10-parse.py capture.pcapng            # auto-pick busiest device
    python3 flx10-parse.py capture.pcapng --dev 11   # force device address
    python3 flx10-parse.py capture.pcapng --sysex    # only distinct SysEx OUT
"""
import struct, sys, argparse
from collections import Counter, OrderedDict

def iter_epb(path):
    with open(path, 'rb') as f:
        blob = f.read()
    off, n = 0, len(blob)
    while off + 8 <= n:
        btype = struct.unpack_from('<I', blob, off)[0]
        blen = struct.unpack_from('<I', blob, off + 4)[0]
        if blen < 12 or off + blen > n:
            break
        if btype == 0x00000006:  # Enhanced Packet Block
            caplen = struct.unpack_from('<I', blob, off + 20)[0]
            yield blob[off + 28:off + 28 + caplen]
        off += blen

def parse(pkt):
    if len(pkt) < 64:
        return None
    (urb, ev, xfer, ep, dev, bus, fs, fd, tsc, tsu,
     status, length, lc) = struct.unpack_from('<QBBBBHbbqiiII', pkt, 0)
    return dict(ev=chr(ev), xfer=xfer, ep=ep, dev=dev, bus=bus,
                status=status, length=length, lc=lc,
                setup=pkt[40:48], data=pkt[64:64 + lc])

XFER = {0: 'ISO', 1: 'INT', 2: 'CTRL', 3: 'BULK'}

def usbmidi_to_stream(data):
    out = bytearray()
    for j in range(0, len(data), 4):
        ev = data[j:j + 4]
        if len(ev) < 4:
            break
        cin = ev[0] & 0x0f
        if cin == 0:
            continue
        if cin == 0x5 or cin == 0xf:
            out += ev[1:2]
        elif cin == 0x6:
            out += ev[1:3]
        else:
            out += ev[1:4]
    return bytes(out)

def pick_dev(path):
    c = Counter()
    for pkt in iter_epb(path):
        r = parse(pkt)
        if r:
            c[r['dev']] += 1
    return c.most_common(1)[0][0]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pcap')
    ap.add_argument('--dev', type=int, default=None)
    ap.add_argument('--sysex', action='store_true')
    a = ap.parse_args()
    dev = a.dev if a.dev is not None else pick_dev(a.pcap)
    print(f"# device address {dev}\n")

    if a.sysex:
        msgs, first = OrderedDict(), {}
        buf = bytearray(); idx = 0
        for pkt in iter_epb(a.pcap):
            r = parse(pkt)
            if not r or r['dev'] != dev:
                continue
            idx += 1
            ep = r['ep']
            if r['xfer'] == 3 and (ep & 0x7f) == 3 and not (ep & 0x80) \
                    and r['lc'] > 0 and r['ev'] == 'S':
                buf += usbmidi_to_stream(r['data'])
                while True:
                    s = buf.find(0xF0)
                    if s < 0:
                        buf = bytearray(); break
                    e = buf.find(0xF7, s)
                    if e < 0:
                        buf = buf[s:]; break
                    h = bytes(buf[s:e + 1]).hex()
                    msgs[h] = msgs.get(h, 0) + 1
                    first.setdefault(h, idx)
                    buf = buf[e + 1:]
        for h, cnt in sorted(msgs.items(), key=lambda x: first[x[0]]):
            print(f"[#{first[h]}] x{cnt}: {h}")
        return

    byep = Counter()
    for pkt in iter_epb(a.pcap):
        r = parse(pkt)
        if not r or r['dev'] != dev:
            continue
        ep = r['ep']
        d = 'IN' if ep & 0x80 else 'OUT'
        byep[(XFER.get(r['xfer']), ep & 0x7f, d)] += 1
    print("# endpoint summary (xfer, endpoint, dir): count")
    for k, v in byep.most_common():
        print(f"  {k}: {v}")

if __name__ == '__main__':
    main()
