#!/usr/bin/env python3
"""
flx10_rekordbox_proto.py — replicate rekordbox's FLX10 jog-wheel screen protocol.

Decoded from /home/vpinedax/Downloads/flx10-4decks-loaded.pcapng (rekordbox, 4 decks
loaded). Rekordbox uses a DIFFERENT command set from VirtualDJ:

  - xx 3D heartbeat (broadcast, deck=0x00) — burst of 5 every ~20 ms, runs continuously
  - xx 21 metadata    — per-deck state, ~50 Hz, byte layout differs from VirtualDJ
  - xx 2C overview    — 35 packets, byte[4]=0x23, byte[5]=0x00 (no marker)
  - xx 2E waveform    — PWV5 LE16 color, byte[4]=0xB9, byte[5]=0x03 marker
  - xx 39 hot-cue label text — burst of 3 packets, ASCII text in seg 1 (not required)

Critical observation from the capture: rekordbox sends ONLY heartbeats for the first
~4.8 seconds before any metadata or waveform. This script does the same: 2 seconds of
heartbeats first, then metadata/upload, then sustained heartbeats + metadata + small
playhead packets to mimic active playback.

Run with Mixxx closed:
  sudo python3 flx10_rekordbox_proto.py --deck 2 --playing

PWV5 LE16 entry encoding (Pioneer half-frame waveform, 150 entries/sec):
  v = (r<<13) | (g<<10) | (b<<7) | (h<<2)        # r/g/b in 0..7, h in 0..31
  bytes = (v & 0xff, v >> 8)                      # little-endian
"""

import argparse
import math
import os
import re
import subprocess
import sys
import time
import threading

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("pyusb not found.  pip install pyusb")

VID = 0x2B73
PID = 0x0041
SCREEN_INTERFACE = 5

DECK_BYTES      = {1: 0x10, 2: 0x20, 3: 0x30, 4: 0x40}
ENTRIES_PER_SEC = 150
SEG_PAYLOAD     = 122      # bytes[6..127]

# 7 vendor OUT commands present in rekordbox/Serato/VirtualDJ full-init
# captures. Removes the "no audio driver" message on the FLX10 LCDs on Linux.
VENDOR_UNLOCK_CMDS = [
    (0x0100, 0xC028),
    (0x0000, 0xC029),
    (0x0200, 0xC013),
    (0x0000, 0xC02B),
    (0x0100, 0xC026),
    (0x0000, 0xC01D),
    (0x0100, 0xC027),
]

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def find_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("DDJ-FLX10 not found (VID 2B73 / PID 0041). Is it plugged in?")
    print(f"Found DDJ-FLX10 — bus {dev.bus}, device {dev.address}")
    return dev


def vendor_unlock(dev):
    """Send the 7-command vendor unlock handshake. Same as flx10_unlock_v2.py
    but without the snd-usb-audio unbind/rebind (which is only needed if the
    LCDs show 'no audio driver'). Always safe to call before screen tests."""
    print("Vendor unlock: sending 7 commands …")
    for i, (wValue, wIndex) in enumerate(VENDOR_UNLOCK_CMDS, 1):
        try:
            dev.ctrl_transfer(0x40, 3, wValue, wIndex, None, timeout=200)
            print(f"  [{i}/7] wValue=0x{wValue:04X} wIndex=0x{wIndex:04X}  OK")
        except usb.core.USBError as e:
            print(f"  [{i}/7] wValue=0x{wValue:04X} wIndex=0x{wIndex:04X}  FAIL: {e}")
        time.sleep(0.005)
    time.sleep(0.2)


def maybe_rebind_audio():
    """Optional snd-usb-audio rebind to clear 'no audio driver' message on LCDs.
    Runs the same sysfs unbind/rebind as flx10_unlock_v2.py. Best-effort; errors
    are common (interface re-enumeration), and safe to ignore."""
    import glob
    sysfs = None
    for vf in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            if int(open(vf).read().strip(), 16) == VID:
                pf = vf.replace("idVendor", "idProduct")
                if int(open(pf).read().strip(), 16) == PID:
                    sysfs = vf.rsplit("/", 1)[0]
                    break
        except Exception:
            continue
    if sysfs is None:
        return
    intfs = []
    for intf_dir in sorted(glob.glob(f"{sysfs}/*:*")):
        drv = intf_dir + "/driver"
        if os.path.islink(drv) and os.path.basename(os.readlink(drv)) == "snd-usb-audio":
            intfs.append(os.path.basename(intf_dir))
    if not intfs:
        return
    print(f"Rebinding snd-usb-audio on {len(intfs)} interface(s) …")
    for path, action in [("/sys/bus/usb/drivers/snd-usb-audio/unbind", "unbind"),
                         ("/sys/bus/usb/drivers/snd-usb-audio/bind",   "bind")]:
        for i in intfs:
            try:
                with open(path, "w") as f:
                    f.write(i)
            except OSError as e:
                pass  # interface may have vanished due to re-enum; OK
        time.sleep(0.3)


def get_screen_endpoints(dev):
    cfg  = dev.get_active_configuration()
    intf = cfg[(SCREEN_INTERFACE, 0)]
    ep_out = usb.util.find_descriptor(
        intf,
        custom_match=lambda e: (
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR
        ))
    ep_in = usb.util.find_descriptor(
        intf,
        custom_match=lambda e: (
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR
        ))
    if ep_out is None or ep_in is None:
        sys.exit(f"Could not find interrupt IN/OUT endpoints on interface {SCREEN_INTERFACE}.")
    print(f"Endpoints — OUT: 0x{ep_out.bEndpointAddress:02X}, IN: 0x{ep_in.bEndpointAddress:02X}")
    return ep_out, ep_in


