#!/usr/bin/env python3
"""
flx10_relay_daemon.py — full coexistence daemon for the DDJ-FLX10.

Owns the FLX10 via libusb the WHOLE time (keeps HID display mode alive — the
ep0 mode switch needs full device ownership and does not survive a kernel
rebind). Bridges MIDI to Mixxx through a VIRTUAL ALSA port so Mixxx can still
run the controller mapping + JS while libusb holds the device.

Single process (one libusb handle can't be shared):
  USB  : ep0 mode switch + ep3/ep5 init  -> jog screens light
  vport: virtual ALSA MIDI "DDJ-FLX10 (relay)"  <- Mixxx connects here
  in   : ep2 BULK read   -> decode USB-MIDI -> vport out  (buttons/jogs -> Mixxx)
  out  : vport in (Mixxx) -> encode USB-MIDI -> ep3 BULK   (LEDs/SysEx -> device)
  screen: ep5 writes (later: deck info/waveform) + ep4 read + ep3 keepalive

Run:  sudo HID/.venv-relay/bin/python HID/flx10_relay_daemon.py
(Mixxx closed at start; start Mixxx AFTER screens light, select the relay port.)

v1 goal: screens in HID mode (logo/pages) + full MIDI control relay to Mixxx.
Screen CONTENT (deck text/waveform) needs the track-load capture — TODO.
"""

import os
import sys
import re
import pwd
import time
import glob
import json
import zlib
import errno
import queue
import struct
import sqlite3
import threading
import usb.core
import usb.util
import rtmidi

VID = 0x2B73
PID = 0x0041

EP2_IN  = 0x82   # BULK  — MIDI IN  (controller -> host)
EP3_OUT = 0x03   # BULK  — MIDI OUT (host -> controller: LEDs, SysEx, jog CCs)
EP5_OUT = 0x05   # INT   — vendor HID OUT (the 30 21 packet, waveforms)
EP4_IN  = 0x84   # INT   — vendor HID IN  (state stream)

VENDOR_CMDS = [
    (0x0100, 0xC028), (0x0000, 0xC029), (0x0200, 0xC013), (0x0000, 0xC02B),
    (0x0100, 0xC026), (0x0000, 0xC01D), (0x0100, 0xC027),
]
KEEPALIVE_FRAME = bytes.fromhex("04f000400405000004040100075000f7")

HERE = os.path.dirname(os.path.abspath(__file__))
INIT_JSON = os.path.join(HERE, "rb_init_sequence.json")
VPORT_NAME = "DDJ-FLX10"   # Mixxx sees "DDJ-FLX10" in + out (matches mapping name)


def log(m):
    print(f"[relay] {m}", flush=True)


# ===== kernel detach ========================================================

def find_sysfs_device(vid, pid):
    for vf in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            if int(open(vf).read().strip(), 16) != vid:
                continue
            if int(open(vf.replace("idVendor", "idProduct")).read().strip(), 16) == pid:
                return os.path.dirname(vf)
        except (OSError, ValueError):
            continue
    return None


def unbind_all(sysfs):
    for intf_dir in sorted(glob.glob(f"{sysfs}/*:*")):
        drv_link = os.path.join(intf_dir, "driver")
        if not os.path.islink(drv_link):
            continue
        drv = os.path.basename(os.readlink(drv_link))
        intf = os.path.basename(intf_dir)
        try:
            with open(f"/sys/bus/usb/drivers/{drv}/unbind", "w") as f:
                f.write(intf)
            log(f"  unbound {intf} <- {drv}")
        except OSError as e:
            log(f"  unbind {intf} ({drv}) FAILED: {e}")


# ===== USB-MIDI <-> raw MIDI =================================================

def usbmidi_decode(data):
    """ep2 USB-MIDI event packets -> list of complete raw MIDI messages."""
    msgs = []
    sysex = bytearray()
    for j in range(0, len(data), 4):
        ev = data[j:j + 4]
        if len(ev) < 4:
            break
        cin = ev[0] & 0x0f
        if cin == 0:
            continue
        if cin >= 0x8 and cin <= 0xE:            # channel voice
            hi = ev[1] & 0xF0
            if hi in (0xC0, 0xD0):
                msgs.append(bytes([ev[1], ev[2]]))
            else:
                msgs.append(bytes([ev[1], ev[2], ev[3]]))
        elif cin == 0x4:                          # sysex start/continue
            sysex += ev[1:4]
        elif cin == 0x5:                          # sysex end +1 (or 1-byte common)
            sysex += ev[1:2]
            msgs.append(bytes(sysex)); sysex = bytearray()
        elif cin == 0x6:                          # sysex end +2
            sysex += ev[1:3]
            msgs.append(bytes(sysex)); sysex = bytearray()
        elif cin == 0x7:                          # sysex end +3
            sysex += ev[1:4]
            msgs.append(bytes(sysex)); sysex = bytearray()
        elif cin == 0xF:                          # single byte
            msgs.append(ev[1:2])
    return msgs


