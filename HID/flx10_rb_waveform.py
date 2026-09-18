#!/usr/bin/env python3
"""
flx10_rb_waveform.py — feed the DDJ-FLX10 jog screen from Mixxx's analyzed
waveform using the REKORDBOX packet family (0x37 + 0x38), decoded from
captures/rb_jogscreen_full_20260916.pcapng (2026-09-16).

WHY this exists
---------------
Veezuhz's engine spoke Serato `xx36`; rekordbox firmware ignores it. The jog
LCD only paints its waveform when driven with the rekordbox family:

    0x37  low-res full-track OVERVIEW   (30 packets  in the capture,  ~1186 cols)
    0x38  high-res full-track WAVEFORM  (609 packets in the capture, 24766 cols)

Both are BULK uploads sent once at track-load. The firmware then scrolls the
wave itself from the playhead it gets via 0x21 / xx27 (already driven by the
daemon's StatePingThread). So the feed is: on track load, build these two
packet lists from Mixxx's waveform and blast them out ep5.

DECODED WIRE FORMAT (verified against the capture)
--------------------------------------------------
6-byte header, identical for 0x37 and 0x38:
    [0]      0x10                 deck 1 (0x20/0x30/0x40 = decks 2/3/4)
    [1]      0x37 | 0x38          packet type
    [2:4]    LE16 seq             packet index, 1-based
    [4:6]    LE16 total           total packet count in this burst
    [6:128]  payload              122 bytes of column data (all used)

Payload = a CONTINUOUS stream of 3-byte columns; columns straddle packet
boundaries (concatenate every packet's [6:] to rebuild the stream). Each column
is three frequency-band amplitudes. Band->lane order inferred from the capture's
temporal statistics (lane1 smoothest & never silent = LOW; lane0 spiky & often
silent = HIGH; lane2 = MID):

    column = bytes([ HIGH, LOW, MID ])      # BAND_ORDER below — hardware-tunable

Value range ~0..0xB0 in the capture (Mixxx bands are 0..255); pass-through with a
gain + clamp. If the on-screen colours look wrong, permute BAND_ORDER — that is
the ONE thing this decode couldn't pin from bytes alone (needs an audio ground
truth), so it is exposed as a single constant to A/B on hardware in one pass.

0x37 packet #1 additionally carries a 4-byte sub-header `80 01 01 00` before its
columns (constant metadata; replayed verbatim).

Column density: rekordbox's 24766 full-res columns / ~165 s track == 150 cols/s,
the same rate Veezuhz used for PWV5 — so FULL_FPS defaults to 150. The overview
is the full-res stream decimated by OVERVIEW_DECIM (~21x -> ~1186 cols).

This module is import-safe (no pyusb/rtmidi) and offline-testable:
    python3 flx10_rb_waveform.py --selftest [path-to-mixxxdb-analysis-file]
"""

import os
import struct
import zlib

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
FULL_FPS       = 150.0     # 0x38 columns per second (matches capture ~165s->24766)
OVERVIEW_DECIM = 21        # 0x37 = full-res decimated by this factor (~1186 cols)
FW_WAVE_BUFFER = 24500     # firmware wave buffer (~163s @150). Clip long tracks.
BAND_GAIN      = 1.0       # Mixxx band (0..255) -> FLX10 byte, clamped 0..255
# Lane->band mapping (the ONE hardware-tunable). Override live without editing:
#   FLX10_BAND_ORDER=low,mid,high   (comma-separated, from: all,low,mid,high)
# One solid colour usually means all 3 lanes are reading the same/adjacent band
# or the wrong band sits in lane0. Try permutations until colours split.
BAND_ORDER     = tuple(
    (os.environ.get("FLX10_BAND_ORDER", "high,low,mid")).split(",")
)

DECK_BYTES = {1: 0x10, 2: 0x20, 3: 0x30, 4: 0x40}

TYPE_OVERVIEW = 0x37
TYPE_FULL     = 0x38

HDR = 6                    # header length
PKT = 128                  # ep5 report size
PAYLOAD = PKT - HDR        # 122 payload bytes per packet
OVERVIEW_SUBHDR = bytes([0x80, 0x01, 0x01, 0x00])   # 0x37 packet-1 sub-header


# ---------------------------------------------------------------------------
# Mixxx waveform parse (self-contained copy of the daemon's parser so this
# module imports with plain stdlib — no pyusb/rtmidi/sqlite needed to encode).
# Returns (visual_sr, values) with 4 interleaved values per visual frame:
# [all, low, mid, high], each 0..255.  visual_sr is DOUBLE the true fps.
# ---------------------------------------------------------------------------
def _varint(data, pos):
    result = 0; shift = 0; n = 0
    while True:
        b = data[pos + n]; n += 1
        result |= (b & 0x7f) << shift
        if not (b & 0x80):
            return result, n
        shift += 7