def build_pkt(b0, b1, seg, sub, b4):
    pkt = bytearray(128)
    pkt[0] = b0 & 0xFF
    pkt[1] = b1 & 0xFF
    pkt[2] = seg & 0xFF
    pkt[3] = sub & 0xFF
    pkt[4] = b4 & 0xFF
    return pkt


# A single lock makes pyusb writes serialized — needed because the heartbeat
# runs on a background thread and we don't want it interleaving mid-upload.
_send_lock = threading.Lock()

def send_pkt(ep_out, pkt):
    with _send_lock:
        ep_out.write(bytes(pkt), timeout=1000)


def drain_acks(ep_in, count=10, timeout_ms=50, label=""):
    got = 0
    for _ in range(count):
        try:
            data = ep_in.read(64, timeout=timeout_ms)
            print(f"  ACK{(' (' + label + ')') if label else ''}: {bytes(data).hex()}")
            got += 1
        except usb.core.USBTimeoutError:
            break
    return got


# ---------------------------------------------------------------------------
# xx 3D heartbeat — broadcast (deck=0x00), burst of 5 every ~20 ms
# ---------------------------------------------------------------------------

def heartbeat_burst(ep_out):
    """One 5-segment heartbeat burst, exactly like rekordbox."""
    for seg in range(1, 6):
        pkt = build_pkt(0x00, 0x3D, seg, 0x00, 0x05)
        send_pkt(ep_out, pkt)


class HeartbeatThread(threading.Thread):
    """Send heartbeat bursts at ~50 Hz in the background."""
    def __init__(self, ep_out, interval_s=0.02):
        super().__init__(daemon=True)
        self.ep_out  = ep_out
        self.interval = interval_s
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                heartbeat_burst(self.ep_out)
            except usb.core.USBError as e:
                print(f"  heartbeat error: {e}")
                break
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# MIDI SysEx polling — rekordbox sends `f0 00 40 05 00 00 04 01 00 50 00 f7`
# every ~200 ms on MIDI OUT (EP3) before the first screen heartbeat.  Hypothesis:
# this is a "host alive" / connection-establishment ping that puts the firmware
# into a mode where screen writes are honored.
# ---------------------------------------------------------------------------

SYSEX_PING = "F0 00 40 05 00 00 04 01 00 50 00 F7"

# One-shot "enter HID screen mode" SysEx — present in VirtualDJ and Serato
# captures BEFORE their first screen write, but absent from rekordbox.
# rekordbox doesn't need it because rekordbox uses xx 3D heartbeats which appear
# to enable a different mode.  Serato's variant of the keepalive (50 31) instead
# of rekordbox's 50 00 also suggests these opcodes select firmware behaviors.
SYSEX_ENTER_HID = "F0 00 40 05 00 00 04 01 00 03 01 F7"