def usbmidi_encode(msg):
    """One raw MIDI message -> USB-MIDI event packet bytes (cable 0)."""
    msg = bytes(msg)
    out = bytearray()
    if msg and msg[0] == 0xF0:                    # SysEx
        i = 0
        while len(msg) - i > 3:
            out += bytes([0x04, msg[i], msg[i + 1], msg[i + 2]]); i += 3
        rem = len(msg) - i
        if rem == 1:
            out += bytes([0x05, msg[i], 0, 0])
        elif rem == 2:
            out += bytes([0x06, msg[i], msg[i + 1], 0])
        elif rem == 3:
            out += bytes([0x07, msg[i], msg[i + 1], msg[i + 2]])
    elif msg:
        status = msg[0]
        cin = status >> 4
        b = list(msg) + [0, 0, 0]
        out += bytes([cin, b[0], b[1], b[2]])
    return bytes(out)


# ============================================================================
# ===== VEEZUHZ WAVEFORM ENGINE — Section 1: parse Mixxx waveform -> PWV5 =====
# ============================================================================
# Ported from flx10_screen_daemon.py by Veezuhz
#   (github.com/Veezuhz/Mixxx_FLX10_Controller_Mapping).
# Original code + comments preserved verbatim; only wiring is adapted for the
# Lou-MX single-owner libusb daemon (screen writes go via ep5 libusb, not
# hidraw — we own the whole device, so all of Veezuhz's hidraw/report-ID
# machinery is dropped).
#
# This section is standalone/offline-testable: it reads Mixxx's own analysis
# files + library DB and produces the LE16 PWV5 byte stream the FLX10 firmware
# wants. No hardware needed to test.
# ----------------------------------------------------------------------------

# When run under sudo, expanduser("~") returns /root — resolve the invoking
# user's home via $SUDO_USER so we find Mixxx's log/DB/analysis dir. (Veezuhz)
def _user_home():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return pwd.getpwnam(sudo_user).pw_dir
    return os.path.expanduser("~")

_HOME = _user_home()

def _mixxx_dir():
    """Flatpak Mixxx keeps data under ~/.var/app/.../.mixxx, not ~/.mixxx."""
    flatpak = os.path.join(_HOME, ".var/app/org.mixxx.Mixxx/.mixxx")
    native = os.path.join(_HOME, ".mixxx")
    if os.path.isfile(os.path.join(flatpak, "mixxxdb.sqlite")):
        return flatpak
    return native

_MIXXX_DIR     = _mixxx_dir()
MIXXX_LOG      = os.path.join(_MIXXX_DIR, "mixxx.log")
MIXXX_DB       = os.path.join(_MIXXX_DIR, "mixxxdb.sqlite")
MIXXX_ANALYSIS = os.path.join(_MIXXX_DIR, "analysis")

PWV5_FPS       = 150       # Pioneer PWV5 spec: 75 fps x 2 half-frames.
FW_WAVE_BUFFER = 24500     # firmware wave buffer in entries (empirical)

DECK_BYTES = {1: 0x10, 2: 0x20, 3: 0x30, 4: 0x40}


# --- Veezuhz: protobuf varint + Mixxx waveform parse ------------------------

def _varint(data, pos):
    result = 0; shift = 0; n = 0
    while True:
        b = data[pos + n]; n += 1
        result |= (b & 0x7f) << shift
        if not (b & 0x80):
            return result, n
        shift += 7


def _parse_mixxx_waveform(filepath):
    with open(filepath, "rb") as f:
        raw = f.read()
    uncompressed_size = int.from_bytes(raw[:4], "big")
    data = zlib.decompress(raw[4:])
    visual_sr = None
    signal_data = b""
    pos = 0
    while pos < len(data):
        tag = data[pos]; pos += 1
        field = tag >> 3; wire = tag & 7
        if field == 1 and wire == 1:
            visual_sr = struct.unpack("<d", data[pos:pos+8])[0]
            pos += 8
        elif field == 3 and wire == 2:
            length, n = _varint(data, pos); pos += n
            signal_data = data[pos:pos+length]
            pos += length
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
            elif wire == 1: pos += 8
            elif wire == 2:
                length, n = _varint(signal_data, pos); pos += n + length
            else: break
    return visual_sr or 441.0, values


