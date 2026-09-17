#!/usr/bin/env python3
"""
flx10_screen_daemon.py — long-running daemon that drives the FLX10 jog wheel
screens with real waveforms from whatever Mixxx is currently playing.

====================================================================
CHANGE LOG
====================================================================
2026-05-26 (late-night session):
  * Hardened interp_pos() against rate spikes. Earlier versions caused
    "wave content flashes to track-start" because a burst of FLX10_POS
    log lines arriving close together produced huge extrapolation rates,
    overshooting to 0.0 or 1.0 for several xx 36 packets in a row.
    Guards now: dt_log must be in [10ms, 500ms]; |rate| must be ≤ 0.5/s;
    extrapolation capped at a 30ms step. Holds last_pos_val otherwise.
  * RefreshThread (xx 36 trickle): switched from raw st.pos to interp_pos
    so wave-entry advances smoothly between Mixxx's ~100ms position ticks
    instead of in 100ms stair-steps.
  * Removed all diagnostic logs ([FLASH?], [XX36] verbose entry log,
    SEEK-detected log, on_track_load echo) after the wave-flash and
    position-anomaly debugging was complete.

2026-05-25 and earlier:
  * xx 35 sends true PWV5 entry count (was 0).
  * xx 30 includes wall-clock duration at bytes [6..9].
  * xx 2f beat-grid encoding (4-byte records at 22050 Hz sample positions).
  * RefreshThread interval = 0.127s (8 Hz), matches Serato's measured cadence.
  * Adaptive PWV5 frame rate to fit firmware's ~24500-entry buffer.
====================================================================


Architecture:
  Mixxx (MIDI mapping) ──► mixxx.log (FLX10_TRACK_LOAD lines)
                              │
                              ▼
                  this daemon (tails log)
                              │
                              ▼ HID interface 5
                          FLX10 jog wheel screen

This daemon REPLACES Mixxx's HID screen controller (PioneerDDJFLX10-screen.js)
which must be DISABLED in Mixxx Preferences. Mixxx's MIDI mapping stays enabled
because it does:
  - All the buttons / LEDs / ch16 jog display feedback
  - The SysEx handshake (03 01 + 50 31 keepalive) the HID screen needs
  - The FLX10_TRACK_LOAD log lines this daemon tails

Prerequisites:
  - Vendor unlock has been run once: sudo python3 flx10_unlock_v2.py
    (or pass --unlock to have this daemon do it on startup)
  - Pre-rendered waveform cache exists for the tracks you'll load:
    python3 flx10_prerender_waveform.py --all
  - Mixxx's "DDJ-FLX10 Screen" HID controller is DISABLED in Preferences

Usage:
  sudo python3 flx10_screen_daemon.py                      # default: tail ~/.mixxx/mixxx.log
  sudo python3 flx10_screen_daemon.py --unlock             # also run vendor unlock at start
  sudo python3 flx10_screen_daemon.py --log /custom/path   # tail a different log file
"""

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("pyusb not found.  pip install pyusb")

# Force line-buffered stdout so prints show up immediately when piped through
# `tee` or to a file. Without this, output is block-buffered and the log file
# stays empty for a long time even though things are happening.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except AttributeError:
    pass  # Python <3.7 fallback — user will need to invoke with `python3 -u`

# ===== Constants ============================================================

VID                = 0x2B73
PID                = 0x0041
SCREEN_INTERFACE   = 5
DECK_BYTES         = {1: 0x10, 2: 0x20, 3: 0x30, 4: 0x40}
SERATO_DECK_31     = {0x10: 0x02, 0x20: 0x01, 0x30: 0x04, 0x40: 0x03}

VENDOR_UNLOCK_CMDS = [
    (0x0100, 0xC028), (0x0000, 0xC029), (0x0200, 0xC013), (0x0000, 0xC02B),
    (0x0100, 0xC026), (0x0000, 0xC01D), (0x0100, 0xC027),
]

# IMPORTANT: when run under sudo, os.path.expanduser("~") returns /root —
# which is NEVER what we want. Resolve to the invoking user's home via
# $SUDO_USER so the daemon finds Mixxx's log, DB, and cache directory.
def _user_home():
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        return pwd.getpwnam(sudo_user).pw_dir
    return os.path.expanduser("~")

_HOME       = _user_home()
MIXXX_LOG        = os.path.join(_HOME, ".mixxx", "mixxx.log")
MIXXX_DB         = os.path.join(_HOME, ".mixxx", "mixxxdb.sqlite")
MIXXX_ANALYSIS   = os.path.join(_HOME, ".mixxx", "analysis")
SEG_PAYLOAD      = 122
PWV5_FPS_NOMINAL = 150     # Pioneer PWV5 spec: 75 frames/sec × 2 half-frames.
                           # We compute an effective FPS per-track so the
                           # whole waveform fits in the firmware's ~24,500-
                           # entry buffer (otherwise wave runs out at ~163 sec
                           # and needle position misaligns with Mixxx).
PWV5_FPS         = 150     # back-compat (read by tail_mixxx_log probe)
FW_WAVE_BUFFER   = 24500   # firmware wave buffer in entries (empirical
                           # 2026-05-25: at 150 fps user saw wave end at
                           # 162.3 sec → 24345 entries; rounded to 24500)

# Hardcoded xx 39 packets from the Serato capture, byte-for-byte.
_XX39_HEX = [
    "10390100030000484f54204355450000000000000000000000000000000000000000000000003f000000000000000000000000000000000000000000000000000000000000023f000000000000000000000000000000000000000000000000000000000000023f00000000000000000000000000000000000000000000000000",
    "1039020003000000000000023f000000000000000000000000000000000000000000000000000000000000023f000000000000000000000000000000000000000000000000000000000000023f000000000000000000000000000000000000000000000000000000000000023f00000000000000000000000000000000000000",
    "1039030003000000000000000000000000023f00000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
]