def parse_mixxx_waveform(filepath):
    with open(filepath, "rb") as f:
        raw = f.read()
    data = zlib.decompress(raw[4:])
    visual_sr = None
    signal_data = b""
    pos = 0
    while pos < len(data):
        tag = data[pos]; pos += 1
        field = tag >> 3; wire = tag & 7
        if field == 1 and wire == 1:
            visual_sr = struct.unpack("<d", data[pos:pos + 8])[0]; pos += 8
        elif field == 3 and wire == 2:
            length, n = _varint(data, pos); pos += n
            signal_data = data[pos:pos + length]; pos += length
        elif wire == 0:
            _, n = _varint(data, pos); pos += n
        elif wire == 1:
            pos += 8
        elif wire == 2:
            length, n = _varint(data, pos); pos += n + length
        else:
            break
    values = []
    pos = 0
    while pos < len(signal_data):
        tag = signal_data[pos]; pos += 1
        if tag == 0x08:
            v, n = _varint(signal_data, pos); pos += n
            values.append(v)
        else:
            wire = tag & 7
            if wire == 0:
                _, n = _varint(signal_data, pos); pos += n
            elif wire == 1:
                pos += 8
            elif wire == 2:
                length, n = _varint(signal_data, pos); pos += n + length
            else:
                break
    return visual_sr or 441.0, values


# ---------------------------------------------------------------------------
# Band downsample -> FLX10 3-byte columns
# ---------------------------------------------------------------------------
def _clamp8(x):
    x = int(x * BAND_GAIN)
    return 0 if x < 0 else (255 if x > 255 else x)


def waveform_to_columns(visual_sr, values, n_columns):
    """Max-pool Mixxx's (low,mid,high) bands down to n_columns columns and pack
    each column as 3 bytes in BAND_ORDER. Returns a flat bytes stream
    (len == 3 * n_columns)."""
    n_in = len(values) // 4
    if n_in == 0 or n_columns <= 0:
        return b""
    band_idx = {"all": 0, "low": 1, "mid": 2, "high": 3}
    order = [band_idx[b] for b in BAND_ORDER]
    step = n_in / float(n_columns)
    out = bytearray(3 * n_columns)
    for o in range(n_columns):
        start = int(o * step)
        end = int((o + 1) * step)
        if end <= start:
            end = start + 1
        if end > n_in:
            end = n_in
        mx = [0, 0, 0, 0]
        for i in range(start, end):
            base = i * 4
            for k in range(4):
                v = values[base + k]
                if v > mx[k]:
                    mx[k] = v
        for lane in range(3):
            out[o * 3 + lane] = _clamp8(mx[order[lane]])
    return bytes(out)


def columns_for_duration(duration_sec, fps=FULL_FPS):
    n = max(1, int(round(duration_sec * fps)))
    if n > FW_WAVE_BUFFER:
        n = FW_WAVE_BUFFER
    return n


# ---------------------------------------------------------------------------
# Packetiser — flat column stream -> list of 128-byte ep5 packets
# ---------------------------------------------------------------------------
def _packetise(deck, ptype, stream, subheader=b""):
    """Chunk `stream` (raw column bytes) into 128-byte packets with the decoded
    6-byte header. `subheader` (if any) is prepended to packet #1's payload
    (0x37 uses `80 01 01 00`). Columns straddle packets exactly as rekordbox
    does — we never pad mid-stream, only the final packet is zero-filled."""
    deck_byte = DECK_BYTES[deck]
    body = subheader + stream
    # number of packets needed
    total = (len(body) + PAYLOAD - 1) // PAYLOAD
    if total == 0:
        total = 1
    packets = []
    for i in range(total):
        seg = body[i * PAYLOAD:(i + 1) * PAYLOAD]
        p = bytearray(PKT)
        p[0] = deck_byte
        p[1] = ptype
        p[2] = (i + 1) & 0xFF          # seq lo (1-based)
        p[3] = ((i + 1) >> 8) & 0xFF   # seq hi
        p[4] = total & 0xFF            # total lo
        p[5] = (total >> 8) & 0xFF     # total hi
        p[HDR:HDR + len(seg)] = seg
        packets.append(bytes(p))
    return packets