def _convert_to_pwv5(visual_sr, values, target_fps=None):
    if target_fps is None:
        target_fps = PWV5_FPS
    """Downsample Mixxx waveform (visual_sr Hz) -> target_fps. Returns LE16
    PWV5 bytes (low band -> red, mid -> green, high -> blue, all -> height).

    Mixxx stores 4 values per visual frame (all/low/mid/high). The `visual_sr`
    field is DOUBLE the actual visual frame rate, so we divide by 2 when
    computing the downsample ratio — otherwise we produce half the entries the
    firmware needs and the waveform cuts off at ~half the track. (Veezuhz)"""
    n_in_frames = len(values) // 4
    if n_in_frames == 0:
        return bytearray()
    actual_visual_fps = visual_sr / 2.0
    in_per_out = actual_visual_fps / target_fps
    n_out_frames = int(n_in_frames / in_per_out)
    out = bytearray(2 * n_out_frames)
    for o in range(n_out_frames):
        start = int(o * in_per_out)
        end   = int((o + 1) * in_per_out)
        if end <= start: end = start + 1
        if end > n_in_frames: end = n_in_frames
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
        v = (r << 13) | (g << 10) | (b << 7) | (h << 2)
        out[2*o]     = v & 0xFF
        out[2*o + 1] = (v >> 8) & 0xFF
    return out


def waveform_for_track(track_id, duration_sec=0.0):
    """Load PWV5 waveform at canonical 150 fps (Serato/Pioneer spec). (Veezuhz)"""
    if not os.path.exists(MIXXX_DB):
        return None, "mixxxdb not found"
    conn = sqlite3.connect(MIXXX_DB)
    cur = conn.execute("SELECT id FROM track_analysis WHERE track_id = ? AND type = 1 LIMIT 1",
                       (track_id,))
    row = cur.fetchone(); conn.close()
    if not row:
        return None, f"no Waveform-5.0 analysis for track_id {track_id}"
    analysis_path = os.path.join(MIXXX_ANALYSIS, str(row[0]))
    if not os.path.exists(analysis_path):
        return None, f"analysis file missing: {analysis_path}"
    try:
        visual_sr, values = _parse_mixxx_waveform(analysis_path)
        pwv5 = _convert_to_pwv5(visual_sr, values, target_fps=150)
        n_entries = len(pwv5) // 2
        return pwv5, None
    except Exception as e:
        return None, f"parse failed: {e}"


def find_track_id(samples, file_bpm, duration, tol_samples=5000, tol_bpm=0.5, tol_dur=2.0):
    """Return track_locations.id (= analysis file id) whose
    (samplerate * duration * channels) ~= samples AND file_bpm ~= library bpm.
    Pitch-invariant: file_bpm and samples never change with the pitch fader. (Veezuhz)"""
    if not os.path.exists(MIXXX_DB):
        return None
    conn = sqlite3.connect(MIXXX_DB)
    if samples > 0 and file_bpm > 0:
        cur = conn.execute("""
            SELECT tl.id
            FROM library lib
            JOIN track_locations tl ON lib.location = tl.id
            WHERE ABS((lib.samplerate * lib.duration * lib.channels) - ?) <= ?
              AND ABS(lib.bpm - ?) <= ?
            ORDER BY ABS((lib.samplerate * lib.duration * lib.channels) - ?) ASC
            LIMIT 1
        """, (samples, tol_samples, file_bpm, tol_bpm, samples))
    elif samples > 0:
        cur = conn.execute("""
            SELECT tl.id FROM library lib
            JOIN track_locations tl ON lib.location = tl.id
            WHERE ABS((lib.samplerate * lib.duration * lib.channels) - ?) <= ?
            ORDER BY ABS((lib.samplerate * lib.duration * lib.channels) - ?) ASC
            LIMIT 1
        """, (samples, tol_samples, samples))
    else:
        cur = conn.execute("""
            SELECT tl.id FROM library lib
            JOIN track_locations tl ON lib.location = tl.id
            WHERE ABS(lib.duration - ?) <= ?
            ORDER BY ABS(lib.duration - ?) ASC
            LIMIT 1
        """, (duration, tol_dur, duration))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

# ===== end Veezuhz waveform engine Section 1 ================================