# Placeholder JPEG built on demand by PIL (see get_test_jpeg). The hardcoded
# fallback is a minimal 1×1 JPEG sufficient to satisfy xx 33's "have an album
# art" requirement even if PIL isn't installed.
_MINIMAL_JPEG_HEX = (
    "ffd8ffe000104a46494600010101006000600000ffdb0043000806060706050807070708090908"
    "0a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434"
    "341f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c213232323232323232"
    "32323232323232323232323232323232323232323232323232323232323232323232323232323232"
    "32323232323232323232323232ffc00011080001000103012200021101031101ffc4001f0000010501010101010100"
    "000000000000000102030405060708090a0bffc400b510000201030302040305"
    "0504040000017d01020300041105122131410613516107227114328191a10823"
    "42b1c11552d1f02433627282090a161718191a25262728292a3435363738393a"
    "434445464748494a535455565758595a636465666768696a737475767778797a"
    "838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7"
    "b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1"
    "f2f3f4f5f6f7f8f9faffc4001f01000301010101010101010100000000000001"
    "02030405060708090a0bffc400b5110002010204040304070504040001027700"
    "0102031104052131061241510761711322328108144291a1b1c109233352f015"
    "6272d10a162434e125f11718191a262728292a35363738393a43444546474849"
    "4a535455565758595a636465666768696a737475767778797a82838485868788"
    "898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4"
    "c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9"
    "faffda000c03010002110311003f00bf3effd9")


# ===== USB plumbing =========================================================

def find_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit(f"FLX10 not found (VID 0x{VID:04X} PID 0x{PID:04X})")
    return dev


def vendor_unlock(dev):
    print("Vendor unlock (7 commands) …")
    for i, (wv, wi) in enumerate(VENDOR_UNLOCK_CMDS, 1):
        try:
            dev.ctrl_transfer(0x40, 3, wv, wi, None, timeout=200)
        except usb.core.USBError as e:
            print(f"  [{i}/7] FAIL: {e}")
        time.sleep(0.005)
    time.sleep(0.2)


def get_screen_ep_out(dev):
    cfg  = dev.get_active_configuration()
    intf = cfg[(SCREEN_INTERFACE, 0)]
    ep   = usb.util.find_descriptor(
        intf,
        custom_match=lambda e: (
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR))
    if ep is None:
        sys.exit(f"Could not find EP5 OUT on interface {SCREEN_INTERFACE}")
    return ep


# Serialize all HID writes (state-ping thread + track-load handler share the
# endpoint).
_send_lock = threading.Lock()

def send_pkt(ep, pkt):
    """Send a 128-byte HID OUT report.

    `ep` is EITHER a pyusb endpoint object (legacy libusb path) OR an int
    file descriptor (new hidraw path). We dispatch by type so both work.

    hidraw write convention: first byte is the HID report ID. FLX10 doesn't
    use report IDs, so we prepend 0x00 to make a 129-byte write."""
    with _send_lock:
        try:
            if isinstance(ep, int):
                # hidraw fd path
                os.write(ep, bytes([0]) + bytes(pkt))
            else:
                # legacy libusb endpoint path
                ep.write(bytes(pkt), timeout=1000)
        except (OSError, usb.core.USBError) as e:
            print(f"  send error: {e}")


def find_flx10_hidraw():
    """Locate /dev/hidrawN that maps to FLX10's HID interface 5."""
    sysdir = "/sys/class/hidraw"
    if not os.path.isdir(sysdir):
        return None
    for name in os.listdir(sysdir):
        uevent_path = os.path.join(sysdir, name, "device", "uevent")
        try:
            with open(uevent_path) as f:
                content = f.read()
        except OSError:
            continue
        # HID_ID format: BUSTYPE:VENDOR:PRODUCT (e.g. 0003:00002B73:00000041)
        if f"{VID:04X}" in content.upper() and f"{PID:04X}" in content.upper():
            return f"/dev/{name}"
    return None


def open_hidraw():
    """Open FLX10's hidraw device for non-exclusive writes."""
    path = find_flx10_hidraw()
    if path is None:
        sys.exit(f"FLX10 hidraw device not found (VID 0x{VID:04X} PID 0x{PID:04X}).\n"
                 f"  Check `ls /sys/class/hidraw/` for the FLX10 entry.")
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError as e:
        sys.exit(f"Failed to open {path}: {e}\n"
                 f"  Try `sudo chmod a+w {path}` or run the daemon as root.")
    print(f"Opened {path} for HID writes (non-exclusive — Mixxx HID screen.js can coexist)")
    return fd


def zeros():
    return bytearray(128)


# ===== Per-deck state =======================================================

class DeckState:
    def __init__(self):
        self.bpm        = 0.0     # file_bpm (pitch-invariant)
        self.loaded     = False
        self.track_id   = None
        self.duration   = 0.0     # track length in seconds (for position calc)
        self.pos        = 0.0     # 0..1 play position from Mixxx (last reported)
        self.last_pos_ts = 0.0    # wall-clock of last FLX10_POS
        self.last_pos_val = 0.0   # pos value at last_pos_ts (for interpolation)
        self.prev_pos_val = 0.0   # pos from update BEFORE last (to detect play rate)
        self.prev_pos_ts  = 0.0
        self.last_seek_seq = 0    # bumped each time a SEEK is detected (RefreshThread reads)
        self.pwv5       = b""
        # 2026-05-29 v6 — set True while upload_xx36_waveform() is bulk-filling
        # the firmware buffer. The RefreshThread MUST skip the deck while this
        # is set: otherwise the 8 Hz trickle (at the real playhead, e.g. entry
        # ~449 near track-start) interleaves with the bulk sweep (entry 0..N),
        # and since the firmware treats the most-recent xx36 counter as the
        # displayed playhead, the display rapidly snaps start↔sweep — the
        # "wave flashes one frame to the very start of the track, ~once per
        # second" the user reported. Verified from the user's own usbmon
        # capture: a ~700 Hz xx36 burst (16454→31635) interleaved with trickle
        # packets at entry 449. See git history / FLX10-SCREEN-PROTOCOL notes.
        self.uploading  = False

DECKS = {1: DeckState(), 2: DeckState(), 3: DeckState(), 4: DeckState()}