def find_flx10_midi_port():
    """Return the amidi port name for the FLX10 (e.g. 'hw:4,0,0'), or None."""
    try:
        out = subprocess.check_output(["amidi", "-l"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    for line in out.splitlines():
        if "FLX10" in line and "hw:" in line:
            m = re.search(r"(hw:[0-9,]+)", line)
            if m:
                return m.group(1)
    return None


class SysExPingThread(threading.Thread):
    """Send the rekordbox `50 00` ping every 200 ms via amidi."""
    def __init__(self, port, interval_s=0.2):
        super().__init__(daemon=True)
        self.port = port
        self.interval = interval_s
        self._stop = threading.Event()
        self.sent = 0
        self.errors = 0

    def run(self):
        while not self._stop.is_set():
            try:
                subprocess.run(
                    ["amidi", "-p", self.port, "-S", SYSEX_PING],
                    check=False, timeout=1,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self.sent += 1
            except (OSError, subprocess.TimeoutExpired):
                self.errors += 1
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()


# ---------------------------------------------------------------------------
# xx 21 metadata — rekordbox layout (decoded from capture)
# ---------------------------------------------------------------------------

def _make_metadata_pkt(deck_byte, track_loaded=True, playing=False):
    """Rekordbox xx 21 metadata.  Byte positions verified from capture frames
    around `1021000a2001000280100000000e0000000000000000003bff...800d`.
    """
    pkt = bytearray(128)
    pkt[0]  = deck_byte
    pkt[1]  = 0x21
    pkt[3]  = 0x0a
    pkt[4]  = 0x20            # rekordbox playing-state byte (VirtualDJ used 0x0c)
    pkt[5]  = 0x01
    pkt[7]  = 0x02 if track_loaded else 0x01
    pkt[8]  = 0x80            # rekordbox-specific (was 0x00 in VirtualDJ)
    pkt[9]  = 0x10            # rekordbox-specific
    pkt[13] = 0x0e if track_loaded else 0x00  # active flag — appears once track recognized
    if track_loaded:
        pkt[23] = 0x3b
        pkt[24] = 0xff
        pkt[27] = 0x80
        pkt[40] = 0xff
        pkt[41] = 0xff
    pkt[58] = 0x80
    pkt[59] = 0x0d            # rekordbox uses 0x0d, not 0x1e (still not fully decoded)
    return pkt


def send_metadata(ep_out, deck, track_loaded=True, playing=False):
    db = DECK_BYTES.get(deck)
    if not db:
        return
    send_pkt(ep_out, _make_metadata_pkt(db, track_loaded, playing))


def metadata_burst(ep_out, loaded_deck, cycles=20, interval_s=0.02, playing=False):
    """Send xx 21 for all 4 decks at 50 Hz for `cycles` rounds."""
    for _ in range(cycles):
        for d in range(1, 5):
            send_metadata(ep_out, d,
                          track_loaded=(d == loaded_deck),
                          playing=(playing and d == loaded_deck))
        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# Serato xx 27 — Per-deck state ping (replaces xx 21 in Serato's protocol)
# ---------------------------------------------------------------------------
#
# Decoded from /home/vpinedax/Downloads/flx10-driverrutil-then-serato.pcapng.
# byte[27] differs per deck (deck 1=0x02, deck 2=0x01, deck 3=0x04, deck 4=0x03 guessed).

_SERATO_DECK_BYTE_27 = {0x10: 0x02, 0x20: 0x01, 0x30: 0x04, 0x40: 0x03}


def _make_serato_state_pkt(deck_byte, track_loaded=True):
    # Verified byte-for-byte against Serato capture state-5 (fully loaded) and
    # state-0 (empty) packets. Both have [32,33,34] = ff ff ff.
    # The (e0, 01) variant I previously set at [32,33] is a different field
    # entirely — likely a "playing-state changed" flag or similar — and putting
    # it on a state-5 packet broke render. Correct loaded packet:
    #   [0..4]=10 27 b4 80 01, [7..8]=e0 01, [9..12]=track-id (4B LE32),
    #   [13]=79, [15]=03, [16,17]=b0 ff (duration), [20]=0e, [21,22]=c2 03,
    #   [25]=80, [29]=92, [30]=0d, [31]=deck-state, [32..34]=ff ff ff.
    pkt = bytearray(128)
    pkt[0]  = deck_byte
    pkt[1]  = 0x27
    pkt[2]  = 0xb4
    pkt[3]  = 0x80
    pkt[4]  = 0x01
    pkt[20] = 0x0e
    pkt[25] = 0x80
    pkt[30] = 0x0d
    pkt[31] = _SERATO_DECK_BYTE_27.get(deck_byte, 0x02)
    pkt[32] = 0xff
    pkt[33] = 0xff
    pkt[34] = 0xff
    if track_loaded:
        pkt[7]  = 0xe0
        pkt[8]  = 0x01
        pkt[9]  = 0x06
        pkt[10] = 0x1b
        pkt[11] = 0xfa
        pkt[12] = 0x01
        pkt[13] = 0x79
        pkt[15] = 0x03
        pkt[16] = 0xb0
        pkt[17] = 0xff
        pkt[21] = 0xc2
        pkt[22] = 0x03
        pkt[29] = 0x92
    else:
        pkt[29] = 0x80
    return pkt


def send_serato_state(ep_out, deck, track_loaded=True):
    db = DECK_BYTES.get(deck)
    if not db:
        return
    send_pkt(ep_out, _make_serato_state_pkt(db, track_loaded))


def serato_state_burst(ep_out, loaded_deck, cycles=20, interval_s=0.02):
    print(f"Sending {cycles} Serato xx 27 state cycles (deck {loaded_deck} = loaded) …")
    for _ in range(cycles):
        for d in range(1, 5):
            send_serato_state(ep_out, d, track_loaded=(d == loaded_deck))
        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# Serato xx 36 — Waveform (PWV5 LE16, Serato framing)
# ---------------------------------------------------------------------------
#
# Same PWV5 encoding as rekordbox xx 2E but different framing:
#   [0]=deck  [1]=36  [2]=seg  [3]=sub  [4]=01  [5]=00
#   [6]=0x13 (=19, entries per packet)  [7]=00
#   [8..11] LE32 entry-position counter (increments by ~18 between packets)
#   [12..13] 00 00
#   [14..127] up to 19 PWV5 LE16 entries (38 bytes), rest zero-padded

def _load_pwv5_from_file(filepath):
    """Load pre-rendered PWV5 LE16 bytes from a .pwv5 cache file (produced by
    flx10_prerender_waveform.py)."""
    with open(filepath, "rb") as f:
        return bytearray(f.read())


def upload_serato_waveform(ep_out, deck, duration_sec, solid=False):
    # Verified packet layout from capture (all 419 deck-1 xx 36 packets):
    #   [0]=deck [1]=36 [2]=01 (CONSTANT, not a segment counter!) [3]=00
    #   [4]=01 [5]=00 [6]=0x13 [7..9]=00
    #   [10..13] = LE32 entry-position counter
    #   [14..51] = 19 PWV5 16-bit entries (38 bytes), rest zero-padded
    # Earlier bug: we incremented byte[2] like a segment counter, which made
    # the firmware reject every packet after the first. Fixed 2026-05-23.
    db = DECK_BYTES[deck]
    # Prefer a pre-rendered cache file if set for this deck; else use test pattern.
    wave_file = _WAVE_FILES.get(deck)
    if wave_file:
        entries_bytes = _load_pwv5_from_file(wave_file)
        print(f"Loaded {len(entries_bytes)} PWV5 bytes from {wave_file} for deck {deck}")
    else:
        entries_bytes = _generate_pwv5_entries(duration_sec, solid=solid)
    n_entries = len(entries_bytes) // 2

    ENTRIES_PER_PKT = 19
    pos = 0
    total = 0
    while pos < n_entries:
        take = min(ENTRIES_PER_PKT, n_entries - pos)
        pkt = bytearray(128)
        pkt[0]  = db
        pkt[1]  = 0x36
        pkt[2]  = 0x01     # constant
        pkt[3]  = 0x00
        pkt[4]  = 0x01
        pkt[5]  = 0x00
        pkt[6]  = 0x13
        # bytes [7..9] stay zero
        pkt[10] =  pos        & 0xFF
        pkt[11] = (pos >> 8)  & 0xFF
        pkt[12] = (pos >> 16) & 0xFF
        pkt[13] = (pos >> 24) & 0xFF
        nbytes = take * 2
        for j in range(nbytes):
            pkt[14 + j] = entries_bytes[pos * 2 + j]
        send_pkt(ep_out, pkt)
        pos += take
        total += 1
    print(f"Waveform (xx 36 Serato): sent {total} packets, "
          f"{n_entries} entries ({duration_sec:.1f}s) to deck {deck}")


# ---------------------------------------------------------------------------
# Serato xx 30 — Per-deck one-shot init
# ---------------------------------------------------------------------------
#
# Verified byte layout from Serato capture (deck 1 frame 7885):
#   [0]=deck [1]=30 [2]=01 [4]=01
#   [10]=ff [16]=ff [22]=ff [28]=ff [34]=ff [40]=ff [46]=ff [52]=ff  (8 flags, every 6 bytes)
#   rest = 00
# These 8 marker bytes are likely 8 pad/cue/feature enable flags.  Earlier
# version only set [10]=ff — that broke render.  All 8 must be set.

def send_serato_init30(ep_out, deck):
    db = DECK_BYTES[deck]
    pkt = bytearray(128)
    pkt[0]  = db
    pkt[1]  = 0x30
    pkt[2]  = 0x01
    pkt[4]  = 0x01
    for i in (10, 16, 22, 28, 34, 40, 46, 52):
        pkt[i] = 0xff
    send_pkt(ep_out, pkt)


# Serato xx 35 — 3 packets per deck, sent right BEFORE xx 36 waveform upload.
# Verified bytes from capture (deck 1):
#   pkt 0: [0]=deck [1]=35  rest=0
#   pkt 1: [0]=deck [1]=35 [2]=0e [3]=e3  rest=0
#   pkt 2: same as pkt 1
# Prime suspect for "begin waveform" signal — without it we never sent xx 36
# at all in our earlier test.
def send_serato_init35(ep_out, deck):
    db = DECK_BYTES[deck]
    # First packet: just header
    pkt = bytearray(128)
    pkt[0] = db
    pkt[1] = 0x35
    send_pkt(ep_out, pkt)
    # Two follow-up packets with 0e e3 marker
    for _ in range(2):
        pkt = bytearray(128)
        pkt[0] = db
        pkt[1] = 0x35
        pkt[2] = 0x0e
        pkt[3] = 0xe3
        send_pkt(ep_out, pkt)


# Serato xx 39 — pad-mode labels.  Verified byte arrays from deck-1 capture;
# we just swap byte[0] for the deck.
_XX39_SEG1_DECK1 = bytes.fromhex(
    "10390100030000484f54204355450000000000000000000000000000000000000000000000003f000000000000000000000000000000000000000000000000000000000000023f000000000000000000000000000000000000000000000000000000000000023f00000000000000000000000000000000000000000000000000")
_XX39_SEG2_DECK1 = bytes.fromhex(
    "1039020003000000000000023f000000000000000000000000000000000000000000000000000000000000023f000000000000000000000000000000000000000000000000000000000000023f000000000000000000000000000000000000000000000000000000000000023f00000000000000000000000000000000000000")
_XX39_SEG3_DECK1 = bytes.fromhex(
    "1039030003000000000000000000000000023f00000000000000000000000000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000")


def send_serato_init39_hotcue(ep_out, deck):
    db = DECK_BYTES[deck]
    for seg_bytes in (_XX39_SEG1_DECK1, _XX39_SEG2_DECK1, _XX39_SEG3_DECK1):
        pkt = bytearray(seg_bytes)
        pkt[0] = db
        send_pkt(ep_out, bytes(pkt))


# Serato xx 2f — cue point data, 54 packets per deck.  Most packets are empty
# (10 2f xx 00 01 00 ...) — the bulk contain cue point timestamps.  For our
# minimum-viable test we send just the first empty packet to satisfy any
# "must have at least one xx 2f" gate.  Real cue data per track would replace
# this in a proper Mixxx port.
def send_serato_init2f(ep_out, deck):
    db = DECK_BYTES[deck]
    pkt = bytearray(128)
    pkt[0] = db
    pkt[1] = 0x2f
    pkt[2] = 0x01
    pkt[4] = 0x01
    send_pkt(ep_out, pkt)


# ---------------------------------------------------------------------------
# Serato xx 33 — Album-art JPEG upload (also used by rekordbox)
# ---------------------------------------------------------------------------
#
# Decoded from Serato capture (frame 8088 = empty placeholder JPEG, frame 24245
# = actual album art).  Format:
#   SEG 1: [0]=deck [1]=33 [2]=1 [3]=0 [4]=total_segs [5]=00
#          [6..7]=jpeg_size_LE16 [8]=00 [9..127]=first 119B of JPEG
#   SEG N: [0]=deck [1]=33 [2]=N [3]=0 [4]=total_segs [5]=00
#          [6..127]=122B JPEG continuation

def upload_album_art(ep_out, deck, jpeg_bytes):
    db = DECK_BYTES[deck]
    jpeg_size = len(jpeg_bytes)
    SEG1_CAP = 119
    SEG_CAP  = 122
    if jpeg_size <= SEG1_CAP:
        total_segs = 1
    else:
        total_segs = 1 + (jpeg_size - SEG1_CAP + SEG_CAP - 1) // SEG_CAP

    pos = 0
    for seg in range(1, total_segs + 1):
        pkt = bytearray(128)
        pkt[0] = db
        pkt[1] = 0x33
        pkt[2] = seg
        pkt[3] = 0x00
        pkt[4] = total_segs & 0xFF
        pkt[5] = 0x00
        if seg == 1:
            pkt[6] = jpeg_size & 0xFF
            pkt[7] = (jpeg_size >> 8) & 0xFF
            pkt[8] = 0x00
            take = min(SEG1_CAP, jpeg_size - pos)
            for j in range(take):
                pkt[9 + j] = jpeg_bytes[pos + j]
            pos += take
        else:
            take = min(SEG_CAP, jpeg_size - pos)
            for j in range(take):
                pkt[6 + j] = jpeg_bytes[pos + j]
            pos += take
        send_pkt(ep_out, pkt)
    print(f"Album art (xx 33): sent {total_segs} packets ({jpeg_size} bytes) to deck {deck}")


def _generate_test_jpeg(size_px=240, color="red"):
    """Album-art-sized JPEG for xx 33.  Default 240×240 to match the FLX10's
    expected album art canvas (per integration notes).  Real Serato album-art
    JPEGs in the capture were ~10KB; a 240×240 solid-color JPEG is ~3-5KB
    which is much closer than our previous 32×32 / 645-byte placeholder."""
    try:
        from PIL import Image
        import io
        img = Image.new("RGB", (size_px, size_px), color)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except ImportError:
        # Minimal 1×1 red JPEG (~134 bytes), generated offline
        import binascii
        return binascii.unhexlify(
            "ffd8ffe000104a46494600010101006000600000ffdb00430008060607060508"
            "0707080908080a0d160e0d0c0c0d1c14150e15181e1f1e1819171a212a25211f"
            "281a17172e2f2c282c322c2e2c34302c2c2c2c2cffdb004301090a0a0d0c0d1a"
            "0e0e1a2c1d171d2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c"
            "2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2c2cffc0001108"
            "0001000103011100021101031101ffc4001500010100000000000000000000"
            "000000000005ffc40014100100000000000000000000000000000000ffc4001501"
            "010100000000000000000000000000000005ffc4001411010000000000000000"
            "00000000000000000000ffda000c03010002110311003f00bf3effd9"
        )


# ---------------------------------------------------------------------------
# xx 2C overview — 35 packets, byte[4]=0x23, byte[5]=0x00
# ---------------------------------------------------------------------------

def upload_overview(ep_out, deck):
    """xx 2C overview: 35 packets × 122 bytes payload.  Inner entry format isn't
    fully decoded; we send a sine-wave pattern that uses byte values across the
    full 0..255 range to maximize chance of visible output if any byte is height.
    """
    db = DECK_BYTES[deck]
    TOTAL = 35
    payload_bytes = TOTAL * SEG_PAYLOAD   # 35 * 122 = 4270
    stream = bytearray(payload_bytes)
    for i in range(payload_bytes):
        # smooth sine wave so we get visible variation regardless of how the
        # firmware interprets the bytes
        phase = i / payload_bytes
        stream[i] = int(127 + 127 * math.sin(phase * 2 * math.pi * 8))

    for seg in range(1, TOTAL + 1):
        pkt = build_pkt(db, 0x2C, seg, 0x00, 0x23)
        # byte[5] stays 0x00 (rekordbox does not use a marker on xx 2C)
        start = (seg - 1) * SEG_PAYLOAD
        for j in range(SEG_PAYLOAD):
            pkt[6 + j] = stream[start + j]
        send_pkt(ep_out, pkt)
    print(f"Overview (xx 2C): sent {TOTAL} packets to deck {deck}")


# ---------------------------------------------------------------------------
# xx 2E PWV5 color waveform
# ---------------------------------------------------------------------------

PWV5_BIG_ENDIAN = False     # set by CLI flag; default LE based on Serato byte inspection

# Per-deck cache file to load real waveform bytes from (set via CLI).
# When None for a deck, _generate_pwv5_entries falls back to a test pattern.
_WAVE_FILES = {1: None, 2: None, 3: None, 4: None}


def encode_pwv5(r, g, b, h):
    """PWV5 16-bit entry. r,g,b ∈ 0..7, h ∈ 0..31. Byte order set by PWV5_BIG_ENDIAN.
    Deep Symmetry docs (anlz.html) say PWV5 in EXPORT FILES is big-endian. On-wire
    to controllers may differ — both are testable via --pwv5-be."""
    v = ((r & 7) << 13) | ((g & 7) << 10) | ((b & 7) << 7) | ((h & 0x1F) << 2)
    if PWV5_BIG_ENDIAN:
        return (v >> 8) & 0xFF, v & 0xFF
    return v & 0xFF, (v >> 8) & 0xFF


def _generate_pwv5_entries(duration_sec, solid=False):
    """Returns a flat bytearray of PWV5-encoded waveform entries.
    If solid=True, every entry is max-height, max-red — guaranteed visible if
    the firmware renders ANY of our data."""
    n = int(duration_sec * ENTRIES_PER_SEC)
    out = bytearray(2 * n)
    for i in range(n):
        if solid:
            r, g, b, h = 7, 0, 0, 31   # full red, full height — unmistakable
        else:
            t   = i / ENTRIES_PER_SEC
            env = 0.3 + 0.7 * abs(((i // 75) % 4 - 1.5) / 1.5)
            if (t % 1.0) < 0.05:
                h = int(31 * env); r, g, b = 7, 2, 1
            elif (t % 0.5) < 0.04:
                h = int(22 * env); r, g, b = 2, 7, 2
            elif (t % 2.0) < 0.15:
                h = int(18 * env); r, g, b = 2, 3, 7
            else:
                h = int(8 * env);  r, g, b = 3, 3, 3
        b0, b1 = encode_pwv5(r, g, b, h)
        out[2*i] = b0
        out[2*i + 1] = b1
    return out


def upload_waveform(ep_out, deck, duration_sec):
    """xx 2E PWV5 color upload.

    seg 1 sub 0: bytes[6..9] = 4-byte track header (duration BE24 + 0x00),
                 bytes[10..127] = 59 PWV5 entries (118 bytes)
    other segs:  bytes[6..127] = 61 PWV5 entries (122 bytes)
    """
    db = DECK_BYTES[deck]
    entries_bytes = _generate_pwv5_entries(duration_sec)
    n_entries = len(entries_bytes) // 2

    # 4-byte header for first packet: duration BE24 + 0x00.
    # Rekordbox captured: `12 e3 00 00`.  0x12e3 = 4835.  Not obviously duration
    # in seconds or ms, may be a track hash.  We'll try BE24 of total entries.
    h0 = (n_entries >> 16) & 0xFF
    h1 = (n_entries >> 8) & 0xFF
    h2 =  n_entries        & 0xFF
    track_header = bytes([h0, h1, h2, 0x00])

    pos = 0          # index into entries_bytes
    seg = 1
    sub = 0
    total_packets = 0

    # ----- First packet (seg 1, sub 0): 4-byte header + 118 bytes entries
    pkt = build_pkt(db, 0x2E, seg, sub, 0xB9)
    pkt[5] = 0x03                              # color marker
    pkt[6:10] = track_header                   # 4-byte track header
    take = min(118, len(entries_bytes) - pos)
    pkt[10:10 + take] = entries_bytes[pos:pos + take]
    send_pkt(ep_out, pkt)
    pos += take
    seg += 1
    total_packets += 1

    # ----- Remaining packets: byte[5]=0x03, bytes[6..127] = 122 bytes entries
    while pos < len(entries_bytes):
        pkt = build_pkt(db, 0x2E, seg, sub, 0xB9)
        pkt[5] = 0x03
        take = min(122, len(entries_bytes) - pos)
        pkt[6:6 + take] = entries_bytes[pos:pos + take]
        send_pkt(ep_out, pkt)
        pos += take
        seg += 1
        total_packets += 1
        if seg > 255:
            seg = 1
            sub += 1

    print(f"Waveform (xx 2E PWV5): sent {total_packets} packets, "
          f"{n_entries} entries ({duration_sec:.1f}s) to deck {deck}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def find_flx10_alsa_card():
    """Return the ALSA card number for the DDJ-FLX10 (e.g. 4), or None."""
    try:
        out = subprocess.check_output(["aplay", "-l"], text=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"^card (\d+): DDJFLX10", out, re.MULTILINE)
    return int(m.group(1)) if m else None


def start_silent_audio(card_num):
    """Pump silence to the FLX10 EP1 OUT in the background.  The hypothesis is
    that the screen-rendering pipeline is gated on active audio streaming.
    Format from /proc/asound/card{N}/stream0: S24_3LE, 4ch, 44100 Hz.
    """
    cmd = ["aplay", "-q", "-D", f"hw:{card_num},0",
           "-f", "S24_3LE", "-c", "4", "-r", "44100",
           "/dev/zero"]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deck",     type=int, default=2, choices=[1, 2, 3, 4])
    ap.add_argument("--duration", type=float, default=30.0,
                    help="Seconds of test waveform (default: 30)")
    ap.add_argument("--warmup",   type=float, default=2.0,
                    help="Seconds of heartbeats before sending metadata (default: 2.0)")
    ap.add_argument("--hold",     type=float, default=10.0,
                    help="Seconds to keep heartbeats+metadata running after upload (default: 10)")
    ap.add_argument("--playing",  action="store_true", default=True,
                    help="Send metadata in playing state (default: True)")
    ap.add_argument("--no-audio", action="store_true",
                    help="Skip the background aplay /dev/zero audio stream. "
                         "By default we pump silence to EP1 because rekordbox's full-init capture "
                         "shows audio starts ~50s before any screen write — likely a precondition.")
    ap.add_argument("--no-sysex", action="store_true",
                    help="Skip the MIDI SysEx layer entirely.")
    ap.add_argument("--no-sysex-enter", action="store_true",
                    help="Skip just the one-shot `03 01` SysEx (the Serato/VirtualDJ enter-HID candidate). "
                         "Keepalive polling still runs unless --no-sysex is also set.")
    ap.add_argument("--sysex-flavor", choices=["rekordbox", "serato"], default="rekordbox",
                    help="Which keepalive variant to poll: rekordbox=50 00, serato=50 31. "
                         "Default rekordbox. (Note: 50 31 is what made the LCDs light up on 2026-05-23.)")
    ap.add_argument("--protocol-flavor", choices=["rekordbox", "serato"], default="rekordbox",
                    help="Which EP5 screen protocol to drive: "
                         "rekordbox=xx 21 metadata + xx 2C overview + xx 2E PWV5 waveform + xx 3D heartbeat; "
                         "serato=xx 27 state + xx 33 album art + xx 36 PWV5 waveform. "
                         "Match this to --sysex-flavor for the cleanest test.")
    ap.add_argument("--no-album-art", action="store_true",
                    help="Skip the xx 33 album-art JPEG upload in Serato flavor.")
    ap.add_argument("--no-init30", action="store_true",
                    help="Skip the xx 30 per-deck one-shot init (the suspected "
                         "display-enable command). Control test for whether xx 30 matters.")
    ap.add_argument("--no-pad-label", action="store_true",
                    help="Skip the xx 39 pad-label upload (HOT CUE text).")
    ap.add_argument("--no-unlock", action="store_true",
                    help="Skip the built-in vendor unlock + snd-usb-audio rebind. "
                         "By default this script does the same handshake as flx10_unlock_v2.py "
                         "before claiming interface 5.")
    ap.add_argument("--pwv5-be", action="store_true",
                    help="Encode PWV5 entries big-endian (per Deep Symmetry docs). "
                         "Default is little-endian based on Serato capture content.")
    ap.add_argument("--solid-waveform", action="store_true",
                    help="Send max-red, max-height entries throughout — guarantees the "
                         "result is visually obvious if the firmware renders ANY xx 36 data.")
    ap.add_argument("--wave-file", action="append", default=[],
                    help="Load real waveform bytes for a deck from a .pwv5 cache file produced "
                         "by flx10_prerender_waveform.py. Repeat for multiple decks. "
                         "Format: 'DECK:PATH', e.g. --wave-file 2:/home/me/.flx10-cache/1015.pwv5")
    ap.add_argument("--album-art-size", type=int, default=240,
                    help="Pixel size of the test album-art JPEG (default 240). "
                         "Larger sizes produce bigger JPEGs closer to Serato's real album art.")
    args = ap.parse_args()
    global SYSEX_PING, PWV5_BIG_ENDIAN, _WAVE_FILES
    if args.sysex_flavor == "serato":
        SYSEX_PING = "F0 00 40 05 00 00 04 01 00 50 31 F7"
    PWV5_BIG_ENDIAN = args.pwv5_be
    for spec in args.wave_file:
        if ":" not in spec:
            sys.exit(f"--wave-file expects 'DECK:PATH', got: {spec}")
        d, p = spec.split(":", 1)
        d = int(d)
        if d not in (1, 2, 3, 4):
            sys.exit(f"--wave-file deck must be 1..4, got: {d}")
        if not os.path.exists(p):
            sys.exit(f"--wave-file path does not exist: {p}")
        _WAVE_FILES[d] = p

    dev = find_device()

    # ===== Vendor unlock — done by Pioneer/AlphaTheta driver in all captures
    if not args.no_unlock:
        if os.geteuid() != 0:
            print("WARNING: not running as root — snd-usb-audio rebind will fail. Continuing anyway.")
        maybe_rebind_audio()
        vendor_unlock(dev)
        # After vendor_unlock the device may have re-enumerated. Re-find.
        usb.util.dispose_resources(dev)
        time.sleep(0.3)
        dev = find_device()
        maybe_rebind_audio()
        time.sleep(0.3)

    detached = False
    try:
        dev.detach_kernel_driver(SCREEN_INTERFACE)
        detached = True
        print(f"Detached kernel HID driver from interface {SCREEN_INTERFACE}")
    except usb.core.USBError as e:
        if e.errno == 61:
            print(f"No kernel driver on interface {SCREEN_INTERFACE} (OK)")
        else:
            print(f"detach_kernel_driver warning: {e}")

    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        if e.errno not in (16, 6):
            raise

    print(f"Claiming interface {SCREEN_INTERFACE} …")
    try:
        usb.util.claim_interface(dev, SCREEN_INTERFACE)
    except usb.core.USBError as e:
        sys.exit(f"\nERROR: Cannot claim interface {SCREEN_INTERFACE}: {e}\n"
                 "Make sure Mixxx and any other FLX10 software is fully closed.")

    ep_out, ep_in = get_screen_endpoints(dev)
    hb = None
    audio_proc = None
    sysex = None

    # ===== Phase 0a: MIDI SysEx handshake ===================================
    # Two layers (controlled separately by --no-sysex-enter and --no-sysex):
    #   1. ONE-SHOT `03 01` — sent by Serato AND VirtualDJ before any screen
    #      writes.  Missing from rekordbox capture.  Candidate "enter HID
    #      screen mode" magic.
    #   2. KEEPALIVE polling of `50 00` (rekordbox flavor) or `50 31` (Serato
    #      flavor) every 200-250 ms.
    if not args.no_sysex:
        midi_port = find_flx10_midi_port()
        if midi_port is None:
            print("WARNING: FLX10 MIDI port not found in `amidi -l`, skipping SysEx layer")
        else:
            if not args.no_sysex_enter:
                print(f"\nPhase 0a1: one-shot SysEx 03 01 on {midi_port} (enter-HID candidate)")
                try:
                    subprocess.run(["amidi", "-p", midi_port, "-S", SYSEX_ENTER_HID],
                                   check=False, timeout=1,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (OSError, subprocess.TimeoutExpired) as e:
                    print(f"  SysEx 03 01 send failed: {e}")
                time.sleep(0.1)
            print(f"\nPhase 0a2: SysEx keepalive on {midi_port} every 200 ms …")
            sysex = SysExPingThread(midi_port)
            sysex.start()
            time.sleep(1.2)   # 5+ pings, matching rekordbox's pre-heartbeat warm-up

    # ===== Phase 0b: start silent audio streaming on EP1 ====================
    if not args.no_audio:
        card = find_flx10_alsa_card()
        if card is None:
            print("WARNING: FLX10 not in `aplay -l`, skipping audio stream")
        else:
            print(f"\nPhase 0b: streaming silence to hw:{card},0 (EP1 OUT) …")
            try:
                audio_proc = start_silent_audio(card)
                time.sleep(0.5)
                if audio_proc.poll() is not None:
                    print(f"  aplay died immediately (exit {audio_proc.returncode}); continuing without audio")
                    audio_proc = None
                else:
                    print("  audio is streaming (pid {})".format(audio_proc.pid))
            except OSError as e:
                print(f"  could not start aplay: {e}; continuing without audio")

    try:
        if args.protocol_flavor == "rekordbox":
            # ===== Phase 1: heartbeats only (rekordbox warm-up) =================
            print(f"\nPhase 1: rekordbox heartbeat warm-up ({args.warmup:.1f}s) …")
            hb = HeartbeatThread(ep_out)
            hb.start()
            time.sleep(args.warmup)
            drain_acks(ep_in, count=10, label="post-warmup")

            # ===== Phase 2: metadata burst =====
            print(f"\nPhase 2: xx 21 metadata burst (deck {args.deck} loaded, "
                  f"{'playing' if args.playing else 'stopped'}) …")
            metadata_burst(ep_out, loaded_deck=args.deck, cycles=20, playing=args.playing)
            drain_acks(ep_in, count=10, label="post-metadata")

            # ===== Phase 3: overview upload =====
            print(f"\nPhase 3: xx 2C overview upload to deck {args.deck} …")
            upload_overview(ep_out, args.deck)
            drain_acks(ep_in, count=10, label="post-overview")

            # ===== Phase 4: waveform upload =====
            print(f"\nPhase 4: xx 2E PWV5 waveform upload, {args.duration:.1f}s …")
            upload_waveform(ep_out, args.deck, args.duration)
            drain_acks(ep_in, count=30, timeout_ms=200, label="post-waveform")

            # ===== Phase 5: sustained metadata + heartbeats =====
            print(f"\nPhase 5: sustained xx 21 + xx 3D for {args.hold:.1f}s …")
            print("        Watch the jog wheel — waveform should appear if rendering works.")
            end_t = time.time() + args.hold
            while time.time() < end_t:
                for d in range(1, 5):
                    send_metadata(ep_out, d,
                                  track_loaded=(d == args.deck),
                                  playing=(args.playing and d == args.deck))
                time.sleep(0.02)
        else:
            # ----- Serato protocol -------------------------------------------
            # No xx 3D heartbeat, no xx 2C overview — Serato sends neither.
            print(f"\nPhase 1: warm-up (no heartbeat for Serato flavor, {args.warmup:.1f}s) …")
            time.sleep(args.warmup)
            drain_acks(ep_in, count=10, label="post-warmup")

            # ===== Phase 2a: initial xx 27 state ping (single round) =====
            print(f"\nPhase 2a: initial xx 27 state ping to all decks …")
            for d in range(1, 5):
                send_serato_state(ep_out, d, track_loaded=(d == args.deck))
            time.sleep(0.05)

            # ===== Phase 2b: per-deck xx 30 one-shot init ("enable display") =====
            # CANDIDATE GATE: this is the one-shot we suspect activates the
            # waveform display element for the deck.
            if not args.no_init30:
                print(f"\nPhase 2b: xx 30 one-shot per-deck init (suspected display-enable) …")
                for d in range(1, 5):
                    send_serato_init30(ep_out, d)
                time.sleep(0.05)

            # ===== Phase 2c: pad-mode label xx 39 ("HOT CUE") per deck =====
            if not args.no_pad_label:
                print(f"\nPhase 2c: xx 39 pad-mode label ('HOT CUE') per deck …")
                for d in range(1, 5):
                    send_serato_init39_hotcue(ep_out, d)
                time.sleep(0.05)

            # ===== Phase 2d: sustained xx 27 state burst =====
            print(f"\nPhase 2d: xx 27 state burst (deck {args.deck} loaded) …")
            serato_state_burst(ep_out, loaded_deck=args.deck, cycles=20)
            drain_acks(ep_in, count=10, label="post-state")

            # ===== Phase 2.5: Serato xx 33 album-art upload (ALL decks) =====
            if not args.no_album_art:
                art = _generate_test_jpeg(size_px=args.album_art_size)
                print(f"\nPhase 2.5: xx 33 album-art ({args.album_art_size}×{args.album_art_size} "
                      f"red JPEG, {len(art)} bytes) upload to ALL 4 decks …")
                for d in range(1, 5):
                    upload_album_art(ep_out, d, art)
                drain_acks(ep_in, count=10, label="post-album-art")

            # ===== Phase 2.7: xx 35 — 3 packets per deck (begin-waveform?) =====
            print(f"\nPhase 2.7: xx 35 ('begin waveform' candidate) — 3 packets per deck …")
            for d in range(1, 5):
                send_serato_init35(ep_out, d)
            drain_acks(ep_in, count=10, label="post-init35")

            # ===== Phase 3: Serato xx 36 waveform upload (ALL decks) =====
            endian = "BE" if PWV5_BIG_ENDIAN else "LE"
            pattern = "SOLID red max-height" if args.solid_waveform else "varied test pattern"
            print(f"\nPhase 3: xx 36 PWV5/{endian} {pattern} upload to ALL 4 decks, {args.duration:.1f}s each …")
            for d in range(1, 5):
                upload_serato_waveform(ep_out, d, args.duration, solid=args.solid_waveform)
            drain_acks(ep_in, count=30, timeout_ms=200, label="post-waveform")

            # ===== Phase 3.5: xx 2f — cue-data placeholder per deck =====
            print(f"\nPhase 3.5: xx 2f cue-data placeholder per deck …")
            for d in range(1, 5):
                send_serato_init2f(ep_out, d)
            drain_acks(ep_in, count=5, label="post-init2f")

            # ===== Phase 4: sustained state =====
            print(f"\nPhase 4: sustained xx 27 state pings for {args.hold:.1f}s …")
            print("        Watch the jog wheel — waveform should appear if rendering works.")
            end_t = time.time() + args.hold
            while time.time() < end_t:
                for d in range(1, 5):
                    send_serato_state(ep_out, d, track_loaded=(d == args.deck))
                time.sleep(0.02)

        print("\n——— Done — protocol stopping ———")

    finally:
        if hb is not None:
            hb.stop()
            hb.join(timeout=1)
        if sysex is not None:
            sysex.stop()
            sysex.join(timeout=1)
            print(f"SysEx pings sent: {sysex.sent} (errors: {sysex.errors})")
        if audio_proc is not None:
            audio_proc.terminate()
            try:
                audio_proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                audio_proc.kill()
            print("Stopped background audio stream.")
        print("Releasing interface …")
        try:
            usb.util.release_interface(dev, SCREEN_INTERFACE)
        except Exception as e:
            print(f"  release_interface: {e}")
        if detached:
            try:
                dev.attach_kernel_driver(SCREEN_INTERFACE)
                print("Kernel HID driver re-attached.")
            except usb.core.USBError as e:
                print(f"Could not re-attach kernel driver: {e}")
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    main()