# ============================================================================
# ===== VEEZUHZ WAVEFORM ENGINE — Section 2: screen packet builders ==========
# ============================================================================
# Ported from flx10_screen_daemon.py by Veezuhz. Builder functions are verbatim
# (headers, byte offsets, encodings unchanged). ONE adaptation: Veezuhz's
# send_pkt() wrote to a hidraw fd (with a 0x00 report-ID prefix) or a pyusb
# endpoint. We OWN the device via libusb and write the raw 128-byte packet
# straight to ep5, so `send_pkt(send, pkt)` here just calls the Relay's ep5
# writer — no report-ID prefix, no hidraw. Everywhere Veezuhz passed `ep`,
# we now pass that send-callable.
# ----------------------------------------------------------------------------

POS_RATE       = 128.0     # xx27 [5,6,7] BE24 = pos * duration * POS_RATE (Veezuhz)
SERATO_DECK_31 = {0x10: 0x02, 0x20: 0x01, 0x30: 0x04, 0x40: 0x03}


def zeros():
    return bytearray(128)


def send_pkt(send, pkt):
    """Adaptation of Veezuhz's send_pkt for the single-owner libusb daemon.
    `send` is Relay._send_screen (writes 128 raw bytes to ep5). No report-ID
    prefix (that was a hidraw convention; libusb ep5 wants the raw packet)."""
    send(bytes(pkt))


# --- Veezuhz: xx 27 state ping (playhead / time / BPM) ----------------------

def build_xx27(deck_byte, loaded, bpm, pos=0.0, duration_sec=0.0):
    """xx 27 state ping. Position encoded as BE24 of [5,6,7] = pos x duration
    x POS_RATE. POS_RATE=128 gives in-sync scroll on the FLX10. (Veezuhz)"""
    p = zeros()
    p[0]  = deck_byte
    p[1]  = 0x27
    p[2]  = 0xb4
    p[3]  = 0x80
    p[4]  = 0x01
    p[20] = 0x0e
    p[25] = 0x80
    p[30] = 0x0d
    p[31] = SERATO_DECK_31[deck_byte]
    # During steady playback the "loaded" trailer is e0 01 00 (not ff ff ff).
    p[32] = 0xe0; p[33] = 0x01; p[34] = 0x00
    if loaded:
        # Track time "-MM:SS.x": [9]=min [10]=sec [11,12]=LE16 ms. Firmware
        # ALSO gates wave rendering on [9..12]; all-zero => deck goes inactive.
        if duration_sec > 0:
            p_clamped = pos
            if p_clamped < 0.0: p_clamped = 0.0
            if p_clamped > 1.0: p_clamped = 1.0
            remaining = duration_sec * (1.0 - p_clamped)
            total_ms  = int(round(remaining * 1000))
            minutes   = total_ms // 60000
            rem_ms    = total_ms %  60000
            seconds   = rem_ms   // 1000
            ms        = rem_ms   %  1000
            p[9]  = minutes & 0xFF
            p[10] = seconds & 0xFF
            p[11] =  ms        & 0xFF
            p[12] = (ms >> 8)  & 0xFF
        else:
            p[9]  = 0x06; p[10] = 0x1b; p[11] = 0xfa; p[12] = 0x01
        p[29] = 0x92
        if bpm > 0:
            p[13] = int(bpm) & 0xFF
            p[14] = (int(round((bpm - int(bpm)) * 10)) & 0x0F) << 4
        # Playhead position: BE24 of [5,6,7] = pos x duration_sec x POS_RATE
        if duration_sec > 0:
            value = int(pos * duration_sec * POS_RATE)
            if value < 0: value = 0
            if value > 0xFFFFFF: value = 0xFFFFFF
            p[5] = (value >> 16) & 0xFF
            p[6] = (value >> 8)  & 0xFF
            p[7] =  value        & 0xFF
    else:
        p[29] = 0x80
    return p


# --- Veezuhz: track-load init packets ---------------------------------------

def send_xx30(ep, deck, duration_sec=0.0):
    """xx 30 init/track-load. Carries TRACK LENGTH at [6..9] (min/sec/ms-LE16).
    All 0x00 after byte 9 (Serato's xx30 is clean). (Veezuhz)"""
    p = zeros()
    p[0] = DECK_BYTES[deck]
    p[1] = 0x30
    p[2] = 0x01
    p[4] = 0x01
    if duration_sec > 0:
        total_ms = int(round(duration_sec * 1000))
        minutes = total_ms // 60000
        rem_ms  = total_ms %  60000
        seconds = rem_ms   // 1000
        ms      = rem_ms   %  1000
        p[6] = minutes & 0xFF
        p[7] = seconds & 0xFF
        p[8] = ms & 0xFF
        p[9] = (ms >> 8) & 0xFF
    send_pkt(ep, p)