def interp_pos(st, now=None):
    """Return an interpolated playhead pos for `st` based on time since the last
    Mixxx update. Mixxx logs FLX10_POS every 100 ms; without interpolation the
    state-ping at 200 Hz would emit 20 ticks of the same value before jumping,
    causing the waveform/playhead to look stepped. We linearly extrapolate from
    the last two pos updates so the displayed position advances smoothly between
    Mixxx ticks.

    CRITICAL: extrapolation must be CAUTIOUS. Earlier versions caused the
    "wave content flashes to track-start (and to track-end)" artifact by
    overshooting:
      - Two FLX10_POS log lines flushed close together produce a tiny dt_log
        → `rate` blows up → est saturates to 0.0 or 1.0 for the next ~200ms,
        which makes xx 36 stream entry=0 (track-start) or entry=N-19
        (track-end) for a burst of packets.
      - Seeks (clicks in the wave) produce a brief huge `rate`.
    Guards added 2026-05-25:
      - dt_log must be in [10ms, 500ms] for extrapolation.
      - |rate| must be ≤ 0.5/sec (well above 8× playback on a 30s track).
      - Total extrapolation step is capped at 30ms-worth so even a marginal
        rate can never overshoot by much."""
    if now is None:
        now = time.time()
    dt = now - st.last_pos_ts
    if dt < 0.0 or dt > 0.2:
        return st.last_pos_val
    if st.prev_pos_ts <= 0:
        return st.last_pos_val
    dt_log = st.last_pos_ts - st.prev_pos_ts
    if dt_log < 0.010 or dt_log > 0.500:
        # Log lines arrived too close (likely a burst flush of a seek) or too
        # far apart — extrapolation would be unreliable. Hold last value.
        return st.last_pos_val
    rate = (st.last_pos_val - st.prev_pos_val) / dt_log
    if abs(rate) > 0.5:
        # Rate implies >0.5 of the track per second — that's a seek, not play.
        return st.last_pos_val
    # Cap extrapolation step so we never wander more than ~30ms ahead even if
    # rate*dt is borderline (defense in depth — clamps to [0,1] still apply).
    step_dt = dt if dt < 0.030 else 0.030
    est = st.last_pos_val + rate * step_dt
    if est < 0.0: est = 0.0
    if est > 1.0: est = 1.0
    return est


# ===== xx 27 (50 Hz state ping) ============================================

POS_RATE = 128.0  # overridden by --pos-rate flag at startup


def build_xx27(deck_byte, loaded, bpm, pos=0.0, duration_sec=0.0):
    """xx 27 state ping. Position encoded as BE24 of [5,6,7] = pos × duration × POS_RATE.
    Empirically POS_RATE=128 gives in-sync scroll on the FLX10. (The smooth-playback
    capture showed ~252/sec for the raw byte changes; the firmware appears to
    interpret the displayed-position value at half that rate. Likely encoding is
    1 unit per 1/128 sec of audio.)"""
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
    # Verified from smooth-playing capture: during steady playback the
    # "loaded" trailer is e0 01 00 NOT ff ff ff. (ff ff ff was from earlier
    # paused-state captures.)
    p[32] = 0xe0; p[33] = 0x01; p[34] = 0x00
    if loaded:
        # Track duration display "-MM:SS.x" (remaining time):
        #   [9]   = minutes  (plain decimal byte, NOT BCD)
        #   [10]  = seconds  (plain decimal byte, 0..59)
        #   [11,12] LE16 = milliseconds within the second (0..999)
        # CRITICAL: firmware also uses these bytes to gate wave rendering;
        # if [9..12] are all zero, the entire deck display goes inactive
        # (confirmed 2026-05-23 by experiment). MIDI ch16 does NOT take
        # over the time display, so we must always feed valid bytes here.
        if duration_sec > 0:
            # Compute REMAINING from current pos (counts down with playback).
            # Encoding: [9]=minutes, [10]=seconds, [11,12] LE16=ms.
            # Firmware also uses these bytes to gate wave rendering, so we
            # MUST send valid values continuously (MIDI ch16 does not
            # fall back — verified 2026-05-23 by setting bytes to 0; wave
            # disappeared).
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

        # Playhead position: BE24 of [5,6,7] = pos × duration_sec × POS_RATE
        if duration_sec > 0:
            value = int(pos * duration_sec * POS_RATE)
            if value < 0: value = 0
            if value > 0xFFFFFF: value = 0xFFFFFF
            p[5] = (value >> 16) & 0xFF
            p[6] = (value >> 8)  & 0xFF
            p[7] =  value        & 0xFF
        # byte [8] left as 0 — it's a 2-bit animation counter, unrelated.
    else:
        p[29] = 0x80
    return p


class StatePingThread(threading.Thread):
    """xx 27 state ping. Tight loop so the playhead moves smoothly — 200 Hz
    matches the position encoding's resolution (256 ticks/sec)."""
    def __init__(self, ep_out, interval_s=0.005):
        super().__init__(daemon=True)
        self.ep_out   = ep_out
        self.interval = interval_s
        self._stop    = threading.Event()

    def run(self):
        while not self._stop.is_set():
            now = time.time()
            for d in (1, 2, 3, 4):
                st = DECKS[d]
                # CRITICAL: only send xx 27 for LOADED decks (matches Serato's
                # behavior — in the smooth-playing capture, empty decks got
                # ZERO xx 27 packets, only loaded decks did). Sending an
                # "empty deck" xx 27 resets the firmware's global display
                # state, causing the BPM decimal to flicker to .0 even on
                # the loaded deck. Per-deck packets carry their own deck byte
                # at [0], so they only affect that deck's display.
                if not st.loaded:
                    continue
                send_pkt(self.ep_out,
                         build_xx27(DECK_BYTES[d], st.loaded, st.bpm, st.pos,
                                    duration_sec=st.duration))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


