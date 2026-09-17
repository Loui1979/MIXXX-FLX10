# FLX10 Mixxx Mapping — Integration Notes

> **⚠️ Read [`FLX10-SCREEN-PROTOCOL-FINDINGS.md`](FLX10-SCREEN-PROTOCOL-FINDINGS.md) first** if you
> are picking this work up. It supersedes the screen-protocol parts of this file
> with results through May 2026, including the negative finding that **no HID
> screen feature renders on Linux** despite ACKs (waveform, background image,
> and album-art uploads all silently fail). Some optimistic statements in this
> file below (e.g. "background image — hidraw writes succeed") were written
> before that test result and are no longer accurate.

Context dump for working on the DDJ-FLX10 Mixxx mapping in VS Code.
Read this before touching the mapping if you're coming back to this work
or you're an AI assistant helping with it.

## Project shape

- **`PioneerDDJFLX10-scripts.js`** — main mapping (this is what runs in Mixxx)
- **`PioneerDDJFLX10.midi.xml`** — MIDI control → JS function bindings
- **`docs/screen-protocol.md`** — full USB protocol reference (read once for background)
- **`docs/captures/`** — pcapng files used to reverse-engineer the protocol
- **`docs/waveform_proof/`** — rendered waveforms proving PWV5 decoding works
- **`tools/flx10_*.py`** — helper Python scripts for screen writes (see below)

## What works today, what's pending

| Capability | Status | Notes |
|------------|--------|-------|
| MIDI input (all buttons/knobs/encoders) | ✅ Working | Already in mapping script |
| MIDI output to LEDs | ✅ Working | Already in mapping script |
| Channel 16 jog display feedback (BPM, time, marker, speed) | ✅ Working | Already in mapping script |
| Static background image upload (DJ Logo Display mode) | ⚠️ Protocol decoded, hidraw writes succeed, **but the May 2026 test produced no visible render and 0 ACKs** | See FLX10-SCREEN-PROTOCOL-FINDINGS.md |
| Waveform upload (Waveform Mode) | ⚠️ Protocol decoded for both rekordbox (xx 2C/2E) and VirtualDJ (xx 37/38) variants, hidraw writes succeed, **but no visible render confirmed** | See FLX10-SCREEN-PROTOCOL-FINDINGS.md |
| Switching display modes from software | ❓ Not yet decoded | Might be a controller hardware button only |
| Live playhead scroll on waveform | ❓ Not yet decoded | `xx 30/3B/3D/3E` packets — observed but contents unknown |
| Track metadata push (artist/title text fields) | ❓ Not yet decoded | `xx 21` packets — observed but contents unknown |

## ⭐ THE KEY FINDING — Linux hidraw Report ID prefix

This is the single discovery that made everything else work. If you take
nothing else from this document, take this:

**Every write to `/dev/hidrawN` for the FLX10 on Linux must be prefixed
with an extra `0x00` byte. Without this prefix, every packet goes out one
byte too short and the device silently discards it.**

### Why it took so long to find

The FLX10's HID descriptor for interface 5 declares output reports as
**128 bytes, no Report ID**. So the natural code is:

```python
os.write(fd, my_128_byte_packet)
```

This LOOKS correct. `os.write` reports success. `usbmon` confirms 128
bytes were submitted by the kernel. There are no errors anywhere in the
stack. But the device ignores every packet.

The reason: Linux's hidraw API **always** treats the first byte of a
write as a Report ID, even for devices that don't use Report IDs. The
kernel strips that first byte before transmission. So when you write 128
bytes, only 127 bytes actually reach the device, shifted by one. The
FLX10 firmware sees malformed packets and discards them silently.

This was invisible from every diagnostic surface we tried:
- `os.write` returns success
- No errors in `dmesg`
- No errors on EP0
- The device acks the USB-level transfer (status=0)
- The protocol-level ACKs (`00 D8 ...`) simply never come back

We only caught it by capturing our own Linux side with `usbmon` and
seeing the EP5 OUT write was **127 bytes** starting with `0xD0` instead
of **128 bytes** starting with `0x00 0xD0`. One byte. One byte that
spent multiple sessions looking like an unbreakable cryptographic gate.