def send_xx35(ep, deck, n_entries=0):
    """xx 35 carries TOTAL WAVEFORM ENTRY COUNT at [2][3] as LE16. Firmware
    uses it to scale the wave needle; sending 0 misaligns the wave. (Veezuhz)"""
    db = DECK_BYTES[deck]
    p = zeros(); p[0] = db; p[1] = 0x35
    p[2] = n_entries & 0xFF
    p[3] = (n_entries >> 8) & 0xFF
    send_pkt(ep, p)
    for _ in range(2):
        p = zeros(); p[0] = db; p[1] = 0x35
        p[2] = n_entries & 0xFF
        p[3] = (n_entries >> 8) & 0xFF
        send_pkt(ep, p)


def send_xx3d_display_mode(ep, mode):
    """Set the FLX10 jog display mode (1..5): `00 3d MODE 00 05 00 ...` on ep5.
    Modes: wave+wave2 -> wave-only -> deck-wave-beatgrid -> logo -> art. (Veezuhz)"""
    p = zeros()
    p[0] = 0x00       # global (not per-deck)
    p[1] = 0x3d
    p[2] = mode & 0x0F
    p[4] = 0x05       # constant from capture
    send_pkt(ep, p)


# --- Veezuhz: xx 36 waveform data (upload + single-packet scroll) -----------

def _xx36_packet(deck_byte, pos_entries, pwv5_bytes, take=19):
    """Build one xx 36 packet. pos_entries is the LE32 counter that tells the
    firmware where these entries belong — and is ALSO how it tracks the current
    playhead (the most-recent xx36 counter is the displayed center). (Veezuhz)"""
    p = zeros()
    p[0]  = deck_byte
    p[1]  = 0x36
    p[2]  = 0x00
    p[4]  = 0x00
    p[6]  = 0x13
    p[10] =  pos_entries        & 0xFF
    p[11] = (pos_entries >> 8)  & 0xFF
    p[12] = (pos_entries >> 16) & 0xFF
    p[13] = (pos_entries >> 24) & 0xFF
    src_off = pos_entries * 2
    src_end = min(src_off + take * 2, len(pwv5_bytes))
    n = src_end - src_off
    if n > 0:
        p[14:14 + n] = pwv5_bytes[src_off:src_end]
    return p


def upload_xx36_waveform(ep, deck, pwv5_bytes):
    """Upload the entire (already-fit-to-buffer) waveform at track-load. (Veezuhz)"""
    db = DECK_BYTES[deck]
    n_entries = len(pwv5_bytes) // 2
    ENTRIES_PER_PKT = 19
    pos = 0
    while pos < n_entries:
        take = min(ENTRIES_PER_PKT, n_entries - pos)
        p = _xx36_packet(db, pos, pwv5_bytes, take)
        send_pkt(ep, p)
        pos += take
    print(f"  [waveform] uploaded {n_entries} entries at track-load")


def send_scroll_update(ep, deck, pos_entries, pwv5_bytes):
    """Send one xx 36 at pos_entries — updates the firmware's displayed
    playhead. Matches Serato re-sending small xx 36 during playback. (Veezuhz)"""
    if not pwv5_bytes or pos_entries < 0:
        return
    p = _xx36_packet(DECK_BYTES[deck], pos_entries, pwv5_bytes)
    send_pkt(ep, p)

# ===== end Veezuhz waveform engine Section 2 ================================


# ============================================================================
# ===== VEEZUHZ WAVEFORM ENGINE — Section 3: deck state + driver threads ======
# ============================================================================
# Ported from flx10_screen_daemon.py by Veezuhz. DeckState / interp_pos /
# StatePingThread / RefreshThread / handle_track_load kept faithful; the
# threads now take a `send` callable (Relay._send_screen -> ep5 libusb) instead
# of a hidraw fd. xx27 IS re-enabled here (Veezuhz disabled it because Mixxx's
# screen.js drove it — we have no screen.js, so the daemon owns xx27 too).
# ----------------------------------------------------------------------------