def build_overview_packets(deck, visual_sr, values, n_full_columns):
    """0x37 low-res overview: full-res decimated by OVERVIEW_DECIM."""
    n_ov = max(1, n_full_columns // OVERVIEW_DECIM)
    stream = waveform_to_columns(visual_sr, values, n_ov)
    return _packetise(deck, TYPE_OVERVIEW, stream, subheader=OVERVIEW_SUBHDR)


def build_full_packets(deck, visual_sr, values, n_full_columns):
    """0x38 high-res full-track waveform."""
    stream = waveform_to_columns(visual_sr, values, n_full_columns)
    return _packetise(deck, TYPE_FULL, stream)


def build_screen_upload(deck, analysis_path, duration_sec):
    """Top-level: analysis file + track duration -> (overview_pkts, full_pkts).
    Send overview first, then full, over ep5 at track-load (each 128-byte packet
    straight to EP5_OUT via the relay's _send_screen; NO hidraw 0x00 prefix)."""
    visual_sr, values = parse_mixxx_waveform(analysis_path)
    n_full = columns_for_duration(duration_sec)
    ov = build_overview_packets(deck, visual_sr, values, n_full)
    fl = build_full_packets(deck, visual_sr, values, n_full)
    return ov, fl


# ---------------------------------------------------------------------------
# Offline self-test
# ---------------------------------------------------------------------------
def _selftest(analysis_path=None):
    ok = True

    # 1) framing round-trips against the decoded header spec
    deck = 1
    fake_vsr = 300.0
    fake_vals = []
    import math
    for i in range(4000):
        t = i / 300.0
        allb = int(120 + 100 * math.sin(t * 2))
        low = int(90 + 60 * math.sin(t * 1.0))
        mid = int(70 + 40 * math.sin(t * 3.3))
        hi = int(30 + 30 * (1 if (i % 17) == 0 else 0.2))
        fake_vals += [allb & 255, low & 255, mid & 255, hi & 255]

    assert columns_for_duration(298.3) == FW_WAVE_BUFFER, "long tracks must clip"
    n_full = 24766   # capture size (slightly over FW_WAVE_BUFFER; packetiser does not clip)
    full = build_full_packets(deck, fake_vsr, fake_vals, n_full)
    ov = build_overview_packets(deck, fake_vsr, fake_vals, n_full)

    def check(name, pkts, ptype):
        nonlocal ok
        total = len(pkts)
        for i, p in enumerate(pkts):
            assert len(p) == 128, f"{name}: packet {i} not 128B"
            assert p[0] == 0x10, f"{name}: deck byte"
            assert p[1] == ptype, f"{name}: type byte"
            seq = p[2] | (p[3] << 8)
            tot = p[4] | (p[5] << 8)
            assert seq == i + 1, f"{name}: seq {seq} != {i+1}"
            assert tot == total, f"{name}: total {tot} != {total}"
        print(f"  {name}: {total} packets, header spec OK "
              f"(type=0x{ptype:02x}, seq 1..{total}, total field={pkts[0][4]|(pkts[0][5]<<8)})")

    check("0x38 full", full, TYPE_FULL)
    check("0x37 overview", ov, TYPE_OVERVIEW)

    # 0x37 packet #1 sub-header present
    assert ov[0][6:10] == OVERVIEW_SUBHDR, "0x37 sub-header missing"
    print(f"  0x37 pkt#1 sub-header {ov[0][6:10].hex(' ')} OK")

    # reconstruct the full-res stream from packets and confirm column count
    body = b"".join(p[6:] for p in full)
    ncols = len(body) // 3
    print(f"  0x38 reconstructs to {ncols} columns "
          f"(capture had 24766 for a ~165s track @150fps)")

    # 2) against a REAL Mixxx analysis file if one was given
    if analysis_path and os.path.exists(analysis_path):
        vsr, vals = parse_mixxx_waveform(analysis_path)
        nframes = len(vals) // 4
        print(f"  real analysis: visual_sr={vsr:.1f} frames={nframes} "
              f"(true fps ~{vsr/2:.1f}, ~{nframes/(vsr/2):.1f}s)")
        cols = waveform_to_columns(vsr, vals, columns_for_duration(nframes / (vsr / 2)))
        nz = sum(1 for b in cols if b)
        print(f"  real -> {len(cols)//3} columns, {100*nz/max(1,len(cols)):.1f}% non-zero bytes")

    print("SELFTEST PASS" if ok else "SELFTEST FAIL")
    return ok


if __name__ == "__main__":
    import sys
    ap = sys.argv[1:]
    if ap and ap[0] == "--selftest":
        path = ap[1] if len(ap) > 1 else None
        _selftest(path)
    else:
        print(__doc__)