### The fix

```python
# WRONG (silently fails — no errors, no ACKs, no rendering):
os.write(fd, packet_128)            # 128 bytes; kernel strips 1; device gets 127

# RIGHT (works — D8 ACKs come back):
os.write(fd, b"\x00" + packet_128)  # 129 bytes; kernel strips 1; device gets 128
```

### Why this matters for the mapping

Any helper script, daemon, or integration that writes to `/dev/hidrawN`
**must** include the prefix. The bug doesn't manifest as a crash or
warning, just as "the screen doesn't update." If you're ever debugging
"screen writes seem to succeed but nothing renders," your first check
is whether the prefix byte is present.

The same applies if anyone ports this to a different language (C, Rust,
Go bindings to libusb/hidapi may or may not handle this automatically —
hidapi DOES, raw ioctl writes do NOT).

### Reads are different

Reads from `/dev/hidrawN` return the input report **as-is**, without
a prefix byte. So `os.read(fd, 64)` returns the actual 64-byte input
report starting with `0x00 0xD8 ...` for an ACK. Don't add a prefix
adjustment on the read side.

## USB device layout (reference)

```
VID:PID  2B73:0041

Iface 0  Audio Control     EP0
Iface 1  Audio Streaming   EP1 OUT (iso, playback)        — snd-usb-audio
Iface 2  Audio Streaming   EP1 IN  (iso, capture)         — snd-usb-audio
Iface 3  vendor            small bulk                     — unused
Iface 4  MIDI              EP2/EP3 bulk                   — Mixxx via ALSA MIDI
Iface 5  HID (vendor)      EP5 OUT 128B int, EP4 IN 64B int  — /dev/hidrawN
```

The screen lives entirely on interface 5. Linux gives us a `/dev/hidrawN`
node automatically. Open it `O_RDWR` so you can also read the `D8` ACK
replies that come back on EP4 IN through the same fd.

To find the right hidraw node programmatically, scan
`/sys/class/hidraw/hidraw*/device/uevent` for `2B73` and `0041`.

## Screen protocol — three layers

### Layer 1: MIDI channel 16 (ALREADY WORKING)

The existing mapping script drives these via `midi.sendShortMsg`:

```javascript
// Already in PioneerDDJFLX10-scripts.js:
var JOG_DISPLAY_CC   = 0xBF;  // CC on channel 16
var JOG_DISPLAY_NOTE = 0x9F;  // Note on channel 16

// Per-deck CC numbers for marker, BPM, speed, time, time-mode:
var _JOG_MARKER_MSB  = [0x10, 0x11, 0x12, 0x13];  // ... etc
```

This is the BPM, time, rotating marker, etc. that displays in **Deck Info**
mode. Visible without any hidraw work.

### Layer 2: Static background images (D0/D1/D7 protocol)

JPEG over USB. 240×240 monochrome converted by device. Use this for
custom track art, mood-based backgrounds, etc. Shows in **DJ Logo Display** mode.

```
Header packet (128 bytes):
  byte[0] = 0x00
  byte[1] = 0xD0           "begin upload"
  byte[2] = slot           0x01 = jog 1, 0x02 = jog 2
  byte[3..4] = size LE16   JPEG byte length
  byte[5..127] = zeros

Data packets (N, one per ~123 bytes of JPEG):
  byte[0] = 0x00
  byte[1] = 0xD1
  byte[2] = segment number (1-indexed)
  byte[3] = 0x00
  byte[4] = total segments
  byte[5..127] = JPEG bytes
  (first data packet's body starts with: 00 SS SS 00 + JPEG bytes, where SSSS = size LE16)

Clear:
  Header packet with size=0 (00 D0 00 00 00 + zeros)
  Then commit packet (00 D7 00 00 00 + zeros)
```

After header, read 2x `00 D8 00 ...` ACKs from EP4 IN. Then send data.

Helper: `tools/flx10_set_background.py <image>` does all of this.

### Layer 3: Live waveform (Pioneer PWV5 LE format)

Per-deck waveform data. Shows in **Waveform Mode** and possibly **Deck Info** mode.