class DeckState:
    def __init__(self):
        self.bpm         = 0.0
        self.loaded      = False
        self.track_id    = None
        self.duration    = 0.0     # seconds (for position encoding)
        self.pos         = 0.0     # 0..1 last reported
        self.last_pos_ts = 0.0
        self.last_pos_val = 0.0
        self.prev_pos_val = 0.0
        self.prev_pos_ts  = 0.0
        self.pwv5        = b""
        self.uploading   = False   # gate RefreshThread during bulk fill

DECKS = {1: DeckState(), 2: DeckState(), 3: DeckState(), 4: DeckState()}


def interp_pos(st, now=None):
    """Interpolated playhead between Mixxx's ~100ms position updates. Cautious
    extrapolation with guards so a burst of updates can't overshoot to 0/1.
    (Veezuhz — verbatim logic.)"""
    if now is None:
        now = time.time()
    dt = now - st.last_pos_ts
    if dt < 0.0 or dt > 0.2:
        return st.last_pos_val
    if st.prev_pos_ts <= 0:
        return st.last_pos_val
    dt_log = st.last_pos_ts - st.prev_pos_ts
    if dt_log < 0.010 or dt_log > 0.500:
        return st.last_pos_val
    rate = (st.last_pos_val - st.prev_pos_val) / dt_log
    if abs(rate) > 0.5:
        return st.last_pos_val
    step_dt = dt if dt < 0.030 else 0.030
    est = st.last_pos_val + rate * step_dt
    if est < 0.0: est = 0.0
    if est > 1.0: est = 1.0
    return est


class StatePingThread(threading.Thread):
    """xx 27 state ping — playhead / time / BPM. 200 Hz for smooth scroll.
    Only LOADED decks get pings (empty-deck xx27 resets firmware display
    state). (Veezuhz — send is now Relay._send_screen.)"""
    def __init__(self, send, interval_s=0.005):
        super().__init__(daemon=True)
        self.send = send
        self.interval = interval_s
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            for d in (1, 2, 3, 4):
                st = DECKS[d]
                if not st.loaded:
                    continue
                send_pkt(self.send,
                         build_xx27(DECK_BYTES[d], st.loaded, st.bpm,
                                    interp_pos(st), duration_sec=st.duration))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


class RefreshThread(threading.Thread):
    """xx 36 trickle at the current playhead, deduped (skip when the entry
    hasn't advanced). Keeps the firmware wave buffer alive + tracks the
    playhead. 8 Hz matches Serato's measured cadence. (Veezuhz)"""
    def __init__(self, send, interval_s=0.125):
        super().__init__(daemon=True)
        self.send = send
        self.interval = interval_s
        self._last_entry = {1: -1, 2: -1, 3: -1, 4: -1}
        self._stop = threading.Event()

    def run(self):
        ENTRIES_PER_PKT = 19
        while not self._stop.is_set():
            for d in (1, 2, 3, 4):
                st = DECKS[d]
                if not (st.loaded and st.pwv5 and st.duration > 0):
                    continue
                if st.uploading:
                    continue
                n_entries = len(st.pwv5) // 2
                entry = int(interp_pos(st) * n_entries)
                if entry < 0:
                    entry = 0
                if entry > n_entries - ENTRIES_PER_PKT:
                    entry = max(0, n_entries - ENTRIES_PER_PKT)
                if entry == self._last_entry[d]:
                    continue
                self._last_entry[d] = entry
                send_pkt(self.send, _xx36_packet(DECK_BYTES[d], entry, st.pwv5))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


def handle_track_load(send, deck, pwv5, label="", duration_sec=0.0, file_bpm=0.0):
    """Veezuhz upload sequence, trimmed to the ported builders: xx30 (length)
    + xx35 (entry count) + full xx36 waveform, then park the playhead at the
    real position. (xx39 hotcue / xx33 art / xx2f beatgrid deferred.)"""
    if not pwv5:
        print(f"  deck {deck}: empty waveform, skipping upload")
        return
    st = DECKS[deck]
    st.pwv5 = bytes(pwv5)
    n_entries = len(st.pwv5) // 2
    print(f"  deck {deck}: uploading {n_entries} entries {label} dur={duration_sec:.1f}s")
    st.uploading = True     # gate RefreshThread during bulk fill (flash fix)
    try:
        send_xx30(send, deck, duration_sec=duration_sec)
        send_xx35(send, deck, n_entries=int(round(duration_sec * 150)))
        upload_xx36_waveform(send, deck, st.pwv5)
        # Bulk fill leaves the firmware counter at buffer END; re-park at real playhead.
        park = int(interp_pos(st) * n_entries)
        if park < 0: park = 0
        if park > n_entries - 19: park = max(0, n_entries - 19)
        send_scroll_update(send, deck, park, st.pwv5)
    finally:
        st.uploading = False
    send_xx3d_display_mode(send, 1)   # jog page = waveform


