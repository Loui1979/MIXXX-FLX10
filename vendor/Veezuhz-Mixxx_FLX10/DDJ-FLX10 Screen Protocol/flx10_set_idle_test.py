#!/usr/bin/env python3
"""
flx10_set_idle_test.py — test whether SET_IDLE to interface 5 unlocks the FLX10 screen.

Background
----------
The pcapng capture of a Windows VM running VirtualDJ shows the Windows HID class
driver sending SET_IDLE (bmRequestType=0x21, bRequest=0x0A, wValue=0x0000, wIndex=5)
to interface 5 (the jog wheel screen interface) before any EP5 OUT screen writes.
Linux's hidraw stack never sends SET_IDLE automatically. This script tests whether
sending it manually is the gate that allows the screen to render.

The script:
  1. Detaches the kernel HID driver from interface 5
  2. Optionally sends audio-class cycling (RANGE/CUR/SET_INTERFACE) — disabled by default
  3. Sends SET_IDLE to interface 5
  4. Sends xx 21 metadata warm-up (same as VirtualDJ's initial burst)
  5. Uploads overview (xx 37) + test waveform (xx 38) for the target deck
  6. Reads and prints any ACK replies
  7. Re-attaches the kernel HID driver

Usage
-----
  sudo python3 flx10_set_idle_test.py --deck 2 --playing   # best test: playing state + deck 2
  sudo python3 flx10_set_idle_test.py --deck 2             # stopped state baseline
  sudo python3 flx10_set_idle_test.py --no-set-idle --deck 2 --playing  # skip SET_IDLE
  sudo python3 flx10_set_idle_test.py --audio-cycle        # also do audio cycling

IMPORTANT: Close Mixxx (or any software using the FLX10) before running.
"""

import argparse
import math
import struct
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("pyusb not found.  pip install pyusb")

VID = 0x2B73
PID = 0x0041
SCREEN_INTERFACE = 5

DECK_BYTES       = {1: 0x10, 2: 0x20, 3: 0x30, 4: 0x40}
ENTRIES_PER_SEC  = 150
SEG_PAYLOAD      = 122    # bytes[6..127] in a 128-byte packet
OVERVIEW_ENTRIES = 600


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def find_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("DDJ-FLX10 not found (VID 2B73 / PID 0041). Is it plugged in?")
    print(f"Found DDJ-FLX10 — bus {dev.bus}, device {dev.address}")
    return dev


def get_screen_endpoints(dev):
    cfg  = dev.get_active_configuration()
    intf = cfg[(SCREEN_INTERFACE, 0)]
    ep_out = usb.util.find_descriptor(
        intf,
        custom_match=lambda e: (
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR
        )
    )
    ep_in = usb.util.find_descriptor(
        intf,
        custom_match=lambda e: (
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_INTR
        )
    )
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


def send_pkt(ep_out, pkt):
    """Write one 128-byte packet directly to EP5 OUT via pyusb (no Report ID prefix)."""
    ep_out.write(bytes(pkt), timeout=1000)


def drain_acks(ep_in, count=10, timeout_ms=50, label=""):
    """Read up to `count` ACK replies from EP4 IN, print them."""
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
# Protocol: xx 21 metadata
# ---------------------------------------------------------------------------
#
# Decoded from VirtualDJ capture (flx10-virtualdj-waveform.pcapng).
# The packet is NOT all zeros — specific bytes signal track state.
# Sending all zeros tells the firmware "no track, nothing to show."
#
# Constant bytes (same across all packets in capture):
#   [3]  = 0x0a
#   [10] = 0x0e
#   [27] = 0x80
#   [58] = 0x03
#   [59] = number of xx 37 overview segments (0x1e = 30 for 600-entry overview)
#   [61] = 0x0d
#
# Track-state bytes:
#   [7]     = 0x02 (track loaded) | 0x01 (no track)
#   [21-24] = 0x75 0x40 0x2a 0xff (loaded) | 0x78 0x00 0x00 0x00 (no track)
#
# Playback-state bytes (stopped → playing):
#   [4]  = 0x04 → 0x0c
#   [5]  = 0x01 → 0x81
#   [49] = 0x30 → 0x20

