# DDJ-FLX10 Jog Wheel Screen Protocol — Findings & Open Problem

> **2026-05-29 — "FLASH TO START EVERY SECOND" SOLVED.** The xx 27 sub-second
> field (`b8<<8 | b7`) is **milliseconds (0–999), NOT 1024ths**. We had been
> encoding `floor(sub × 1024)` → 0–1023. For the last ~23 ms of every second the
> field was 1000–1023, which the firmware reads as *more than one full second* of
> sub-second time → it overflows and snaps the wave/time display to the origin
> for one frame: the once-per-second flash.
>
> Forensic proof: in `flx10-serato-longplay.pcapng` the field **maxes at exactly
> 999 over 66k packets and never reaches 1023**. Fix: encode `floor(sub × 1000)`,
> capped at 999 (`PioneerDDJFLX10Screen._buildState`, bytes 7/8).
>
> This corrects the earlier "÷1024" model below — that model was monotonic-
> consistent (so it passed the 51k-transition check) but the max-value test
> (999 vs 1023) disproves it. Symptom cross-checks confirming the millisecond
> model: `b8=0` made the field max 255 (never overflows → no flash, but jumpy);
> pausing froze the display at the origin inside the 1000–1023 band (~17.98 s);
> moving the tempo slider rescaled the field and slid the band off the paused
> position.

**Status (2026-05-24 — SCROLLING WAVEFORMS, IN-SYNC):** Loading any analyzed track in Mixxx automatically renders its colored waveform on the FLX10 jog wheel; the waveform **scrolls in sync with playback and responds to scrubbing on paused decks**. Pipeline:

1. Mixxx's MIDI script (`Pioneer-DDJ-FLX10-scripts.js`) logs `FLX10_TRACK_LOAD deck=N samples=X file_bpm=Y duration=Z` on track load and `FLX10_POS deck=N pos=0.0234` every 100ms for any loaded deck.
2. Daemon `flx10_screen_daemon.py` tails `~/.mixxx/mixxx.log`, parses both log types.
3. On track load: looks up the track in `mixxxdb.sqlite` by matching `track_samples` (pitch-invariant) + `file_bpm` against `samplerate × duration × channels`. Reads `~/.mixxx/analysis/<analysis_id>` directly, parses the zlib+protobuf in-memory, downsamples Mixxx's 441 fps → Pioneer's 150 fps, encodes PWV5 with low→red/mid→green/high→blue, uploads via the full Serato `xx 30/39/33/35/36/2f` sequence.
4. **200 Hz state-ping** continuously writes the playhead position into `xx 27 [5,6,7]` BE24 = `pos × duration_sec × 128`. POS_RATE=128/sec was determined empirically — the FLX10 firmware interprets each unit as 1/128 sec of audio.
5. **20 Hz `xx 36` trickle** per loaded deck writes 19 entries at the current playhead — keeps the firmware's display buffer alive (without this it drops the waveform after ~1 minute).

No pre-rendering needed; Mixxx's existing analysis data is the source of truth.

**Working invocation for real waveforms:**

```bash
# One-time per track: pre-render Mixxx's analysis to PWV5 cache
python3 ~/.mixxx/controllers/"DDJ-FLX10 Screen Protocol"/flx10_prerender_waveform.py <track_id>
# Then play it on the FLX10 (Mixxx HID screen controller must be disabled or Mixxx closed)
sudo python3 ~/.mixxx/controllers/"DDJ-FLX10 Screen Protocol"/flx10_rekordbox_proto.py \
    --deck 2 --sysex-flavor serato --protocol-flavor serato \
    --wave-file 2:/home/vpinedax/.flx10-cache/<track_id>.pwv5
```

**Earlier milestone (Mixxx integrated):** Updated `PioneerDDJFLX10-screen.js` (Serato protocol, solid-red placeholder waveform) + `Pioneer-DDJ-FLX10-scripts.js` (SysEx handshake) deliver end-to-end working waveform display on the FLX10 when running Mixxx 2.5.6 on Linux — but with a placeholder waveform because Mixxx HID scripts cannot read filesystem cache files. Real-waveform display requires running the pyusb test script as a separate process (the daemon path described below).

**Known limitation (open work item):** the deck-info text fields (time, BPM) on the jog LCDs show fixed placeholder values from `xx 27` instead of Mixxx's real ch16 MIDI values. Removing the duration/BPM bytes from `xx 27` ([16,17] and [21,22]) didn't shift control back to MIDI ch16 — the firmware enters "HID controls text" mode whenever `xx 27` has any track_loaded markers set, and dropping the loaded markers entirely also drops the waveform render. To fix this we need to reverse-engineer the encoding of the duration/BPM/track-id bytes so we can populate them with real Mixxx track data instead of placeholders. A second Serato capture with different known-duration tracks would let us crack the encoding by correlation.

---

