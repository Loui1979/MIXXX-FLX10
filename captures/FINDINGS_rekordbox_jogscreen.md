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

## DECODED (2026-09-17) — 0x37 + 0x38 payload encoding

Full decode of the two waveform families, verified against
`rb_jogscreen_full_20260916.pcapng` (deck 1, b0=0x10). Decision method:
adjacent-column smoothness — stride 3 gives mean delta **4.3**, stride 2/4 give
**~24**, so the payload is unambiguously **3-byte columns** for BOTH types.

### Shared 6-byte ep5 header (0x37 and 0x38 identical)

| off | bytes | meaning |
|-----|-------|---------|
| 0   | `10`  | deck (0x10/20/30/40 = deck 1..4) |
| 1   | `37`/`38` | packet type |
| 2:4 | LE16  | **packet sequence**, 1-based |
| 4:6 | LE16  | **total packet count** of this burst |
| 6:128 | 122B | payload (ALL 122 bytes are data) |

Verified: 0x37 total field = `1e 00` = 30 = the 30 0x37 packets; 0x38 total
field = `61 02` = 609 = the 609 0x38 packets. 0x38 seq is a true 16-bit counter
(lo=byte2 wraps 0-255, hi=byte3 increments: 1..255, 256..511, 512..609).
(The earlier note that 0x61 was a payload length was wrong — it's the count LSB.)

### Payload = continuous 3-byte column stream

Columns STRADDLE packet boundaries — concatenate every packet's `[6:]` (in seq
order) to rebuild one flat stream, then slice 3 bytes per column. Each column =
three frequency-band amplitudes. Band→lane order inferred from temporal stats
of the capture (no audio ground truth, so this is the one hardware-tunable):

- **lane1 = LOW**  — smoothest (lag-1 Δ 2.35), 1% near-silent, sustained bass
- **lane2 = MID**  — intermediate (lag-1 Δ 4.27)
- **lane0 = HIGH** — spiky (lag-1 Δ 6.30), 12% near-silent, biggest transients

So `column = [HIGH, LOW, MID]`. Values ran 0..~0xB0 (Mixxx bands are 0..255).
If on-screen colours look wrong, permute this triplet (one A/B pass).

### 0x37 = low-res full-track OVERVIEW

30 packets → ~**1186 columns**. Packet #1 payload begins with a 4-byte
sub-header `80 01 01 00` (constant; replay verbatim), then columns.

### 0x38 = high-res full-track WAVEFORM

609 packets → **24766 columns**. This is a **load-time bulk upload**, NOT a live
per-frame feed. `24766 cols / ~165 s = 150 cols/s` — the same rate Veezuhz used
for PWV5. The firmware scrolls locally off the playhead from `0x21`/`xx27`
(already driven by the daemon's StatePingThread) — we do not stream 0x38 per
tick; we upload it once per track load.

### Feeding it from Mixxx — `HID/flx10_rb_waveform.py`

Import-safe encoder (stdlib only). `build_screen_upload(deck, analysis_path,
duration_sec)` → `(overview_pkts, full_pkts)`, each a list of raw 128-byte ep5
packets. It max-pools Mixxx's `(low,mid,high)` bands (4 vals/visual-frame, the
same `parse_mixxx_waveform` the daemon uses) into `[HIGH,LOW,MID]` columns at
`FULL_FPS=150` (0x38) and `/OVERVIEW_DECIM=21` (0x37), then frames them with the
header above. Offline `--selftest` reproduces the capture byte-for-frame: 609
0x38 packets / 30 0x37 packets / correct seq+total / the `80 01 01 00`
sub-header / 24766 reconstructed columns.

**Wired in `flx10_relay_daemon.py` (default ON).** Track-load skips PWV5 and
calls `build_screen_upload`; long tracks clip at 24500 cols; no Serato `0x3d`
after the RB upload. Opt out with `RUN_REKORDBOX_WAVE=0`. Tunables:
`BAND_ORDER`, `BAND_GAIN`, `FULL_FPS`, `OVERVIEW_DECIM`. Paint+scroll still
UNVERIFIED on hardware — power-cycle first. If the wave sits still, decode
`0x21` next (not BAND_ORDER).

## What already works (verified on hardware this session)

- ep0 unlock with all kernel drivers still bound (no detach needed).
- 323-frame SysEx handshake + keepalive over native ALSA MIDI → device leaves
  the "rekordbox" splash and enters live HID display mode.
- hidraw writes (0x00 prefix) land with zero errors; `xx27` renders BPM + a
  counting/position playhead. Only the waveform bitmap was missing — because
  wrong packet family (Serato vs rekordbox), now identified.