class RefreshThread(threading.Thread):
    """xx 36 trickle at the current playhead at 20 Hz.

    Trickling proved better than streaming-forward (Serato's pattern) for our
    daemon: fast seek response and tight playhead tracking. Both approaches
    hit the same ~2-min waveform reset — that's a firmware-side limit we
    haven't cracked yet."""
    def __init__(self, ep_out, interval_s=0.125):
        super().__init__(daemon=True)
        self.ep_out   = ep_out
        self.interval = interval_s   # 2026-05-29: REVERTED to 8 Hz (0.125s)
                                     # to match Serato's measured xx 36 slow-
                                     # stream cadence. Forensic audit of macOS
                                     # captures showed Serato sends xx 36 at
                                     # mean ~14 Hz (dual cadence: 22ms fast +
                                     # 125ms slow), with forward-streaming
                                     # entries (+18-19 per packet) and only
                                     # 0.4% consecutive same-entry packets.
                                     # The previous 50 Hz refresh + anchor-at-
                                     # playhead pattern produced ~80% same-
                                     # entry packets — likely interpreted by
                                     # the firmware as "redraw at this entry"
                                     # and producing the residual wave-content
                                     # flash. Combined with the entry-dedup
                                     # below, our wire should now match Serato.
        self._last_entry = {1: None, 2: None, 3: None, 4: None}
        self._stop    = threading.Event()

    def run(self):
        # Simple xx 36 trickle: one packet per tick at the current playhead,
        # deduped (skip when the entry hasn't advanced). 2026-05-29: REVERTED
        # the v7 "contiguous free-running streaming" experiment. v7 was added
        # for a starvation hypothesis that turned out WRONG (the once-per-second
        # flash was the xx27 ms-overflow, fixed in screen.js). v7 pushed the
        # xx36 rate to ~2x Serato (~14/s vs ~7/s) with bursts, and is the prime
        # suspect for the ~27 s firmware hang (screen.js xx27 writes stalled
        # while this hidraw trickle kept going). The wave scroll is driven by
        # screen.js xx27, not this trickle — this just keeps the buffer alive.
        ENTRIES_PER_PKT = 19
        while not self._stop.is_set():
            for d in (1, 2, 3, 4):
                st = DECKS[d]
                if not (st.loaded and st.pwv5 and st.duration > 0):
                    continue
                if st.uploading:
                    continue   # don't fight the bulk buffer-fill (flash fix v6)
                n_entries = len(st.pwv5) // 2
                entry = int(interp_pos(st) * n_entries)
                if entry < 0:
                    entry = 0
                if entry > n_entries - ENTRIES_PER_PKT:
                    entry = max(0, n_entries - ENTRIES_PER_PKT)
                if entry == self._last_entry[d]:
                    continue   # dedup: only send when the playhead entry advances
                self._last_entry[d] = entry
                send_pkt(self.ep_out, _xx36_packet(DECK_BYTES[d], entry, st.pwv5))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


# ===== Track-load upload sequence ==========================================

def send_xx30(ep, deck, duration_sec=0.0):
    """Send xx 30 init/track-load packet.

    2026-05-25 finding: highbpm Serato capture shows xx 30 includes TRACK
    LENGTH at bytes [6..9] in the same min/sec/ms-LE16 format as the time
    bytes. Hypothesis: firmware uses this LENGTH as a reference for time
    computation, possibly fixing the drift we observed.
    """
    p = zeros()
    p[0] = DECK_BYTES[deck]
    p[1] = 0x30
    p[2] = 0x01
    p[4] = 0x01
    # Encode track length at bytes [6..9]: min(1B), sec(1B), ms(LE16)
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
    # 2026-05-29: removed the 0xff-every-6-bytes pattern at [10,16,22,...].
    # Byte-level diff vs flx10-serato-loading4tracks shows Serato's xx30 is
    # ALL 0x00 after byte 9 (10 30 01 00 01 00 <min sec ms ms> 00 00 ...).
    # Our stray 0xff bytes may have selected a different firmware track-load
    # mode (candidate cause of the once-per-second sub-second-wrap redraw).
    send_pkt(ep, p)


def send_xx35(ep, deck, n_entries=0):
    """xx 35 carries the TOTAL WAVEFORM ENTRY COUNT at bytes [2][3] as LE16.
    Verified 2026-05-25 from flx10-serato-loading4tracks: deck 0x10 sent
    `10 35 38 81` (= 33080 = 220.54s × 150 fps for the 220.54s track),
    deck 0x20 sent `20 35 8a c1` (= 49546 = 330.31s × 150 fps).
    Firmware uses this count to scale the wave needle position; sending 0
    causes wave misalignment."""
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


def send_xx39(ep, deck):
    db = DECK_BYTES[deck]
    for hex_str in _XX39_HEX:
        p = bytearray.fromhex(hex_str)
        p[0] = db
        send_pkt(ep, bytes(p))


# ===== xx 2f beat-grid encoding ============================================
# Decoded packet layout (128 bytes each):
#   bytes 0..5  : header  = deck_byte 0x2f seq(1..N) 0x00 0x15 0x00
#   bytes 6..125: payload = 30 records × 4 bytes  (= 120 bytes)
#   bytes 126,127: 2 padding bytes (zero)
# Each record: [beat_type, pos_low, pos_mid, pos_high]
#   beat_type cycles 0x03,0x04,0x00,0x02 for beats 1,2,3,4 in a 4/4 bar.
#   position = LE24 sample count at 22050 Hz:
#       samples = round(beat_time_ms * 22050 / 1000)
# First record is a fixed marker: [0x80, 0x02, 0x01, 0x00] (samples=258).
# Serato generates ~22 markers/beat (fine sub-grid); we emit one per beat.

_XX2F_SR        = 22050           # position sample-rate denominator
_XX2F_BEAT_TYPE = (0x03, 0x04, 0x00, 0x02)   # cycles per 4/4 bar
_XX2F_RECS_PER_PKT = 30
_XX2F_MARKER    = (0x80, 0x02, 0x01, 0x00)   # first-record marker (samples=258)


def _xx2f_record(beat_idx, beat_time_ms):
    """Encode one 4-byte beat record."""
    samples = int(round(beat_time_ms * _XX2F_SR / 1000.0)) & 0xFFFFFF
    btype = _XX2F_BEAT_TYPE[beat_idx & 0x03]
    return (btype,
            samples & 0xFF,
            (samples >> 8) & 0xFF,
            (samples >> 16) & 0xFF)