def _unpack7(bs):
    """Reassemble a big-endian 7-bit-packed integer (matches JS _pack7)."""
    v = 0
    for b in bs:
        v = (v << 7) | (b & 0x7F)
    return v

# ===== end Veezuhz waveform engine Section 3 ================================


# ===== USB init =============================================================

def mode_switch(dev):
    log("ep0: 7 vendor mode-switch transfers")
    for i, (wv, wi) in enumerate(VENDOR_CMDS, 1):
        try:
            dev.ctrl_transfer(0x40, 3, wv, wi, None)
        except usb.core.USBError as e:
            log(f"  [{i}/7] FAIL: {e}")
        time.sleep(0.005)


def replay_init(dev, seq, delay, wlock):
    log(f"ep3/ep5: replay {len(seq)} init frames")
    for ep, hexb in seq:
        data = bytes.fromhex(hexb)
        with wlock:
            try:
                dev.write(EP3_OUT if ep == "ep3" else EP5_OUT, data, timeout=500)
            except usb.core.USBError as e:
                log(f"  {ep} FAIL: {e}")
        if delay:
            time.sleep(delay)
    log("  init replayed")


# ===== relay =================================================================

class Relay:
    def __init__(self, dev):
        self.dev = dev
        self.stop = threading.Event()
        self.wlock = threading.Lock()          # serialize EP3/EP5 writes
        self.midi_out = rtmidi.MidiOut(name=VPORT_NAME)   # daemon writes -> Mixxx input
        self.midi_in = rtmidi.MidiIn(name=VPORT_NAME)      # Mixxx writes -> daemon
        self.midi_in.ignore_types(sysex=False, timing=False, active_sense=False)
        self._out_count = 0
        self._ipc_count = 0
        self.load_q = queue.Queue()            # track-load work (heavy upload off the MIDI thread)

    def open_vport(self):
        self.midi_out.open_virtual_port(VPORT_NAME)
        self.midi_in.open_virtual_port(VPORT_NAME)
        self.midi_in.set_callback(self.on_mixxx_midi)
        log(f"virtual MIDI port '{VPORT_NAME}' open (in+out) — select it in Mixxx")

    # ep5 screen write (used by the Section 2/3 builders via send_pkt)
    def _send_screen(self, pkt):
        with self.wlock:
            try:
                self.dev.write(EP5_OUT, bytes(pkt), timeout=500)
            except usb.core.USBError:
                pass

    # ---- private IPC from the JS: F0 7D <type> <deck> <payload> F7 ----------
    # Consumed here (NOT forwarded to the controller). Fast fields update inline;
    # the heavy track-load waveform upload is queued to the loader thread.
    def _handle_ipc(self, msg):
        if len(msg) < 5 or msg[-1] != 0xF7:
            return
        typ = msg[2]; deck = msg[3]
        st = DECKS.get(deck)
        if st is None:
            return
        body = msg[4:-1]
        if typ == 0x01 and len(body) >= 10:            # track load
            samples = _unpack7(body[0:4])
            file_bpm = _unpack7(body[4:7]) / 100.0
            duration = _unpack7(body[7:10]) / 1000.0
            self._ipc_count += 1
            if self._ipc_count <= 8:
                log(f"  IPC track-load deck={deck} samples={samples} "
                    f"bpm={file_bpm} dur={duration:.1f}s")
            self.load_q.put((deck, samples, file_bpm, duration))
        elif typ == 0x02 and len(body) >= 3:           # position
            pos = _unpack7(body[0:3]) / 1000000.0
            st.prev_pos_val = st.last_pos_val
            st.prev_pos_ts  = st.last_pos_ts
            st.last_pos_val = pos
            st.last_pos_ts  = time.time()
            st.pos = pos
        elif typ == 0x03 and len(body) >= 3:           # bpm
            st.bpm = _unpack7(body[0:3]) / 100.0

    def loader_loop(self):
        """Consumes track-load requests: library lookup -> parse waveform ->
        upload. Runs off the MIDI callback thread so the ~1900-packet xx36
        bulk fill never stalls MIDI."""
        while not self.stop.is_set():
            try:
                deck, samples, file_bpm, duration = self.load_q.get(timeout=0.3)
            except queue.Empty:
                continue
            track_id = find_track_id(samples, file_bpm, duration)
            st = DECKS[deck]
            if track_id is None:
                log(f"  [deck {deck}] no library match "
                    f"(samples={samples} bpm={file_bpm} dur={duration:.1f})")
                continue
            if st.track_id == track_id and st.pwv5:
                continue                                # already loaded
            st.track_id = track_id
            st.bpm = file_bpm
            st.duration = duration
            st.loaded = True
            pwv5, err = waveform_for_track(track_id, duration_sec=duration)
            if err:
                log(f"  [deck {deck}] waveform: {err}")
                continue
            handle_track_load(self._send_screen, deck, pwv5,
                              label=f"(track_id={track_id})",
                              duration_sec=duration, file_bpm=file_bpm)

    # Mixxx -> controller (ep3), OR private IPC we consume
    def on_mixxx_midi(self, event, data=None):
        msg, _ = event
        # Intercept private IPC (F0 7D ...) — do NOT forward to the controller.
        if len(msg) >= 3 and msg[0] == 0xF0 and msg[1] == 0x7D:
            self._handle_ipc(msg)
            return
        self._out_count += 1
        if self._out_count <= 20:
            log(f"  Mixxx->ep3 #{self._out_count}: {bytes(msg).hex()}")
        pkt = usbmidi_encode(msg)
        if not pkt:
            return
        with self.wlock:
            try:
                self.dev.write(EP3_OUT, pkt, timeout=500)
            except usb.core.USBError as e:
                log(f"  ep3 write err: {e}")

    # controller -> Mixxx (ep2)
    def ep2_loop(self):
        while not self.stop.is_set():
            try:
                data = self.dev.read(EP2_IN, 64, timeout=200)
            except usb.core.USBError as e:
                if e.errno == errno.ETIMEDOUT:
                    continue
                time.sleep(0.01); continue
            for m in usbmidi_decode(bytes(data)):
                try:
                    self.midi_out.send_message(list(m))
                except Exception:
                    pass

    def ep4_loop(self):
        while not self.stop.is_set():
            try:
                self.dev.read(EP4_IN, 64, timeout=200)
            except usb.core.USBError:
                pass

    def keepalive_loop(self):
        while not self.stop.is_set():
            with self.wlock:
                try:
                    self.dev.write(EP3_OUT, KEEPALIVE_FRAME, timeout=500)
                except usb.core.USBError:
                    pass
            self.stop.wait(0.2)

    def close(self):
        self.stop.set()
        try:
            self.midi_in.close_port()
            self.midi_out.close_port()
        except Exception:
            pass


