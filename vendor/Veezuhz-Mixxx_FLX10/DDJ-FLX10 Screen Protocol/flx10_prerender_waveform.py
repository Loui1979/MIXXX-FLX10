#!/usr/bin/env python3
"""
flx10_prerender_waveform.py — convert a Mixxx-analyzed track's waveform into
a PWV5 LE16 cache file suitable for FLX10 xx 36 upload.

Mixxx already analyzes every track and stores the waveform binary in
~/.mixxx/analysis/<id>. Format: 4-byte BE uncompressed-length, then zlib data,
which decompresses to a protobuf with:
  visualSampleRate (typically 441.0)
  audioVisualRatio (~109 = 48kHz/441)
  signal.value[]  repeated uint32 — 4 packed bands per frame:
                   value[0]=all, [1]=low, [2]=mid, [3]=high, then next frame

We downsample from Mixxx's 441 fps to Pioneer's 150 fps (≈2.94:1) by taking
the max amplitude per output frame, then encode each output frame as a PWV5
LE16 entry with:
  height = clamp(all / 255 * 31, 0, 31)
  r      = clamp(low / 255 * 7, 0, 7)   # Pioneer convention: low band → red
  g      = clamp(mid / 255 * 7, 0, 7)   #                     mid band → green
  b      = clamp(high / 255 * 7, 0, 7)  #                     high band → blue

Usage:
  python3 flx10_prerender_waveform.py 1015                       # by Mixxx track_id
  python3 flx10_prerender_waveform.py /path/to/track.mp3         # by file path
  python3 flx10_prerender_waveform.py --all                      # all analyzed tracks
  python3 flx10_prerender_waveform.py --list                     # list available tracks

Output goes to ~/.flx10-cache/<track_id>.pwv5
"""

import argparse
import os
import sqlite3
import struct
import sys
import zlib

MIXXX_DB        = os.path.expanduser("~/.mixxx/mixxxdb.sqlite")
MIXXX_ANALYSIS  = os.path.expanduser("~/.mixxx/analysis")
CACHE_DIR       = os.path.expanduser("~/.flx10-cache")

PWV5_FPS = 150     # Pioneer half-frame rate


# ---------------------------------------------------------------------------
# Protobuf helpers (minimal)
# ---------------------------------------------------------------------------

def varint(data, pos):
    result = 0
    shift = 0
    n = 0
    while True:
        b = data[pos + n]
        n += 1
        result |= (b & 0x7f) << shift
        if not (b & 0x80):
            return result, n
        shift += 7


def parse_mixxx_waveform_file(filepath):
    with open(filepath, "rb") as f:
        raw = f.read()
    uncompressed_size = int.from_bytes(raw[:4], "big")
    data = zlib.decompress(raw[4:])
    if len(data) != uncompressed_size:
        raise ValueError(f"size mismatch: expected {uncompressed_size}, got {len(data)}")

    visual_sr = None
    audio_visual_ratio = None
    signal_data = b""
    pos = 0
    while pos < len(data):
        tag = data[pos]; pos += 1
        field = tag >> 3
        wire = tag & 7
        if field == 1 and wire == 1:
            visual_sr = struct.unpack("<d", data[pos:pos+8])[0]
            pos += 8
        elif field == 2 and wire == 1:
            audio_visual_ratio = struct.unpack("<d", data[pos:pos+8])[0]
            pos += 8
        elif field == 3 and wire == 2:
            length, n = varint(data, pos); pos += n
            signal_data = data[pos:pos+length]
            pos += length
        elif wire == 0:
            _, n = varint(data, pos); pos += n
        elif wire == 1:
            pos += 8
        elif wire == 2:
            length, n = varint(data, pos); pos += n + length
        else:
            break

    values = []
    pos = 0
    while pos < len(signal_data):
        tag = signal_data[pos]; pos += 1
        if tag == 0x08:
            v, n = varint(signal_data, pos); pos += n
            values.append(v)
        else:
            wire = tag & 7
            if wire == 0:
                _, n = varint(signal_data, pos); pos += n
            elif wire == 1:
                pos += 8
            elif wire == 2:
                length, n = varint(signal_data, pos); pos += n + length
            else:
                break

    return {
        "visualSampleRate": visual_sr,
        "audioVisualRatio": audio_visual_ratio,
        "values": values,
    }


# ---------------------------------------------------------------------------
# PWV5 encoding
# ---------------------------------------------------------------------------

def encode_pwv5_le(r, g, b, h):
    v = ((r & 7) << 13) | ((g & 7) << 10) | ((b & 7) << 7) | ((h & 0x1F) << 2)
    return v & 0xFF, (v >> 8) & 0xFF