_META_OVERVIEW_SEGS = OVERVIEW_ENTRIES * 6 // SEG_PAYLOAD + (1 if (OVERVIEW_ENTRIES * 6 % SEG_PAYLOAD) else 0)

def _make_metadata_pkt(deck_byte, track_loaded=True, playing=False):
    pkt = bytearray(128)
    pkt[0]  = deck_byte
    pkt[1]  = 0x21
    pkt[2]  = 0x00
    pkt[3]  = 0x0a
    # byte[4]/[5] reflect playback state (from VirtualDJ capture):
    #   stopped: 0x04, 0x01   playing: 0x0c, 0x81
    pkt[4]  = 0x0c if playing else 0x04
    pkt[5]  = 0x81 if playing else 0x01
    pkt[7]  = 0x02 if track_loaded else 0x01
    pkt[10] = 0x0e
    if track_loaded:
        pkt[21] = 0x75; pkt[22] = 0x40; pkt[23] = 0x2a; pkt[24] = 0xff
        pkt[49] = 0x20 if playing else 0x30
    else:
        pkt[21] = 0x78
    pkt[27] = 0x80
    pkt[58] = 0x03
    pkt[59] = _META_OVERVIEW_SEGS & 0xFF   # 30 for 600-entry overview
    pkt[61] = 0x0d
    return pkt


def send_metadata(ep_out, deck, track_loaded=True, playing=False):
    db = DECK_BYTES.get(deck)
    if not db:
        return
    send_pkt(ep_out, _make_metadata_pkt(db, track_loaded, playing))


def metadata_warmup(ep_out, loaded_deck, cycles=20, interval_s=0.02, playing=False):
    """Replicate VirtualDJ's pre-waveform metadata burst (all 4 decks at ~50 Hz)."""
    state = "playing" if playing else "stopped"
    print(f"Sending {cycles} metadata warm-up cycles (deck {loaded_deck} = loaded/{state}) …")
    for _ in range(cycles):
        for d in range(1, 5):
            send_metadata(ep_out, d, track_loaded=(d == loaded_deck), playing=(playing and d == loaded_deck))
        time.sleep(interval_s)


def metadata_sustain(ep_out, loaded_deck, duration_s=3.0, interval_s=0.02, playing=True):
    """Keep firing xx 21 at 50 Hz for duration_s — lets firmware settle after upload."""
    cycles = int(duration_s / interval_s)
    state = "playing" if playing else "stopped"
    print(f"Sustaining metadata ({state}) for {duration_s:.1f}s ({cycles} cycles) …")
    for _ in range(cycles):
        for d in range(1, 5):
            send_metadata(ep_out, d, track_loaded=(d == loaded_deck), playing=(playing and d == loaded_deck))
        time.sleep(interval_s)


# ---------------------------------------------------------------------------
# Protocol: xx 37 overview + xx 38 waveform
# ---------------------------------------------------------------------------

def upload_overview(ep_out, deck):
    # xx 37 format (from VirtualDJ capture analysis):
    #   byte[5] = 0x00  (no marker byte — different from xx 38)
    #   payload = 122 raw 3-byte entries starting at byte[6], NO 4-byte header
    #   NO entry duplication (each [lo,md,hi] sent once)
    #   30 packets × 122 bytes = 3660 bytes = 1220 entries per upload
    db = DECK_BYTES[deck]
    n  = 30 * SEG_PAYLOAD // 3   # = 1220 entries
    stream = []
    for i in range(n):
        phase = i / n
        lo = int(180 * (0.5 + 0.5 * math.sin(phase * 6.28 * 8)))
        md = int(100 * (0.5 + 0.5 * math.sin(phase * 6.28 * 12 + 1)))
        hi = int(60  * (0.5 + 0.5 * math.sin(phase * 6.28 * 16 + 2)))
        stream += [lo, md, hi]   # no duplication

    pos, seg = 0, 1
    while pos < len(stream):
        pkt = build_pkt(db, 0x37, seg, 0x00, 0x1E)
        # byte[5] stays 0x00 — no marker for xx 37
        for j in range(SEG_PAYLOAD):
            pkt[6 + j] = stream[pos + j] if (pos + j) < len(stream) else 0
        send_pkt(ep_out, pkt)
        pos += SEG_PAYLOAD
        seg += 1
    print(f"Overview (xx 37): sent {seg - 1} packets for deck {deck} ({n} entries, no dup)")