Each waveform entry is 2 bytes (LE16). Decoded:

```python
def encode_pwv5_le(r, g, b, height):
    """r, g, b in 0..7; height in 0..31."""
    v = (r << 13) | (g << 10) | (b << 7) | (height << 2)
    return bytes([v & 0xFF, (v >> 8) & 0xFF])  # LE order!

# Decode (for verification):
def decode_pwv5_le(byte0, byte1):
    v = (byte1 << 8) | byte0  # LE
    r = (v >> 13) & 0x07
    g = (v >> 10) & 0x07
    b = (v >> 7)  & 0x07
    h = (v >> 2)  & 0x1F
    return r, g, b, h
```

**150 entries per second of audio** (Pioneer's standard half-frame rate).
A 5-minute track is 45,000 entries = 90,000 bytes of payload.

Packetizing for EP5 OUT (128-byte packets):

```
byte[0] = deck    0x10/0x20/0x30/0x40 for decks 1/2/3/4
byte[1] = 0x2E    waveform bulk command
byte[2] = segment number (1-indexed for subframe 0, 0-indexed otherwise; wraps at 255)
byte[3] = subframe number (0..3; multi-page for long tracks)
byte[4] = 0xB9 for color, 0x87 for monochrome
bytes[5..127] = 123 bytes of PWV5 LE entries

First data packet of subframe 0 starts with a 5-byte header inside its payload:
  byte[0] = 0x03 (color) or 0x01 (monochrome)
  bytes[1..3] = track-specific (probably hash or duration BE24)
  byte[4] = 0x00
  bytes[5..] = PWV5 entries begin
```

Helper: `tools/flx10_waveform.py` has the encode + packetize + send code.

## Mapping Mixxx waveform data → PWV5

Mixxx already analyzes tracks for its on-screen display. The mapping script
can request that data, but the API is limited from JS — you can get
`waveform_zoom` and overall `playposition` but not the per-sample band
amplitudes.

For now the cleanest approach is to compute the waveform externally:

```python
# Pseudo-code for an offline pre-render (or one-shot at track load)
import librosa
import numpy as np

def waveform_to_pwv5_entries(audio_path, target_rate=150):
    y, sr = librosa.load(audio_path, sr=44100, mono=False)
    if y.ndim > 1: y = y.mean(axis=0)  # mono

    # Multi-band split for color
    n_fft, hop = 2048, sr // target_rate  # ~294 samples per slot at 44100/150
    S = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop))
    # Three bands: low (bass), mid, high
    freq_bins = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    low_mask  = freq_bins < 250
    mid_mask  = (freq_bins >= 250) & (freq_bins < 4000)
    high_mask = freq_bins >= 4000

    low  = S[low_mask].mean(axis=0)
    mid  = S[mid_mask].mean(axis=0)
    high = S[high_mask].mean(axis=0)

    # Normalize to 0-7 for color channels
    def norm3(x): return np.clip((x / x.max()) * 7, 0, 7).astype(int)
    r = norm3(low)
    g = norm3(mid)
    b = norm3(high)

    # Overall amplitude for height (0-31)
    amp = np.sqrt((y[:len(r)*hop].reshape(-1, hop)**2).mean(axis=1))
    h = np.clip((amp / amp.max()) * 31, 0, 31).astype(int)[:len(r)]

    return list(zip(r, g, b, h))
```

Mixxx's color choice convention: Pioneer typically uses
**low band → blue, mid band → red, high band → green/white**, but
this is purely a visual preference. Try them all.

## Architecture: how to call screen helpers from a Mixxx mapping

The Mixxx controller mapping JS engine is sandboxed — no filesystem,
no subprocess, no sockets. To bridge JS → hidraw you have a few options:

**Option A: Helper daemon + named pipe** (recommended)

A small Python daemon runs as root, opens `/dev/hidrawN`, and listens on
a FIFO at `/tmp/flx10-ctrl`:

```bash
mkfifo /tmp/flx10-ctrl
sudo python3 flx10_daemon.py /tmp/flx10-ctrl &
```

From Mixxx JS:

```javascript
// Mixxx exposes script.fileExists but not write. Workaround:
// use engine.connectControl on a sentinel CO, with a tiny userspace
// glue script polling /tmp/flx10-events.json or similar.
```

Actually — Mixxx JS really can't write files. So Option A needs a fancier
bridge. Better:

**Option B: Run helper from Mixxx command line, on track load**

Set `mixxx.cfg` to spawn the helper at startup. The helper subscribes to
Mixxx state via OSC or D-Bus (Mixxx has both). When a deck loads a track,
the helper hears the event and uploads the waveform.

**Option C: Pre-render to a cache**

For each track in the library, batch-render waveforms once into a cache
directory:

```
~/.mixxx/flx10-cache/<track-md5>.pwv5
```

When a track loads, the helper just reads the cache and uploads. No
live analysis needed.

This is probably the right first version — cheap to compute, no latency
at track load, no Mixxx integration headaches.

## What we still don't know — open questions

1. **How to switch display modes from software.** The FLX10 has 4 modes
   (Deck Info / Waveform / Artwork / DJ Logo) that the user cycles
   with a hardware button. We don't know if software can drive this.
   - Hypothesis: it might be a HID feature report (SET_REPORT) that
     hasn't appeared in the captures because the settings utility doesn't
     change modes.
   - Diagnostic: capture USB traffic while the user manually presses
     the view-cycle button. If a control transfer or short EP5 OUT
     packet appears, that's the mode-set command.

2. **`xx 21` track metadata packets.** Observed in playback capture but
   contents not decoded. Probably encode track name, artist, BPM in some
   binary form that shows in Deck Info mode.

3. **`xx 30 / 3B / 3D / 3E` packets.** Small packets that fire during
   playback, presumably playhead position updates for the waveform
   scrolling. Decoding these would let us implement live waveform scroll.

4. **`xx 2C` packets.** Smaller bulk transfers (~35 segments). Likely
   PWV4 (color preview, 1200-entry overview) or PWV3 (monochrome detail)
   variants of the waveform. Same Deep Symmetry tag family.

## Helper scripts in `tools/`

- **`flx10_unlock_v2.py`** — vendor handshake for older kernels. Audio
  works without it on kernel 6.x+.
- **`flx10_set_background.py`** — upload a JPEG as DJ Logo background.
  Usage: `sudo python3 flx10_set_background.py red.jpg [--slot 1]`
- **`flx10_waveform.py`** — generate test waveform and send. Use this as
  the basis for the real Mixxx waveform sender.
  Usage: `sudo python3 flx10_waveform.py --test-pattern --deck 1`
- **`flx10_report_id_fix.py`** — diagnostic that demonstrates the
  Report ID prefix bug. Keep as a sanity-check / regression test.

## Existing MIDI mapping conventions (in `PioneerDDJFLX10-scripts.js`)

- Functions live under the `PioneerDDJFLX10` namespace.
- Per-deck state uses `{1: ..., 2: ..., 3: ..., 4: ...}` object keyed by deck.
- Display feedback uses `_JOG_*` arrays indexed `0..3` for decks `1..4`.
- The script uses `engine.makeConnection` (not the deprecated
  `engine.connectControl`) — keep this convention.
- Channel 16 output uses `midi.sendShortMsg(JOG_DISPLAY_CC|JOG_DISPLAY_NOTE, ...)`.

## Tunable parameters (also in the JS file)

```javascript
var SCRATCH_INTERVALS_PER_REV = 1500;  // jog scratch sensitivity
var JOG_BEND_DIVISOR = 16;             // pitch bend nudge size
var LOOP_ADJUST_STEP = 100;            // samples per jog tick in loop-adjust mode
var BEATGRID_THRESHOLD = 6;            // jog ticks per beatgrid nudge
```

## Credits

- Loui1979 — community discovery of channel 16 display CCs
- Marc Zischka (Zim) — original FLX10 mapping (forked)
- Arnold Kalambani — DDJ-1000 mapping (ancestor of Zim's fork)
- Deep Symmetry team (James Elliott et al) — Pioneer ANLZ/PWV5 format
  documentation: <https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/anlz.html>
- Victor Pineda (Veezuhz) — FLX10-specific USB reverse engineering and
  this entire integration effort