def convert_to_pwv5(waveform, target_fps=PWV5_FPS):
    """Resample Mixxx waveform (visualSampleRate Hz) → target_fps Hz PWV5 entries.
    Take max per output frame to preserve peaks."""
    values = waveform["values"]
    visual_sr = waveform["visualSampleRate"] or 441.0
    n_in_frames = len(values) // 4
    if n_in_frames == 0:
        return bytearray()

    in_fps_per_out_fps = visual_sr / target_fps     # e.g. 441/150 = 2.94
    n_out_frames = int(n_in_frames / in_fps_per_out_fps)

    out = bytearray(2 * n_out_frames)
    for o in range(n_out_frames):
        start = int(o * in_fps_per_out_fps)
        end   = int((o + 1) * in_fps_per_out_fps)
        if end <= start:
            end = start + 1
        if end > n_in_frames:
            end = n_in_frames

        max_all = max_low = max_mid = max_high = 0
        for i in range(start, end):
            base = i * 4
            if values[base + 0] > max_all:  max_all  = values[base + 0]
            if values[base + 1] > max_low:  max_low  = values[base + 1]
            if values[base + 2] > max_mid:  max_mid  = values[base + 2]
            if values[base + 3] > max_high: max_high = values[base + 3]

        h = min(31, max_all * 31 // 255)
        r = min(7, max_low * 7 // 255)
        g = min(7, max_mid * 7 // 255)
        b = min(7, max_high * 7 // 255)
        lo, hi = encode_pwv5_le(r, g, b, h)
        out[2*o]     = lo
        out[2*o + 1] = hi

    return out


# ---------------------------------------------------------------------------
# Mixxx DB lookups
# ---------------------------------------------------------------------------

def db_open():
    if not os.path.exists(MIXXX_DB):
        sys.exit(f"Mixxx database not found at {MIXXX_DB}")
    return sqlite3.connect(MIXXX_DB)


def find_analysis_id_for_track(conn, track_id, wave_type=1):
    """wave_type 1 = Waveform-5.0 (detail), 2 = WaveformSummary-5.0 (overview)."""
    cur = conn.execute(
        "SELECT id FROM track_analysis WHERE track_id = ? AND type = ? LIMIT 1",
        (track_id, wave_type),
    )
    row = cur.fetchone()
    return row[0] if row else None


def find_track_by_path(conn, path):
    cur = conn.execute(
        "SELECT id, location FROM track_locations WHERE location = ?",
        (os.path.abspath(path),),
    )
    row = cur.fetchone()
    return row[0] if row else None


def list_available_tracks(conn, limit=20):
    cur = conn.execute("""
        SELECT tl.id, tl.filename, ta.id AS analysis_id
        FROM track_locations tl
        JOIN track_analysis ta ON ta.track_id = tl.id
        WHERE ta.type = 1
        ORDER BY tl.id DESC
        LIMIT ?
    """, (limit,))
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Render orchestration
# ---------------------------------------------------------------------------

def render_one(conn, track_id, force=False):
    analysis_id = find_analysis_id_for_track(conn, track_id, wave_type=1)
    if analysis_id is None:
        print(f"  track_id={track_id}: no Waveform-5.0 analysis available, skipping")
        return None

    out_path = os.path.join(CACHE_DIR, f"{track_id}.pwv5")
    if os.path.exists(out_path) and not force:
        print(f"  track_id={track_id}: cache exists, skipping (use --force to regenerate)")
        return out_path

    analysis_path = os.path.join(MIXXX_ANALYSIS, str(analysis_id))
    if not os.path.exists(analysis_path):
        print(f"  track_id={track_id}: analysis file {analysis_path} missing")
        return None

    try:
        waveform = parse_mixxx_waveform_file(analysis_path)
    except Exception as e:
        print(f"  track_id={track_id}: parse failed: {e}")
        return None

    pwv5 = convert_to_pwv5(waveform)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(pwv5)
    duration = (len(pwv5) / 2) / PWV5_FPS
    print(f"  track_id={track_id}: wrote {out_path}  ({len(pwv5)} bytes, "
          f"{len(pwv5)//2} entries, ~{duration:.1f}s)")
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?",
                    help="Mixxx track_id (integer) or audio file path. Omit if using --all/--list.")
    ap.add_argument("--all", action="store_true",
                    help="Render every analyzed track in Mixxx's library.")
    ap.add_argument("--list", action="store_true",
                    help="List recent tracks with available analysis.")
    ap.add_argument("--force", action="store_true",
                    help="Re-render even if cache file already exists.")
    args = ap.parse_args()

    conn = db_open()

    if args.list:
        print("Recent tracks with available Waveform-5.0 analysis:")
        for row in list_available_tracks(conn):
            tid, fn, aid = row
            cache_exists = "✓" if os.path.exists(os.path.join(CACHE_DIR, f"{tid}.pwv5")) else " "
            print(f"  [{cache_exists}] track_id={tid:>6}  analysis_id={aid:>6}  {fn}")
        return

    if args.all:
        cur = conn.execute("""
            SELECT DISTINCT tl.id FROM track_locations tl
            JOIN track_analysis ta ON ta.track_id = tl.id WHERE ta.type = 1
        """)
        track_ids = [row[0] for row in cur.fetchall()]
        print(f"Rendering {len(track_ids)} tracks …")
        ok = fail = 0
        for tid in track_ids:
            try:
                if render_one(conn, tid, force=args.force):
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                print(f"  track_id={tid}: error {e}")
                fail += 1
        print(f"Done: {ok} rendered, {fail} skipped/failed")
        return

    if not args.target:
        ap.print_help()
        sys.exit(1)

    # Resolve target → track_id
    try:
        track_id = int(args.target)
    except ValueError:
        track_id = find_track_by_path(conn, args.target)
        if track_id is None:
            sys.exit(f"No track found at path: {args.target}")

    render_one(conn, track_id, force=args.force)


if __name__ == "__main__":
    main()