def _generate_test_entries(duration_sec):
    n = int(duration_sec * ENTRIES_PER_SEC)
    entries = []
    for i in range(n):
        t   = i / ENTRIES_PER_SEC
        env = 0.3 + 0.7 * abs(((i // 75) % 4 - 1.5) / 1.5)
        low  = min(255, int(((t % 1.0) < 0.05 and 255 or 30) * env))
        mid  = min(255, int(((t % 0.5) < 0.04 and 200 or 40) * env))
        high = min(255, int(((t % 2.0) < 0.15 and 180 or 50) * env))
        entries.append((low, mid, high))
    return entries


def upload_waveform(ep_out, deck, entries):
    db  = DECK_BYTES[deck]
    n   = len(entries)
    stream = [n & 0xFF, (n >> 8) & 0xFF, 0x00, 0x00]
    for lo, md, hi in entries:
        stream += [lo & 0xFF, md & 0xFF, hi & 0xFF,
                   lo & 0xFF, md & 0xFF, hi & 0xFF]

    SEGS_PER_SF = 255
    MAX_BYTES   = SEGS_PER_SF * SEG_PAYLOAD   # 255 * 122 = 31110

    pos, subframe, total = 0, 0, 0
    while pos < len(stream) and subframe < 2:
        sf_end = min(pos + MAX_BYTES, len(stream))
        seg = 1
        while pos < sf_end:
            pkt      = build_pkt(db, 0x38, seg, subframe, 0xD9)
            pkt[5]   = 0x01
            for j in range(SEG_PAYLOAD):
                pkt[6 + j] = stream[pos + j] if (pos + j) < sf_end else 0
            send_pkt(ep_out, pkt)
            pos  += SEG_PAYLOAD
            seg  += 1
            total += 1
            if seg > 255:
                break
        subframe += 1
    print(f"Waveform (xx 38): sent {total} packets for deck {deck} ({n} entries)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deck",         type=int, default=1, choices=[1, 2, 3, 4])
    ap.add_argument("--no-set-idle",  action="store_true",
                    help="Skip SET_IDLE — use as a baseline to confirm rendering is blocked")
    ap.add_argument("--audio-cycle",  action="store_true",
                    help="Also send audio-class cycling (RANGE/CUR/SET_INTERFACE) before SET_IDLE")
    ap.add_argument("--duration",     type=float, default=10.0,
                    help="Seconds of test pattern waveform to upload (default: 10)")
    ap.add_argument("--playing",      action="store_true",
                    help="Send playing-state metadata (byte[4]=0x0c, byte[5]=0x81) — as seen in VirtualDJ")
    args = ap.parse_args()

    dev    = find_device()
    detached = False

    # Detach kernel HID driver unconditionally — is_kernel_driver_active() misses
    # hid-generic on newer kernels (it shows False even while holding the interface).
    try:
        dev.detach_kernel_driver(SCREEN_INTERFACE)
        detached = True
        print(f"Detached kernel HID driver from interface {SCREEN_INTERFACE}")
    except usb.core.USBError as e:
        # errno 61 = ENODATA: no kernel driver attached — that's fine.
        if e.errno == 61:
            print(f"No kernel driver on interface {SCREEN_INTERFACE} (OK)")
        else:
            print(f"detach_kernel_driver warning (errno {e.errno}): {e}")

    # Set configuration (may already be set; EBUSY is harmless)
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        if e.errno not in (16, 6):    # EBUSY or EPIPE — already configured
            raise

    print(f"Claiming interface {SCREEN_INTERFACE} …")
    try:
        usb.util.claim_interface(dev, SCREEN_INTERFACE)
    except usb.core.USBError as e:
        print(f"\nERROR: Cannot claim interface {SCREEN_INTERFACE}: {e}")
        print("Make sure Mixxx and any other FLX10 software is fully closed.")
        print("Check with:  fuser /dev/hidraw* 2>/dev/null")
        sys.exit(1)

    ep_out, ep_in = get_screen_endpoints(dev)

    try:
        # ---- Optional: audio class cycling --------------------------------
        if args.audio_cycle:
            print("Audio-class cycling (RANGE / CUR / SET_INTERFACE) …")
            # GET RANGE CS_SAM_FREQ_CONTROL on clock entity (wIndex=0x0100)
            try:
                resp = dev.ctrl_transfer(0xA1, 0x03, 0x0100, 0x0100, 14, timeout=200)
                print(f"  RANGE CS_SAM_FREQ: {bytes(resp).hex()}")
            except usb.core.USBError as e:
                print(f"  RANGE CS_SAM_FREQ: error {e} (may be OK)")
            # GET CUR CS_CLOCK_VALID_CONTROL (wValue=0x0200)
            try:
                resp = dev.ctrl_transfer(0xA1, 0x01, 0x0200, 0x0100, 1, timeout=200)
                print(f"  CUR CS_CLOCK_VALID: {bytes(resp).hex()}")
            except usb.core.USBError as e:
                print(f"  CUR CS_CLOCK_VALID: error {e} (may be OK)")

        # ---- Core test: SET_IDLE to interface 5 ----------------------------
        if not args.no_set_idle:
            print(f"Sending SET_IDLE to interface {SCREEN_INTERFACE} …")
            # bmRequestType = 0x21: Host→Device, Class, Interface
            # bRequest      = 0x0A: SET_IDLE
            # wValue        = 0x0000: duration=0 (idle forever), report ID=0 (all)
            # wIndex        = 5 (interface 5)
            # wLength       = 0
            try:
                result = dev.ctrl_transfer(0x21, 0x0A, 0x0000, SCREEN_INTERFACE, None,
                                           timeout=500)
                print(f"  SET_IDLE OK (returned {result})")
            except usb.core.USBError as e:
                print(f"  SET_IDLE error: {e}")
                print("  (EPIPE / STALL here means the device rejected it — unusual.)")
        else:
            print("Skipping SET_IDLE (baseline run).")

        # ---- Metadata warm-up (all 4 decks, ~20 cycles at 50 Hz) ----------
        deck = args.deck
        metadata_warmup(ep_out, loaded_deck=deck, playing=args.playing)
        drain_acks(ep_in, count=20, label="post-metadata")

        # ---- Overview upload (twice, like VirtualDJ) -----------------------
        print(f"\nUploading overview (xx 37) to deck {deck} — first pass …")
        upload_overview(ep_out, deck)
        drain_acks(ep_in, count=10, label="post-overview-1")
        time.sleep(0.05)
        print(f"Uploading overview (xx 37) to deck {deck} — second pass (VirtualDJ sends twice) …")
        upload_overview(ep_out, deck)
        drain_acks(ep_in, count=10, label="post-overview-2")

        # ---- Waveform upload -----------------------------------------------
        print(f"\nUploading test waveform (xx 38) to deck {deck} ({args.duration}s) …")
        entries = _generate_test_entries(args.duration)
        upload_waveform(ep_out, deck, entries)
        drain_acks(ep_in, count=20, timeout_ms=200, label="post-waveform")

        # ---- Sustain metadata at playing state to let firmware settle ------
        metadata_sustain(ep_out, loaded_deck=deck, duration_s=3.0, playing=args.playing)
        drain_acks(ep_in, count=10, label="post-sustain")

        state_flag = "--playing" if args.playing else "(stopped state — try adding --playing)"
        print(f"\n——— Done — {state_flag} ———")
        print("Check the jog wheel — does the waveform ring appear?")

    finally:
        print("\nReleasing interface …")
        usb.util.release_interface(dev, SCREEN_INTERFACE)
        if detached:
            try:
                dev.attach_kernel_driver(SCREEN_INTERFACE)
                print("Kernel HID driver re-attached.")
            except usb.core.USBError as e:
                print(f"Could not re-attach kernel driver: {e}")
                print("You may need to unplug/replug the FLX10.")
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    main()
