# FLX10 jog-screen protocol — rekordbox capture findings (2026-09-16)

Capture: rekordbox in tinywin11 VM driving the FLX10, host usbmon3.
337 MB / 248k+ records. **pcap kept local only (gitignored) — too big for git.**
Working copy: `captures/rb_jogscreen_working_20260916_2225.pcapng`.

## THE headline

**rekordbox draws the jog screen with `b1=0x21` HID-OUT packets on ep5 — NOT
Serato's `0x36`.** Our whole failure to render was speaking the wrong dialect:
we sent Veezuhz's Serato `xx36` to firmware that wants rekordbox `0x21`. The
frame/time/marker drew (that's `xx27`), the waveform silently dropped. The
"firmware auth wall" was a misread — it was just an unknown packet type.

## Critical operational finding: the controller wedges

Earlier captures showed **ep5_OUT = 1 packet** and rekordbox itself would NOT
draw the jog waveform. Cause: the FLX10 gets stuck after repeated
libusb claim / mode-switch / unlock cycling. **A full power-cycle (unplug USB
AND power ~10s) clears it.** After reset, the same rekordbox session produced
**ep5_OUT = 14,544 packets** and the screen drew normally. Always power-cycle
the FLX10 before a capture or a "why won't it draw" debug.

## ep5 packet-type census (post-reset, deck 1 = b0 0x10)

| b0 | b1 | count | meaning (inferred) |
|----|----|-------|--------------------|
| 10 | 21 | 13840 | **main display/waveform stream** (continuous) |
| 10 | 38 | 609   | frequent update — **scrolling waveform column data** (see below) |
| 10 | 37 | 30    | **bulk overview waveform** (byte-pair PWV-like data) |
| 10 | 39 | 22    | hot-cue labels — payload contains ASCII "HOT CUE" |
| 10 | 2f | 12    | beatgrid |
| 10 | 30 | 10    | track length |
| 10 | 2d | 7     | (setup) |
| 10 | 3b | 10    | (setup) |
| 10 | 2b/33/3c/3e | 1 each | one-shot setup packets |
| 30 | 21 | (init)| global init `30 21 00 00 20 01 ...` (already known) |

All packets are 128 bytes, ep5 INT OUT, wMaxPacketSize=128, bInterval=4.
Over hidraw a 0x00 report-ID prefix is required (129-byte write).

## Packet payload notes (from samples)

- **0x21** (main): `10 21 02 0a 10 81 01 02 80 b4 00 00 19 72 03 03 19 29 01 00
  00 90 40 d9 ff 00 00 80 ...` — when idle/paused it repeats byte-identical
  (a steady-state display-state frame). Header carries several LE fields
  (`19 72`, `19 29`, `90 40`) — likely track length / playhead / bpm. NOT yet
  fully decoded; the actual scrolling wave pixels appear to ride on 0x38.
- **0x38** (609): `10 38 SS 00 61 02 ...` where byte2 = sequence (01,02,03,04…).
  Payload is dense **2-byte pairs** (`00 10 / 0b 0d / 00 15 15 …`) = per-column
  waveform samples (height + colour). This is the **live scrolling waveform
  column feed**. Strong candidate for the "make the wave move" packet.
- **0x37** (30): `10 37 SS 00 1e 00 80 01 01 00 05 08 00 08 0c 02 …` byte2 =
  segment (01,02,03…), then dense byte-pairs. 30 packets ≈ a **one-shot full
  overview waveform upload** at track load.
- **0x39**: `10 39 01 00 03 00 00 "HOT CUE" 00…` — hot-cue text label.
- **0x30**: `10 30 01 00 01 00 03 19 29 01 …` — `03 19 29` recurs across 0x21/0x2d
  = likely the **track length in samples/frames** (shared constant this session).

## Inferences / plan

1. rekordbox screen = **0x21 state + 0x37 bulk overview + 0x38 scrolling columns**
   (+ 0x2f grid, 0x39 cues, 0x30 length). Serato `0x36` is a different path.
2. To draw **Mixxx's** waveform in rekordbox mode we must emit the **rekordbox
   family (0x21/0x37/0x38)**, feeding them from Mixxx's analyzed PWV data —
   not Serato `xx36`.
3. Next decode step: pin the 0x37 (bulk) and 0x38 (scroll) payload encoding —
   entries-per-packet, byte-pair meaning (height/colour), and the position/seq
   fields in the 0x21 header — by diffing packets across known playhead moves.
4. Architecture already proven and unchanged: **no interface detach; ep0 vendor
   unlock; native ALSA MIDI for the mapping + SysEx handshake + keepalive;
   /dev/hidraw (0x00-prefixed 128B writes) for the screen.** `xx27` digits
   already render live via this path.

## What already works (verified on hardware this session)

- ep0 unlock with all kernel drivers still bound (no detach needed).
- 323-frame SysEx handshake + keepalive over native ALSA MIDI → device leaves
  the "rekordbox" splash and enters live HID display mode.
- hidraw writes (0x00 prefix) land with zero errors; `xx27` renders BPM + a
  counting/position playhead. Only the waveform bitmap was missing — because
  wrong packet family (Serato vs rekordbox), now identified.