def encode_xx2f_packets(deck_byte, beat_times_ms):
    """Encode a beat-time list into 128-byte xx 2f packets.

    Layout per packet: 6-byte header + 30 × 4-byte records + 2 pad bytes.
    First record of the entire stream is the fixed 0x80 0x02 0x01 0x00 marker
    (= samples 258, ~11.7ms at 22050 Hz) — Serato uses this as a track-start
    sentinel. Remaining records carry one entry per downbeat.

    Returns a list of bytes() packets.
    """
    # records = [marker] + one record per beat
    records = [_XX2F_MARKER]
    for i, t in enumerate(beat_times_ms):
        records.append(_xx2f_record(i, t))

    # Chunk into packets of 30 records each.
    packets = []
    total_segs = max(1, (len(records) + _XX2F_RECS_PER_PKT - 1) // _XX2F_RECS_PER_PKT)
    for seg_idx in range(total_segs):
        chunk = records[seg_idx * _XX2F_RECS_PER_PKT : (seg_idx + 1) * _XX2F_RECS_PER_PKT]
        p = zeros()
        # Header: deck 0x2f seq 0x00 0x15 0x00
        p[0] = deck_byte
        p[1] = 0x2f
        p[2] = (seg_idx + 1) & 0xFF
        p[3] = 0x00
        p[4] = 0x15
        p[5] = 0x00
        # Records start at byte 6.
        off = 6
        for rec in chunk:
            p[off]     = rec[0]
            p[off + 1] = rec[1]
            p[off + 2] = rec[2]
            p[off + 3] = rec[3]
            off += 4
        # bytes 126,127 stay zero (padding)
        packets.append(bytes(p))
    return packets


def send_xx2f(ep, deck, beat_times_ms=None):
    """Send the xx 2f beat-grid packets for this deck.

    With no beat list, falls back to the original header-only placeholder
    (kept for callers that just want to register the packet type).
    """
    db = DECK_BYTES[deck]
    if not beat_times_ms:
        p = zeros()
        p[0] = db; p[1] = 0x2f; p[2] = 0x01; p[4] = 0x01
        send_pkt(ep, p)
        return 0, 1
    packets = encode_xx2f_packets(db, beat_times_ms)
    for pkt in packets:
        send_pkt(ep, pkt)
    return len(beat_times_ms), len(packets)


# ===== Rekordbox-mode init placeholders (added 2026-05-24) ==================
# Decoded from flx10-rekordbox-opening.pcapng. Rekordbox sends these tiny
# placeholders per deck at startup BEFORE any track is loaded. The firmware
# requires them to know "this packet type is supported" — without them,
# the wave display modes are not selectable from the physical jog buttons.
# Content is essentially all-zero with just type+segment headers.

def send_xx2d(ep, deck):
    """xx 2D — purpose unknown, but required for rekordbox init. 1 packet/deck."""
    p = zeros()
    p[0] = DECK_BYTES[deck]; p[1] = 0x2d; p[2] = 0x01; p[4] = 0x01
    send_pkt(ep, p)


def send_xx3e(ep, deck):
    """xx 3E — purpose unknown, but required for rekordbox init. 1 packet/deck."""
    p = zeros()
    p[0] = DECK_BYTES[deck]; p[1] = 0x3e; p[2] = 0x01; p[4] = 0x01
    send_pkt(ep, p)


def send_xx2c_empty(ep, deck):
    """xx 2C overview waveform — empty placeholder = 35 zero-payload segments.
    Pattern: 10 2c NN 00 23 00 ... (byte[4]=0x23=35 total segments)."""
    db = DECK_BYTES[deck]
    for seg in range(1, 36):
        p = zeros()
        p[0] = db; p[1] = 0x2c; p[2] = seg; p[4] = 0x23
        send_pkt(ep, p)


def send_xx2e_empty(ep, deck):
    """xx 2E color waveform — empty placeholder. 3 packets/deck per capture."""
    db = DECK_BYTES[deck]
    for seg in range(1, 4):
        p = zeros()
        p[0] = db; p[1] = 0x2e; p[2] = seg; p[4] = 0x01
        send_pkt(ep, p)


def send_xx3d_display_mode(ep, mode):
    """Set the FLX10 jog display mode (1..5).

    Captured 2026-05-24 from flx10-rekordbox-menu-modes-cycle.pcapng:
    rekordbox sends `00 3d MODE 00 05 00 ...` on EP5 to set the jog display
    mode. The 5 modes observed (per user notes): wave-with-wave2 → wave-only
    → deck-wave-with-beatgrid → rekordbox-logo → album-artwork.

    Note: the original capture shows rekordbox cycling all 5 values every
    ~50ms as a heartbeat, so it's possible the firmware only acts on edge
    transitions and needs the full sequence — but we try the simple
    "single packet with desired mode" first; if it doesn't work we'll
    iterate to a full 1→2→…→N sweep.
    """
    p = zeros()
    p[0] = 0x00       # global (not per-deck)
    p[1] = 0x3d
    p[2] = mode & 0x0F
    p[4] = 0x05       # constant from capture
    send_pkt(ep, p)


def upload_xx33_album_art(ep, deck, jpeg_bytes):
    db = DECK_BYTES[deck]
    jpeg_size = len(jpeg_bytes)
    SEG1_CAP, SEG_CAP = 119, 122
    total_segs = 1 if jpeg_size <= SEG1_CAP else 1 + (jpeg_size - SEG1_CAP + SEG_CAP - 1) // SEG_CAP
    pos = 0
    for seg in range(1, total_segs + 1):
        p = zeros()
        p[0] = db; p[1] = 0x33; p[2] = seg
        p[4] = total_segs & 0xFF
        if seg == 1:
            p[6] = jpeg_size & 0xFF
            p[7] = (jpeg_size >> 8) & 0xFF
            take = min(SEG1_CAP, jpeg_size - pos)
            for j in range(take): p[9 + j] = jpeg_bytes[pos + j]
            pos += take
        else:
            take = min(SEG_CAP, jpeg_size - pos)
            for j in range(take): p[6 + j] = jpeg_bytes[pos + j]
            pos += take
        send_pkt(ep, p)


def upload_xx36_waveform(ep, deck, pwv5_bytes):
    """Upload the entire (already-fit-to-buffer) waveform at track-load."""
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


def _xx36_packet(deck_byte, pos_entries, pwv5_bytes, take=19):
    """Build a single xx 36 packet. pos_entries is the LE32 counter that tells
    the firmware where these entries belong (and, per the playhead-capture
    finding, this is ALSO how the firmware tracks the current playhead — the
    most recent xx 36's counter is the displayed center)."""
    p = zeros()
    p[0]  = deck_byte
    p[1]  = 0x36
    # 2026-05-29 forensic round 6: macOS Serato uses b2=0x00, b4=0x00 in
    # ALL xx 36 packets (504 in load + 208 in steady play). We were
    # sending 0x01 for both — possibly putting the firmware into a
    # different "chunk update" mode that produced the wave-content flash.
    # User's timeline observation (flash appeared after the xx 27 byte 8
    # sub-second fix) is consistent with this: the b8 cycling likely
    # unlocked a firmware "live deck" mode that's strict about xx 36
    # header byte values.
    p[2]  = 0x00
    p[4]  = 0x00
    p[6]  = 0x13
    p[10] =  pos_entries        & 0xFF
    p[11] = (pos_entries >> 8)  & 0xFF
    p[12] = (pos_entries >> 16) & 0xFF
    p[13] = (pos_entries >> 24) & 0xFF
    # Copy `take` entries (2 bytes each) starting at pos_entries × 2.
    src_off = pos_entries * 2
    src_end = min(src_off + take * 2, len(pwv5_bytes))
    n = src_end - src_off
    if n > 0:
        p[14:14 + n] = pwv5_bytes[src_off:src_end]
    return p


def send_scroll_update(ep, deck, pos_entries, pwv5_bytes):
    """Send a single xx 36 packet positioned at pos_entries — this updates
    the firmware's displayed playhead position. Matches Serato's behavior of
    continuously re-sending small xx 36 packets at the current playhead
    position during playback / scrubbing."""
    if not pwv5_bytes or pos_entries < 0:
        return
    p = _xx36_packet(DECK_BYTES[deck], pos_entries, pwv5_bytes)
    send_pkt(ep, p)


def get_test_jpeg():
    """240×240 red JPEG via PIL if available, else a minimal 1×1 hardcoded JPEG."""
    try:
        from PIL import Image
        import io
        img = Image.new("RGB", (240, 240), "red")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except ImportError:
        return bytes.fromhex(_MINIMAL_JPEG_HEX)


def handle_track_load(ep, deck, pwv5, label="", duration_sec=0.0, file_bpm=0.0):
    """Run the full Serato upload sequence with PWV5 bytes for this deck.
    Also caches the PWV5 on the deck state so the scroll thread can do
    position-update bursts without re-parsing.
    2026-05-25: pass duration_sec so xx 30 carries the track length the
    firmware may use as a time reference. Also pass file_bpm so we can
    synthesize a constant-BPM beat grid and send it as xx 2f."""
    if not pwv5:
        print(f"  deck {deck}: empty waveform, skipping upload")
        return
    DECKS[deck].pwv5 = bytes(pwv5)
    print(f"  deck {deck}: uploading {len(pwv5)} bytes ({len(pwv5)//2} entries) {label} dur={duration_sec:.1f}s")
    # v6: gate the RefreshThread off this deck for the whole bulk-fill so the
    # 8 Hz trickle can't interleave snap-backs into the sweep (the flash).
    DECKS[deck].uploading = True
    try:
        send_xx30(ep, deck, duration_sec=duration_sec)
        # xx39 = HOT CUE display setup (the packet payload contains the ASCII
        # "HOT CUE" — decoded 2026-05-29). NOT a key-sync indicator: the earlier
        # "blue indicator" attribution was wrong. Removing xx39 dropped a hot-cue
        # UI element from the jog screen, which is why it looked like a missing
        # indicator. Sent once per track load to set up the hot-cue display.
        send_xx39(ep, deck)
        upload_xx33_album_art(ep, deck, get_test_jpeg())
        # xx 35 entry count = duration × 150, matching Serato's pattern.
        # (Serato sends true entry count for the track at 150 fps even when wave
        # data extends past the firmware's display buffer.)
        xx35_entries = int(round(duration_sec * 150))
        send_xx35(ep, deck, n_entries=xx35_entries)
        upload_xx36_waveform(ep, deck, pwv5)
        # The bulk fill leaves the firmware's playhead-counter parked at the
        # END of the buffer (entry N). Immediately re-park it at the REAL
        # playhead so the display lands on the current position instead of
        # the track end, and reset the RefreshThread's dedup memory so its
        # first post-upload tick is guaranteed to send.
        st = DECKS[deck]
        n_entries = len(st.pwv5) // 2
        ipos = interp_pos(st)
        park = int(ipos * n_entries)
        if park < 0: park = 0
        if park > n_entries - 19: park = max(0, n_entries - 19)
        send_scroll_update(ep, deck, park, st.pwv5)
    finally:
        DECKS[deck].uploading = False
    # xx 2f beat grid — DENSE 16ms grid (62.5 Hz, one audio buffer at 48k/768).
    # 2026-05-29: switched from eighth-note (~250ms at 120 BPM, ~15x sparser
    # than Serato) to 16ms intervals. Decoded from Serato capture
    # flx10-serato-tracks_starting-playing.pcapng:
    #   pkt seq=0x02+ records are at +345 samples each (= 0.0156s @ 22050 Hz
    #   = ~62.5 Hz audio-buffer rate), with btype cycling 0x03→0x04→0x00→0x02
    #   in straight chronological order (matches our cycle already).
    #
    # Hypothesis: firmware uses the xx 2f grid to interpolate the wave-needle
    # between sparse xx 27 state pings. Our sparse eighth-note grid gave the
    # firmware almost nothing, so it fell back to default interpolation —
    # observable as a "skip" every ~quarter-note. Density may fix this.
    #
    # Cost: for a 4-min track that's ~15000 records ÷ 30 per packet = 500 xx 2f
    # packets at track-load. /dev/hidraw sustains >1000 Hz so this completes
    # in <0.5s. Compared to Serato (~21 packets in the load capture), this is
    # 23x more — Serato may stream additional dense packets at playback time
    # instead of front-loading everything. If load time becomes painful, cap
    # via _XX2F_DENSE_DURATION_S below.
    _XX2F_DENSE_INTERVAL_MS = 16.0          # 62.5 Hz, matches Serato
    _XX2F_DENSE_DURATION_S  = duration_sec  # whole-track for now
    if file_bpm and file_bpm > 0 and duration_sec > 0:
        n_records = int(_XX2F_DENSE_DURATION_S * 1000.0 / _XX2F_DENSE_INTERVAL_MS)
        beat_times_ms = [i * _XX2F_DENSE_INTERVAL_MS for i in range(n_records)]
        n_sent, n_pkts = send_xx2f(ep, deck, beat_times_ms)
        print(f"[beatgrid] deck {deck}: sent {n_sent} 16ms-grid records in "
              f"{n_pkts} packets ({_XX2F_DENSE_DURATION_S:.1f}s coverage @ "
              f"{_XX2F_DENSE_INTERVAL_MS:.1f}ms)")
    else:
        print(f"[beatgrid] deck {deck}: skipped (file_bpm={file_bpm}, dur={duration_sec})")


# (Removed earlier xx 36 scroll-update thread — turned out the playhead
# encoding lives in xx 27 [5..8] LE32, not in re-uploads of xx 36 entries.)


# ===== On-the-fly waveform load from Mixxx's analysis files ================
# Mixxx has already analyzed every track in your library — its waveform binary
# lives at ~/.mixxx/analysis/<analysis_id> (4 bytes BE uncompressed length +
# zlib protobuf). We parse + downsample + PWV5-encode in memory at track-load
# time; no pre-render cache needed.

import zlib, struct

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
    """Downsample Mixxx waveform (visual_sr Hz) → target_fps. Returns LE16
    PWV5 bytes (low band → red, mid → green, high → blue, all → height).

    Mixxx stores 4 values per visual frame (all/low/mid/high) — verified
    by previous working RGB output. The `visual_sr` field returned by the
    parser is double the ACTUAL visual frame rate (for a 263.8s track,
    visual_sr=441 produced 232666 values = 58166 frames × 4 vals, which
    means real visual fps = 58166/263.8 ≈ 220.5 = visual_sr/2). So we
    divide visual_sr by 2 when computing the downsample ratio. Without
    this, in_per_out is 2× too high and we produce half the entries the
    firmware needs, causing the waveform to cut off at ~half the track."""
    n_in_frames = len(values) // 4
    if n_in_frames == 0:
        return bytearray()
    actual_visual_fps = visual_sr / 2.0
    in_per_out = actual_visual_fps / target_fps     # e.g. 220.5/150 = 1.47
    n_out_frames = int(n_in_frames / in_per_out)
    print(f"  [waveform] visual_sr={visual_sr} (actual_fps={actual_visual_fps}) "
          f"n_values={len(values)} n_in_frames={n_in_frames} "
          f"in_per_out={in_per_out:.3f} n_out_frames={n_out_frames}")
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
    """Load PWV5 waveform at canonical 150 fps (Serato/Pioneer spec).
    Wave covers up to ~163 sec for long tracks (firmware buffer limit) but
    needle alignment is maintained by sending xx 35 with duration × 150."""
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
        print(f"  [waveform] produced {n_entries} entries at 150 fps "
              f"(covers {n_entries/150:.1f}s of audio)")
        return pwv5, None
    except Exception as e:
        return None, f"parse failed: {e}"


# ===== Track ↔ library lookup (samples + file_bpm → track_id) ==============
# scripts.js logs track_samples + file_bpm + duration; the daemon looks up
# the unique library row by matching computed samplerate*duration to samples
# (within slop) AND file_bpm exact (within 0.5 BPM). This is pitch-invariant
# because file_bpm and samples never change with the user's pitch fader.

def find_track_id(samples, file_bpm, duration, tol_samples=5000, tol_bpm=0.5, tol_dur=2.0):
    """Return track_locations.id (= analysis file id) whose
    (samplerate * duration * channels) ≈ samples AND file_bpm ≈ library bpm.
    Mixxx's track_samples is the MULTI-CHANNEL total (per-channel × channels).
    tol_samples=5000 covers ~50ms of slop for 44100/48000 Hz stereo."""
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


# ===== mixxx.log tailer =====================================================

TRACK_LOAD_PATTERN = re.compile(
    r"FLX10_TRACK_LOAD\s+deck=(\d+)\s+samples=([\d.]+)\s+file_bpm=([\d.]+)\s+duration=([\d.]+)")
POS_PATTERN = re.compile(r"FLX10_POS\s+deck=(\d+)\s+pos=([\d.]+)")
BPM_PATTERN = re.compile(r"FLX10_BPM\s+deck=(\d+)\s+bpm=([\d.]+)")
CYCLE_DISPLAY_PATTERN = re.compile(
    r"FLX10_CYCLE_JOG_DISPLAY\s+side=(left|right)\s+seq=(\d+)")


def tail_mixxx_log(log_path, on_track_load, on_pos_update, on_bpm_update,
                   on_cycle_display=None):
    """Tail mixxx.log forever; dispatch FLX10_TRACK_LOAD and FLX10_POS lines.
    Reopens the file if Mixxx rotates it."""
    while not os.path.exists(log_path):
        time.sleep(0.5)
    inode = os.stat(log_path).st_ino
    f = open(log_path, "r", errors="replace")
    f.seek(0, 2)
    print(f"Tailing {log_path} for FLX10 events …")
    # Tail-lag diagnostic: every second, count processed lines and print a
    # summary so we can see if the tail is keeping up with Mixxx's writes.
    pos_count = 0
    last_diag = time.time()
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.05)   # responsive — 50ms not 300ms
            try:
                if os.stat(log_path).st_ino != inode:
                    f.close()
                    while not os.path.exists(log_path):
                        time.sleep(0.5)
                    f = open(log_path, "r", errors="replace")
                    inode = os.stat(log_path).st_ino
                    print("(reopened mixxx.log after rotation)")
            except OSError:
                pass
            now = time.time()
            if now - last_diag >= 1.0:
                # Compute lag: file size vs our read position
                try:
                    size = os.stat(log_path).st_size
                    pos  = f.tell()
                    bytes_behind = size - pos
                    print(f"[tail-diag] processed {pos_count} pos lines/sec, "
                          f"{bytes_behind} bytes behind end-of-log")
                except OSError:
                    pass
                pos_count = 0
                last_diag = now
            continue
        m = TRACK_LOAD_PATTERN.search(line)
        if m:
            on_track_load(int(m.group(1)),
                          float(m.group(2)),    # samples
                          float(m.group(3)),    # file_bpm
                          float(m.group(4)))    # duration
            continue
        m = POS_PATTERN.search(line)
        if m:
            on_pos_update(int(m.group(1)), float(m.group(2)))
            pos_count += 1
            continue
        m = BPM_PATTERN.search(line)
        if m:
            on_bpm_update(int(m.group(1)), float(m.group(2)))
            continue
        m = CYCLE_DISPLAY_PATTERN.search(line)
        if m and on_cycle_display is not None:
            on_cycle_display(m.group(1))   # 'left' or 'right'
            continue