def main():
    if os.geteuid() != 0:
        sys.exit("Need root: sudo HID/.venv-relay/bin/python HID/flx10_relay_daemon.py")
    if not os.path.exists(INIT_JSON):
        sys.exit(f"Missing {INIT_JSON}")
    seq = json.load(open(INIT_JSON))

    sysfs = find_sysfs_device(VID, PID)
    if not sysfs:
        sys.exit("FLX10 not in sysfs.")
    log(f"sysfs {sysfs}; detaching kernel drivers")
    unbind_all(sysfs)
    time.sleep(0.3)

    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("pyusb can't see device")
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        log(f"set_configuration: {e}")

    relay = Relay(dev)

    mode_switch(dev)
    time.sleep(0.2)
    replay_init(dev, seq, 0.002, relay.wlock)

    relay.open_vport()
    threads = [
        threading.Thread(target=relay.ep2_loop, daemon=True),
        threading.Thread(target=relay.ep4_loop, daemon=True),
        threading.Thread(target=relay.keepalive_loop, daemon=True),
        threading.Thread(target=relay.loader_loop, daemon=True),
    ]
    for t in threads:
        t.start()

    # Screen driver threads (Section 3): xx27 state ping + xx36 wave trickle.
    ping = StatePingThread(relay._send_screen)
    refresh = RefreshThread(relay._send_screen)
    ping.start()
    refresh.start()

    log("=== RELAY LIVE ===")
    log(">>> screens should be lit. Start Mixxx, pick controller 'DDJ-FLX10' (relay).")
    log(">>> buttons/jogs -> Mixxx via virtual port; Mixxx LEDs/SysEx -> controller.")
    log(">>> load a track: JS sends F0 7D IPC -> daemon uploads waveform to jog screens.")
    log("Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("stopping")
        ping.stop()
        refresh.stop()
        relay.close()


if __name__ == "__main__":
    main()