**Original status (2026-05-23 night, pre-Mixxx-integration):** SYNTHESIZED WAVEFORMS RENDER on the FLX10 jog wheel via Linux.

**Confirmed working invocation:**

```bash
sudo python3 "DDJ-FLX10 Screen Protocol/flx10_rekordbox_proto.py" --deck 2 \
    --sysex-flavor serato --protocol-flavor serato --solid-waveform
```

**The five byte-level bugs that were hiding from us:**

1. `xx 27` loaded packet: bytes `[32,33]` must be `ff ff`, not `e0 01` (we'd flipped these based on a misleading histogram).
2. `xx 30` init packet: 8 enable-flags at positions `[10, 16, 22, 28, 34, 40, 46, 52]` all must be `0xff`. We were setting only `[10]`.
3. `xx 35`: 3 packets per deck (first all-zero header; next two with `[2]=0e [3]=e3`) — sent right BEFORE `xx 36`. Missing entirely from our script.
4. `xx 39` pad labels: had several `02 3f` markers at irregular positions across the 3 segments. Hardcoded byte-for-byte from capture now.
5. `xx 2f` cue-data placeholder: missing entirely. One packet per deck after `xx 36`.

Required SEND ORDER per deck: `xx 27 → xx 30 → xx 39 → xx 33 → xx 35 → xx 36 → xx 2f`. Must repeat for all 4 decks (Serato's dual-deck UI requires deck-1 waveform data to render the deck-2 view too).

**Remaining work is integration, not protocol decoding:**

- Port to Mixxx (`PioneerDDJFLX10-screen.js` + SysEx handshake in `PioneerDDJFLX10-scripts.js` since HID controllers cannot send MIDI in Mixxx)
- Hook real Mixxx track waveform data into the `xx 36` upload (currently uses a synthetic test pattern)

```bash
# Confirmed working invocation:
sudo python3 flx10_unlock_v2.py
sudo python3 "DDJ-FLX10 Screen Protocol/flx10_rekordbox_proto.py" --deck 2 --sysex-flavor serato
```

**Remaining gaps:**
1. Metadata is read as "no track loaded" — either our `xx 21` byte layout is still off, or after the Serato-flavor handshake the firmware expects Serato's `xx 27` instead of `xx 21`.
2. Waveform upload behavior post-handshake is unverified; the `xx 2C / xx 2E` data we sent may have been ignored for the same reason as metadata.
3. Need to isolate whether `03 01` alone, `50 31` alone, or the combination is the actual gate (experiments A/B below).

This document is the canonical "where we are" for a fresh contributor. It supersedes the earlier `DDJ-FLX10 Screen Protocol/FLX10-SCREEN-PROTOCOL-INVESTIGATION.md` (kept for history) and complements [`FLX10-INTEGRATION-NOTES.md`](FLX10-INTEGRATION-NOTES.md).

---

## TL;DR

| Capability | Path | Works? |
|------------|------|--------|
| Buttons / knobs / encoders in | MIDI in (iface 4) | ✅ |
| LEDs / pad colors | MIDI out (iface 4) | ✅ |
| Jog display BPM / time / spinning marker | MIDI ch16 CC/Note (iface 4) | ✅ |
| Jog display **waveform ring** | HID (iface 5) PWV5 / 3-band | ❌ acks but no render |
| Jog display **static background image** | HID (iface 5) `xx D0/D1/D7` | ❌ no render (May 2026) |
| Audio playback / capture | UAC2 (iface 1/2) | ✅ on kernel 6.x+ |

We send valid bytes. The device acks every one of them (`xx D8 ...` on EP4 IN). Nothing changes on the LCDs. The only display modes the user can cycle through on the hardware are "rekordbox logo" and "deck mode" (which **should** include the waveform ring). Replaying VirtualDJ's exact byte sequence on Linux produces the same negative result.

The remaining gate is not in the data we send. It is some device-side initialization / mode / firmware state we have not been able to observe in any capture.

---

## What works today (don't break it)

[`PioneerDDJFLX10-scripts.js`](PioneerDDJFLX10-scripts.js) + [`PioneerDDJFLX10.midi.xml`](PioneerDDJFLX10.midi.xml) drive everything in the MIDI column above. The jog display BPM/time/marker work via **MIDI channel 16** CC and Note messages (CC `0xBF`, Note `0x9F`). This is the "MIDI-fallback" display the firmware always supports.

The HID screen module [`PioneerDDJFLX10-screen.js`](PioneerDDJFLX10-screen.js) + [`PioneerDDJFLX10-screen.hid.xml`](PioneerDDJFLX10-screen.hid.xml) is the **attempted waveform path**. It currently uses the VirtualDJ `xx 37 / xx 38` 3-band format. Loads cleanly in Mixxx, sends packets, gets ACKs — but produces no visible change. Keep it loaded only when actively iterating; it does no harm but no good either.

---

## USB device layout (reference)

```
VID:PID  2B73:0041  (AlphaTheta DDJ-FLX10)

Iface 0  Audio Control                                        EP0
Iface 1  Audio Streaming OUT (iso, 4ch S24_3LE @ 44100)       EP1 OUT  — snd-usb-audio
Iface 2  Audio Streaming IN  (iso, 10ch S24_3LE @ 44100)      EP1 IN   — snd-usb-audio
Iface 3  Vendor (unused)                                      — unbound
Iface 4  MIDI bulk                                            EP2/EP3  — ALSA seq
Iface 5  HID vendor                                           EP5 OUT 128B int (screen writes)
                                                              EP4 IN  64B int  (D8 ACKs)
```

Find the hidraw node:
```bash
for d in /sys/class/hidraw/hidraw*/device/uevent; do
  grep -q "2B73.*0041" "$d" && echo "${d%/device/uevent}"
done
```

---

## Linux hidraw Report ID prefix (essential gotcha)

The FLX10's HID descriptor declares 128-byte output reports **without a Report ID**. Linux's hidraw API still treats the first byte of every write as a Report ID and strips it. So:

```python
# WRONG — kernel strips one byte; device gets 127 and silently rejects
os.write(fd, my_128_byte_packet)

# RIGHT
os.write(fd, b"\x00" + my_128_byte_packet)
```

**Mixxx's HID API handles this internally.** `controller.send(pkt, null, 0)` does the right thing.

**pyusb writing directly to EP5 via `ep_out.write()` does NOT need the prefix** — it bypasses hidraw and goes straight to the endpoint. Both pyusb scripts in this repo do it that way.

---

## Complete protocol reference

All HID screen packets are **128 bytes**. Common header:

```
[0]  deck byte: 0x10/0x20/0x30/0x40 for decks 1/2/3/4
     (0x00 for broadcast packets like heartbeat)
[1]  command byte (0x21, 0x2C, 0x2E, 0x37, 0x38, 0x3D, ...)
[2]  segment number (1-indexed unless noted)
[3]  subframe (0 or 1, except xx 38 which uses up to 4)
[4]  command-specific (often total segment count or magic byte)
[5+] payload
```

### Vendor unlock — 7 control transfers on EP0

Present in **both** the rekordbox and VirtualDJ full-init captures (sent by Pioneer's Windows driver / settings utility at device attach):

```
bmRequestType = 0x40  (vendor OUT, device recipient)
bRequest      = 3
wLength       = 0

  wValue  wIndex
  0x0100  0xC028
  0x0000  0xC029
  0x0200  0xC013
  0x0000  0xC02B
  0x0100  0xC026
  0x0000  0xC01D
  0x0100  0xC027
```

Implemented in [`flx10_unlock_v2.py`](flx10_unlock_v2.py). On Linux these put audio into a state where the FLX10 LCDs stop showing "no audio driver." They appear to be audio-clock / class-control writes, not screen-related. **They are necessary for audio. They do not unlock the screen.**

### SET_IDLE — STALLs, expected

Windows HID class driver sends `SET_IDLE` to interface 5. The FLX10 STALLs it. The Windows driver ignores the STALL and continues. Our pyusb test scripts also send it and get the same STALL. **Not the gate.**

### MIDI SysEx — three flavors across three apps

AlphaTheta SysEx manufacturer ID is `00 40 05`. The byte at index 8 (after `F0 00 40 05 00 00 04 01`) is the opcode. The three apps we captured use overlapping but distinct sets:

| Pattern | rekordbox | VirtualDJ | Serato | Frequency |
|---------|-----------|-----------|--------|-----------|
| `F0 00 40 05 00 00 04 01 00 03 01 F7` | ❌ | ✅ once | ✅ once | one-shot before first screen write |
| `F0 00 40 05 00 00 04 01 00 50 00 F7` | ✅ poll | ✅ once | ❌ | rekordbox keepalive (~200ms) |
| `F0 00 40 05 00 00 04 01 00 50 31 F7` | ❌ | ❌ | ✅ poll | Serato keepalive (~250ms) |
| `F0 00 20 7F 01 02 01 01 22 0F 0C 06 08 04 0A 02 ... F7` | ❌ | ❌ | ✅ once | Stanton/Serato app identification |
| `F0 00 40 05 00 00 04 01 11/12/13/14 00 ...` | ❌ | ❌ | ✅ live | Serato deck-specific (11=deck 1 etc.) |
| `F0 00 40 05 00 00 04 01 00 00 0B 31 ...` | ✅ track-load | ❌ | ✅ track-load | track-load events |
| `F0 00 40 05 00 00 04 01 00 00 0C 00 ...` | ✅ track-load | ❌ | ✅ track-load | track-load events |

**Key finding: `03 01` is sent by both apps where the screen renders (VirtualDJ on Linux replay would, Serato on Windows does), but NOT by rekordbox.** Rekordbox uses xx 3D heartbeats + the `50 00` keepalive instead. The two apps that send `03 01` happen to also be the ones whose screen-render pipelines we never directly observed on Linux.

The user previously tested `03 01` via `amidi -S` in isolation and "nothing changed," but that test wasn't followed by the screen-data flow it precedes in the captures. As of 2026-05-23, `flx10_rekordbox_proto.py` sends `03 01` once at startup before the keepalive poll begins; rerunning with that script is the next concrete test.

Run options on `flx10_rekordbox_proto.py`:
```
sudo python3 flx10_rekordbox_proto.py --deck 2                                # default: 03 01 once + 50 00 poll + rekordbox protocol
sudo python3 flx10_rekordbox_proto.py --deck 2 --sysex-flavor serato          # use 50 31 poll instead
sudo python3 flx10_rekordbox_proto.py --deck 2 --no-sysex-enter               # skip the 03 01 (control test)
```

### `00 3D` — Broadcast heartbeat (rekordbox)

Sent by rekordbox 6,388 times in the deck-loaded capture, in **bursts of 5 at ~1ms apart, every ~20ms** (~50Hz overall). VirtualDJ does NOT send this. Always uses deck byte 0x00 (broadcast):

```
[0]=00  [1]=3D  [2]=seg(1..5)  [3]=00  [4]=05  [5..127]=00
```

### `xx 21` — Track metadata (50 Hz per deck)

Sent continuously to all 4 decks at ~50Hz regardless of load state. **VirtualDJ and rekordbox use DIFFERENT byte layouts** — this caused us hours of confusion. The byte differences:

| Byte | VirtualDJ | rekordbox | Meaning |
|------|-----------|-----------|---------|
| `[3]`   | `0x0a` | `0x0a` | constant |
| `[4]`   | `0x04` stopped / `0x0c` playing | `0x20` | playback state |
| `[5]`   | `0x01` stopped / `0x81` playing | `0x01` | playback flag |
| `[7]`   | `0x02` loaded / `0x01` empty | same | track load |
| `[8]`   | `0x00` | `0x80` | rekordbox-specific |
| `[9]`   | `0x00` | `0x10` | rekordbox-specific |
| `[10]`  | `0x0e` | `0x00` | (moved in rekordbox) |
| `[13]`  | `0x00` | `0x0e` | activity flag (rekordbox here) |
| `[21..24]` | `75 40 2a ff` (loaded) / `78 00 00 00` (empty) | `00 00 3b ff` | track-state bytes |
| `[27]`  | `0x80` | `0x80` | constant |
| `[40..41]` | `00 00` | `ff ff` | rekordbox-only |
| `[49]`  | `0x30` stopped / `0x20` playing | `0x00` | (different role) |
| `[58]`  | `0x03` | `0x80` | (different) |
| `[59]`  | `0x1e` (= 30 xx 37 segs) | `0x0d` (=13) | overview count, but doesn't match xx 2C segment count of 35 — incompletely decoded |
| `[61]`  | `0x0d` | (different position) | |

The current `screen.js` uses the VirtualDJ layout. The current `flx10_rekordbox_proto.py` uses the rekordbox layout. Both fail to render.

### `xx 2C` — Overview (rekordbox)

Sent by rekordbox as 35 packets per deck. **VirtualDJ does not use this.**

```
[0]=deck  [1]=2C  [2]=seg(1..35)  [3]=00  [4]=0x23 (= 35 total)  [5]=0x00 (NO marker)
[6..127] = 122 bytes payload
```

35 × 122 = 4270 bytes of payload per deck. The inner entry format inside the payload bytes shows repeating ~7-byte groups with `00` separators but isn't fully decoded. Likely a 2-byte-per-entry compact preview format.

### `xx 2E` — Waveform detail in PWV5 color (rekordbox)

Pioneer's standard color waveform format (same as their EXPORT files, documented by Deep Symmetry). Each entry is **2 bytes LE16** packing r/g/b/height:

```
v = (r << 13) | (g << 10) | (b << 7) | (h << 2)        # r,g,b ∈ 0..7; h ∈ 0..31
bytes = [v & 0xff, v >> 8]                              # little-endian
```

Packet format:
```
[0]=deck  [1]=2E  [2]=seg  [3]=sub  [4]=0xB9 (color) | 0x87 (mono)  [5]=0x03 (always, color marker)

seg 1 sub 0 only:
  [6..9]    = 4-byte track-specific header (e.g. 12 e3 00 00 — likely a track hash)
  [10..127] = 59 PWV5 entries (118 bytes)

every other packet:
  [6..127]  = 61 PWV5 entries (122 bytes)
```

Rate: **150 entries per second of audio.** rekordbox sent 946 such packets for deck 1 in the capture (multiple subframes, seg wraps at 255).

### `xx 37` — Overview (VirtualDJ — different from rekordbox's `xx 2C`)

```
[0]=deck  [1]=37  [2]=seg(1..30)  [3]=0  [4]=0x1E (=30 total)  [5]=0x00 (NO marker)
[6..127] = 3-byte (low,mid,high) entries, NOT duplicated
```

30 × 122 / 3 = 1220 entries per upload. VirtualDJ sent it **twice** (two bursts of 30 packets), which was a key correction to our initial implementation.

### `xx 27` — Per-deck state (Serato)

Serato's equivalent of `xx 21`. Sent constantly per deck — ~1420 packets per deck in the captured session.

```
[0]=deck  [1]=27  [2]=b4 (constant)  [3]=80 (constant)  [4]=01 (loaded?)
[5..14]   mostly zero, occasional state bytes
[16]      0x0e — activity flag
[21]      0x80 (constant)
[25]      0x80 (constant)
[26]      0x0d (constant)
[27]      sub-state per deck
[28..30]  ff ff ff (white?)
```

**Serato does NOT send `xx 21` at all.** It uses `xx 27` instead.

### `xx 36` — Waveform detail (Serato)

Serato's waveform format. Same PWV5 LE16 encoding as rekordbox's `xx 2E` but a different framing:

```
[0]=deck  [1]=36  [2]=seg  [3]=sub  [4]=01  [5]=00
[6]=0x13 (= 19, entries per packet?)
[7]=00
[8..11]   LE32 position counter (increments by ~18 between packets)
[12..13]  00 00
[14..127] up to ~19 PWV5 LE16 entries (38 bytes), rest zero-padded
```

Capture shows Serato sent ~420-490 `xx 36` packets per deck. Position counter starts at 0 and walks forward; values like 0x12, 0x25, 0x37 in consecutive packets suggest ~18 entries advance per packet.

**Serato does NOT send `xx 2C` overview, `xx 2E` waveform, `xx 37/38` waveform, or `xx 3D` heartbeat.** Pure `xx 27` + `xx 36` + a few small commands (`xx 33`, `xx 2F`, `xx 30`, `xx 35`, `xx 39`).

### `xx 38` — Waveform detail 3-band (VirtualDJ)

A simpler 3-byte-per-entry format (raw low/mid/high amplitudes):

```
[0]=deck  [1]=38  [2]=seg(1..255)  [3]=sub(0..3+)  [4]=0xD9 (magic, NOT segment count)  [5]=0x01 (data marker)
[6..127] = 122 bytes of stream

Stream layout:
  [0..3]   4-byte header: entry_count_LE16, 0x00, 0x00
  [4+]     each (low,mid,high) entry sent TWICE on the wire:
           lo mid hi lo mid hi  lo mid hi lo mid hi  ...
```

Verified byte-for-byte against VirtualDJ's 5,184-entry capture (100% match). 150 entries per second.

### `xx D0 / xx D1 / xx D7` — Static background image (JPEG)

Used in "DJ Logo" display mode for custom backgrounds.

```
xx D0  Header: [1]=D0  [2]=slot(0x01/0x02)  [3..4]=jpeg_size_LE16  rest=0
xx D1  Data:   [1]=D1  [2]=seg(1..N)  [3]=00  [4]=N  [5..127]=JPEG bytes
       (first data packet starts with size LE16 prefix then JPEG)
xx D7  Commit: [1]=D7  rest=0
xx D0  Clear:  same as header with size=0, then xx D7
```

Implemented in [`DDJ-FLX10 Screen Protocol/flx10_set_background.py`](DDJ-FLX10 Screen Protocol/flx10_set_background.py).

**May 2026 test result:** uploading a 240×240 red JPEG produced **zero ACKs and no render** even after vendor unlock and with the device in DJ Logo display mode. This corrected an earlier note that claimed ACKs returned. The script needs review on its EP4 IN read path — it may be timing out before the device replies — but the visual result is unambiguous: **no rendering**.

### `xx D8` — ACK (EP4 IN, device → host)

64-byte response packets. Returned for every screen command type when the device accepts a packet. Their presence proves the firmware received our data; **their presence does not prove the data will render.**

### Other commands seen in captures (not yet investigated)

| Cmd | Count in rekordbox cap | Notes |
|-----|------------------------|-------|
| `xx 2B` | rare | small bulk transfer |
| `xx 2D` | ~6 | state/cue packets at track load |
| `xx 2F` | up to ~28 | cue-point data |
| `xx 30` | ~9 | playback state/playhead? |
| `xx 33` | 30 | JPEG-wrapped album-art-sized image |
| `xx 39` | 57 (in bursts of 3) | ASCII labels (e.g. "HOT CUE\0") for pad mode |
| `xx 3B / 3E` | a few each | likely playhead position updates during playback |

Not required for waveform rendering (none appear before xx 37/2C/2E in the captures), but useful when implementing full rekordbox-style behavior.

---

## Init sequence timeline (rekordbox full-init capture)

From `flx10-connect-driversettingutil-startrkrdbox-loadtrack-playtrack.pcapng`. **Pioneer's settings utility runs at t=8..18s; rekordbox itself launches at ~t=55s.**

```
t=0.0s    USB enumeration (GET_DESCRIPTOR, SET_CONFIGURATION)
t=8.7s    Class control transfers begin (UAC2 GET CUR / SET CUR on clock/sample-rate)
t=9.2s    SET_INTERFACE alt=1 on interfaces 1 and 2 → audio streaming starts
t=9.3s    EP1 OUT host→device audio data flowing (5043-byte iso packets)
t=17.36s  Vendor unlock: 7× (bmRequestType=0x40, bRequest=3, varying wValue/wIndex)
t=37.4s   More UAC2 class control (settings utility making changes)
t=56.17s  SET_INTERFACE alt=0 on interface 4 (MIDI reset)
t=56.18s  First MIDI OUT SysEx: F0 00 40 05 00 00 04 01 00 50 00 F7
t=56..57s 4 more identical SysEx pings (200ms intervals)
t=59.9s   First EP5 OUT screen write: 00 3D heartbeat
t=60..s   xx 21 metadata + xx 3D heartbeats continuously
t=62.6s   xx 37 (VirtualDJ) — no, wait, this is rekordbox capture:
          first xx 2C overview to deck 1 (35 packets)
          immediately after: xx 2E PWV5 waveform (946 packets to deck 1)
t=84.5s   Track load event: SysEx 0B/0C, then xx 2C+2E to deck 2, etc.
```

We have replicated all of this (unlock + audio + SysEx + heartbeat + xx 21 + xx 2C + xx 2E in correct order), and the device still does not render.

---

## What we've tested

| Layer | Implementation | Result |
|-------|----------------|--------|
| `xx 38` (3-band, VJ format) via hidraw | `flx10_set_idle_test.py` | ACKs, no render |
| `xx 38` via Mixxx HID API | `PioneerDDJFLX10-screen.js` | ACKs, no render |
| VirtualDJ exact-byte replay | `flx10_replay.py` | ACKs, no render |
| `xx 2E` PWV5 (rekordbox format) | `flx10_rekordbox_proto.py` | ACKs, no render |
| All four layers (unlock + audio + SysEx + heartbeat + uploads) | `flx10_rekordbox_proto.py` | ACKs, no render |
| `xx 21` VirtualDJ byte layout | both | ACKs, no render |
| `xx 21` rekordbox byte layout | `flx10_rekordbox_proto.py` | ACKs, no render |
| `xx 21` with playing state byte set | both | no render |
| `xx 3D` heartbeat enabled | `flx10_rekordbox_proto.py` | no render |
| SET_IDLE to iface 5 | both | STALLs (expected) |
| 7-command vendor unlock | `flx10_unlock_v2.py` | unlocks audio; no screen effect |
| EP1 OUT silent-audio stream | aplay subprocess in test script | no screen effect |
| SysEx polling on MIDI (50 00 rekordbox flavor) | amidi thread in test script | no screen effect |
| `xx D0/D1/D7` static JPEG background | `flx10_set_background.py` | **no render, 0 ACKs** |
| `03 01` SysEx one-shot (standalone via amidi) | `amidi -S` | no immediate visible effect |
| `03 01` one-shot + rekordbox `50 00` keepalive + rk protocol | `flx10_rekordbox_proto.py` default | no render |
| `03 01` one-shot + **Serato `50 31` keepalive** + rk protocol | `--sysex-flavor serato` | ✅ **JOG WHEEL LCDS LIGHT UP — first time anything rendered.** Deck text shows "no track loaded" — metadata not yet honored |
| Serato `xx 27/36` protocol with same SysEx handshake | not yet implemented | next test |

---

## The remaining hypothesis

The most concrete current lead is the `03 01` SysEx, present in Serato and VirtualDJ but absent from rekordbox. The user has tested `03 01` standalone (`amidi -S`) and seen no immediate effect, but never tested it as the precursor to a screen-data flow. The updated `flx10_rekordbox_proto.py` now sends `03 01` once at startup before the rest of the protocol. **Re-running this script is the priority test before drawing further conclusions.**

If that still doesn't render: every protocol layer visible on the wire has been reproduced and the gate is **device-side, not protocol-side**.

The remaining candidates, in order of likelihood:

1. **Firmware version / region.** AlphaTheta has shipped firmware updates that change protocol behavior on other Pioneer DJ controllers. The captured FLX10 may be on a different firmware than the user's. **Action: read the firmware version on the user's unit and compare against the latest from AlphaTheta. If outdated, update via Pioneer DJ Settings Utility (Windows).**

2. **Hidden device-side setting.** Pioneer controllers often have a hardware "utility mode" accessed by holding buttons at power-on. There may be a "USB display mode" or "HID software" setting that gates whether HID screen data is honored. **Action: power on FLX10 while holding various buttons (LOAD A, LOAD B, SHIFT, BROWSE) one at a time; check the LCDs for a menu.**

3. **Some signal in the capture we still haven't seen.** All captures so far are from a USB sniffer that started AFTER the device enumerated. There may be a control transfer issued earlier — at the Windows USB enumeration step — that puts the firmware into a "HID software present" mode. **Action: capture from device-plug-in with usbmon on Linux while Wine or a Windows VM with USB passthrough loads the AlphaTheta driver.**

4. **Proprietary signed handshake.** The Windows driver may issue an authenticated command we cannot reproduce. Unlikely (most Pioneer / AlphaTheta gear doesn't sign), but possible.

---

## Recommended next investigations (in priority order)

### 1. Firmware version + utility menu check

Cost: minutes. Highest expected value because **no amount of protocol decoding will fix a device-side gate.**

- Power off FLX10, hold each candidate button while powering on, watch the LCDs.
- The official manual or AlphaTheta forum threads may have undocumented combos.
- Note the firmware version (usually visible in a corner of the LCDs at startup, or in the AlphaTheta DJ Settings Utility).

### 2. Serato capture

Cost: an hour if a Serato license / trial is available. Different software, same hardware — if Serato makes the waveform render, comparing its init sequence against rekordbox's may reveal an additional handshake step. Specifically:
- Did Serato send any vendor commands on top of the 7 we've identified?
- Does Serato use yet another `xx ??` command set?
- Is there a HID feature report (`SET_REPORT` / `GET_REPORT`) we missed?

Capture procedure (Windows + USBPcap, save as pcapng):
```
1. Disconnect FLX10
2. Start USBPcap, select the FLX10's USB hub
3. Plug in FLX10 — capture enumeration!
4. Launch Serato DJ Pro, wait for it to recognize the FLX10
5. Load a track, press play, scrub through it
6. Stop capture
```

Drop the pcapng under `~/Downloads/` and re-run the same `tshark` queries used for the rekordbox capture (recipes below).

### 3. usbmon capture on Linux through Wine / KVM

Capture the moment the Pioneer driver attaches in a Windows VM with USB passthrough, from the *host* Linux's `usbmon`. This catches anything the driver does BEFORE Windows USBPcap starts. Procedure outline:

```
# On Linux host:
sudo modprobe usbmon
sudo mount -t debugfs none /sys/kernel/debug 2>/dev/null || true

# Find the USB bus the FLX10 is on after passthrough
ls -la /sys/kernel/debug/usb/usbmon/
# typically you want bus 1u, 2u, etc.

sudo tshark -i usbmon1 -w /tmp/flx10-attach.pcapng &
# Pass FLX10 to VM, watch driver load, wait for render
# Stop capture
```

### 4. Read EP4 IN replies carefully during the rekordbox-proto run

Currently we briefly poll EP4 IN after each phase, but only print to console. If the device sends data of meaningful length (not just ACKs), that data may contain state we can act on. Modify `flx10_rekordbox_proto.py` to log EVERY EP4 IN packet to a file with timestamps and re-run.

### 5. Try implementing HID input report parsing

The FLX10's iface 5 also has an INPUT endpoint (EP4 IN). If the device sends periodic status reports we're not reading, the firmware may stall its render pipeline waiting on the host to read them.

---

## Tools / scripts inventory

All paths are under [`/home/vpinedax/.mixxx/controllers/`](.):

### Mixxx mapping (production)
- [`PioneerDDJFLX10.midi.xml`](PioneerDDJFLX10.midi.xml) — MIDI mapping (working)
- [`PioneerDDJFLX10-scripts.js`](PioneerDDJFLX10-scripts.js) — MIDI handler (working)
- [`PioneerDDJFLX10-screen.hid.xml`](PioneerDDJFLX10-screen.hid.xml) — HID controller declaration
- [`PioneerDDJFLX10-screen.js`](PioneerDDJFLX10-screen.js) — HID screen module (sends correct bytes, currently no visible effect)

### Test scripts (in `DDJ-FLX10 Screen Protocol/`)
- [`flx10_unlock_v2.py`](DDJ-FLX10 Screen Protocol/flx10_unlock_v2.py) — 7-command vendor unlock + snd-usb-audio rebind. Run before any test if device shows "no audio driver"
- [`flx10_set_idle_test.py`](DDJ-FLX10 Screen Protocol/flx10_set_idle_test.py) — VirtualDJ-style `xx 37/xx 38` test via pyusb
- [`flx10_rekordbox_proto.py`](DDJ-FLX10 Screen Protocol/flx10_rekordbox_proto.py) — Rekordbox-style `xx 2C/xx 2E/xx 3D` + SysEx poll + silent audio stream
- [`flx10_set_background.py`](DDJ-FLX10 Screen Protocol/flx10_set_background.py) — Static JPEG background upload via `xx D0/D1/D7`
- [`flx10_replay.py`](DDJ-FLX10 Screen Protocol/flx10_replay.py) — Replay packets from a pcapng
- [`flx10_ctrl_dump.py`](DDJ-FLX10 Screen Protocol/flx10_ctrl_dump.py) — Extract control transfers from pcapng (USBPcap LinkType=152/220 only)

### Capture files
Stored under `~/Downloads/`:
- `flx10-virtualdj-waveform.pcapng` — VirtualDJ rendering waveform (Windows VM)
- `flx10-4decks-loaded.pcapng` — Rekordbox with 4 decks loaded (mid-stream, no enumeration)
- `flx10-connect-driversettingutil-startrkrdbox-loadtrack-playtrack.pcapng` — Rekordbox **full-init** from cold plug-in. Most valuable.

---

## How to analyze captures (tshark recipes)

These all work on USBPcap pcapng files captured on Windows.

### Find the FLX10's device address in the capture
```bash
tshark -r FILE -Y "usb.idVendor == 0x2b73" -T fields -e usb.device_address -c 1
```

### Histogram of EP5 OUT command bytes
```bash
tshark -r FILE -Y "usb.src == \"host\" && usb.endpoint_address == 0x05" \
  -T fields -e usbhid.data 2>/dev/null \
  | awk '{print substr($1,1,4)}' | sort | uniq -c | sort -rn
```

### First N EP5 OUT writes with command/seg/sub
```bash
tshark -r FILE -Y "usb.src == \"host\" && usb.endpoint_address == 0x05" \
  -T fields -e frame.number -e frame.time_relative -e usbhid.data 2>/dev/null \
  | head -20 | awk '{print $1, $2, substr($3,1,8)}'
```

### All vendor OUT control transfers (the 7-command unlock)
```bash
tshark -r FILE -Y "usb.bmRequestType == 0x40 && usb.device_address == DEV" \
  -T fields -e frame.number -e frame.time_relative \
  -e usb.setup.wValue -e usb.setup.wIndex
```

### All SET_INTERFACE calls (alt-setting changes)
```bash
tshark -r FILE -Y "usb.setup.bRequest == 11 && usb.device_address == DEV" -V 2>&1 \
  | grep -E "Frame [0-9]+|bAlternateSetting|wInterface"
```

### Extract MIDI SysEx from EP3 host writes
```bash
for f in $(tshark -r FILE -Y "usb.endpoint_address == 0x03 && usb.src == \"host\" && frame.len > 30" \
              -T fields -e frame.number); do
  echo -n "frame $f: "
  tshark -r FILE -Y "frame.number == $f" -x 2>&1 \
    | grep -A1 "Reassembled" | tail -1 | sed -E 's/^[0-9a-f]+\s+//' | sed -E 's/ {2,}.*$//' | tr -d ' \n'
  echo
done
```

### Hex dump of a single frame's HID payload
```bash
tshark -r FILE -Y "frame.number == N" -x 2>&1 | tail -15
```

---

## For a fresh agent picking this up

If you've cloned this repo and want to make progress on the jog wheel waveform display:

1. **Read this entire file first.** Then [`FLX10-INTEGRATION-NOTES.md`](FLX10-INTEGRATION-NOTES.md). Then [`DDJ-FLX10 Screen Protocol/FLX10-SCREEN-PROTOCOL-INVESTIGATION.md`](DDJ-FLX10 Screen Protocol/FLX10-SCREEN-PROTOCOL-INVESTIGATION.md) (historical, partly superseded).
2. **Do not start by decoding another capture.** The protocol is fully decoded. Adding more byte-level analysis will not unblock rendering.
3. **Do start by determining whether the user's FLX10 unit is even capable of rendering HID screen data via Linux.** Run `flx10_set_background.py` with a red JPEG. If you can make a red square appear on the jog LCD, the protocol path works and you have something to iterate against. Until then, you are debugging blind.
4. **Investigation 1 (firmware/utility menu) is far more likely to unblock you than further captures.** Don't skip it.
5. **If you do capture more traffic, capture from device plug-in.** Mid-stream captures lose the enumeration handshake which is the most likely place the gate lives.
6. **Update this document with negative results too.** Tracking what doesn't work is as valuable as tracking what does.

The user (Veezuhz) has been at this on-and-off for months. Be honest about dead-ends rather than chasing yet another speculative hypothesis. The MIDI mapping, LEDs, and channel-16 jog display already make the FLX10 usable on Mixxx; the waveform ring is a nice-to-have that may genuinely be blocked at the firmware layer.

---

## Credits

- **Veezuhz (Victor Pineda)** — all FLX10-specific reverse engineering and Linux integration
- **Marc Zischka (Zim)** — original FLX10 mapping (forked from)
- **Arnold Kalambani** — DDJ-1000 mapping (ancestor)
- **Loui1979** — community discovery of channel-16 display CCs
- **Deep Symmetry team (James Elliott et al)** — Pioneer ANLZ / PWV5 format documentation: https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/anlz.html