# ===== Main =================================================================

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log",    default=MIXXX_LOG, help="mixxx.log path (default ~/.mixxx/mixxx.log)")
    ap.add_argument("--unlock", action="store_true", help="Run vendor unlock at startup")
    ap.add_argument("--pos-rate", type=float, default=128.0,
                    help="Position encoding rate (units per second of audio). "
                         "Default 128 gives in-sync scroll. Only override if you observe drift.")
    ap.add_argument("--pwv5-fps", type=float, default=73.0,
                    help="Waveform entries per second of audio. Empirically 73 "
                         "gives best wave-vs-playhead sync (verified by user on "
                         "2026-05-23). Lower values = wave ahead; higher = behind.")
    ap.add_argument("--no-trickle", action="store_true",
                    help="Skip the 20Hz xx 36 trickle. Without it the firmware drops the "
                         "waveform after ~1 min, but this is useful for diagnosing whether "
                         "the trickle is corrupting other display fields.")
    args = ap.parse_args()
    global POS_RATE, PWV5_FPS
    POS_RATE = args.pos_rate
    PWV5_FPS = args.pwv5_fps
    print(f"Playhead encoding: BE24 of xx 27 [5,6,7] = pos × duration_sec × {POS_RATE}")
    print(f"Waveform encoding: {PWV5_FPS} entries per second of audio")

    if os.geteuid() != 0:
        print("WARNING: not running as root — interface claim may fail")

    dev = find_device()
    if args.unlock:
        vendor_unlock(dev)
    # NOTE: we do NOT detach_kernel_driver or claim_interface anymore — that
    # was exclusive and prevented Mixxx's HID screen.js from coexisting on
    # interface 5. Vendor unlock above uses control transfers which don't
    # require a claim.

    # Open the hidraw device (non-exclusive — multi-writer via kernel queue)
    ep_out = open_hidraw()

    # Initial per-deck init packets (Serato-mode baseline)
    print("Sending xx 30 + xx 39 init to all 4 decks …")
    for d in (1, 2, 3, 4):
        send_xx30(ep_out, d)
        send_xx39(ep_out, d)   # xx39 = HOT CUE display setup (see handle_track_load)

    # xx 27 is handled ENTIRELY by Mixxx's HID screen.js, with zero position
    # bytes [5..7] (so firmware uses [9..12] for accurate time). Wave shape
    # is uploaded by daemon's xx 36 but wave does NOT scroll — that's the
    # accepted tradeoff: accurate time over scrolling wave.
    # (Hybrid 2Hz daemon-kick attempt caused visible flicker between the two
    # position sources. Reverted 2026-05-24.)
    pinger = None
    print("xx 27 state-ping DISABLED in daemon (screen.js owns it for accurate time)")

    # Start xx 36 trickle (20 Hz per loaded deck — keeps firmware buffer alive
    # and writes entries at the current playhead position)
    refresher = None
    if not args.no_trickle:
        refresher = RefreshThread(ep_out)
        refresher.start()
        print("xx 36 trickle started (5 Hz per loaded deck, writes entries at current playhead)")
    else:
        print("xx 36 trickle DISABLED (--no-trickle) — waveform will revert to static after ~1 min")

    # Track-load callback — parse Mixxx's analysis file on-the-fly (no cache).
    # Matches the library row using track_samples + file_bpm (both invariant to
    # the pitch fader; deterministic identifier).
    def on_track_load(deck, samples, file_bpm, duration):
        st = DECKS.get(deck)
        if st is None: return
        track_id = find_track_id(samples, file_bpm, duration)
        if track_id is None:
            print(f"\n[deck {deck}] track load: samples={samples:.0f} file_bpm={file_bpm} "
                  f"dur={duration:.1f}s — NO MATCH in Mixxx library")
            return
        if st.track_id == track_id:
            return
        st.track_id = track_id
        st.bpm      = file_bpm
        st.duration = duration   # needed for position encoding
        st.loaded   = True
        st.pos      = 0.0
        print(f"\n[deck {deck}] track load: track_id={track_id} "
              f"samples={samples:.0f} file_bpm={file_bpm} dur={duration:.1f}s")
        pwv5, err = waveform_for_track(track_id, duration_sec=duration)
        if err:
            print(f"  deck {deck}: {err}")
            return
        handle_track_load(ep_out, deck, pwv5, label=f"(track_id={track_id})",
                          duration_sec=duration, file_bpm=file_bpm)

    def on_pos_update(deck, pos):
        st = DECKS.get(deck)
        if st is None: return
        # Seek detection: a single 100ms tick should advance pos by at most
        # ~(rate × 0.1 / duration). Any jump bigger than 0.02 (= 2% of track)
        # is a seek (click in Mixxx waveform, hot cue, etc.). RefreshThread
        # watches this counter to immediately upload the new playhead area.
        if st.duration > 0 and abs(pos - st.last_pos_val) > 0.02:
            st.last_seek_seq += 1
        # Track two consecutive updates so interp_pos() can estimate the play
        # rate between Mixxx's 100ms log ticks.
        st.prev_pos_val = st.last_pos_val
        st.prev_pos_ts  = st.last_pos_ts
        st.last_pos_val = pos
        st.last_pos_ts  = time.time()
        st.pos          = pos          # legacy, still read by some callers

    def on_bpm_update(deck, bpm):
        st = DECKS.get(deck)
        if st is None: return
        st.bpm = bpm   # active BPM (pitch-affected) for xx 27 [13]/[15]

    # Jog display mode cycling. State persists per-side. The mode wraps 1..5.
    # Both sides share the same firmware-side mode in the original captures
    # (byte [0] = 0x00 = global), so for now both buttons drive a single mode.
    cycle_state = {"mode": 1}
    def on_cycle_display(side):
        cycle_state["mode"] = (cycle_state["mode"] % 5) + 1   # 1→2→3→4→5→1
        print(f"[cycle-display] side={side} → mode={cycle_state['mode']}", flush=True)
        # Try sweep approach: send 1..N values (rekordbox cycles all 5 every
        # 50ms continuously, so firmware may require the full sweep ending on
        # the desired mode rather than a single set-packet).
        for m in range(1, cycle_state["mode"] + 1):
            send_xx3d_display_mode(ep_out, m)
            time.sleep(0.002)

    # Rekordbox-mode threads (xx 3D heartbeat + xx 2C trickle) — disabled
    # after the rekordbox-mode pivot hit a firmware auth wall. Kept in code
    # for future use. To re-enable, set heartbeat_running["on"] = True.
    heartbeat_running = {"on": False}

    try:
        tail_mixxx_log(args.log, on_track_load, on_pos_update, on_bpm_update,
                       on_cycle_display=on_cycle_display)
    except KeyboardInterrupt:
        print("\nShutting down …")
    finally:
        heartbeat_running["on"] = False
        if pinger is not None:
            pinger.stop()
            pinger.join(timeout=1)
        if refresher is not None:
            refresher.stop()
            refresher.join(timeout=1)
        usb.util.release_interface(dev, SCREEN_INTERFACE)
        try: dev.attach_kernel_driver(SCREEN_INTERFACE)
        except Exception: pass
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    main()
