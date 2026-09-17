# DDJ-FLX10 Screen Protocol — Reverse Engineering Notes

Status: **partial** — input/MIDI fully decoded, static backgrounds decoded
but gated by an audio-class state we can't easily replicate on Linux, live
waveform protocol partially decoded.

Captured 2026-05-22 by Victor Pineda (Veezuhz) on Windows + USBPcap,
analyzed against the Linux/Mixxx target environment.

## Device overview

- **VID:PID**: `2B73:0041` (AlphaTheta DDJ-FLX10)
- **Bus**: high speed
- **USB configuration**: composite device, 6 interfaces

### Interface map

| Iface | Class           | Endpoints                          | Purpose                            |
| ----- | --------------- | ---------------------------------- | ---------------------------------- |
| 0     | Audio Control   | EP0 (control)                      | Audio config, sample rate          |
| 1     | Audio Streaming | EP1 OUT (iso)                      | Audio playback (host → device)     |
| 2     | Audio Streaming | EP1 IN (iso)                       | Audio capture (device → host)      |
| 3     | (vendor)        | small bulk                         | Management, mostly empty           |
| 4     | MIDI (USB-MIDI Class) | EP2 IN/OUT (bulk) in driver mode, EP3 in class-compliant | All button/knob/encoder MIDI |
| 5     | HID (vendor-defined) | EP5 OUT (128B int), EP4 IN (64B int) | Screen display protocol      |

The HID report descriptor on interface 5 declares:
- **Output reports**: 128 bytes, no Report ID
- **Input reports**: 64 bytes, no Report ID

## Unlock handshake

The FLX10 ships in a state where some Linux drivers can't probe audio properly.
The handshake is **seven vendor OUT control transfers** sent in order:

```
bmRequestType=0x40, bRequest=0x03
   wValue=0x0100  wIndex=0xC028
   wValue=0x0000  wIndex=0xC029
   wValue=0x0200  wIndex=0xC013
   wValue=0x0000  wIndex=0xC02B
   wValue=0x0100  wIndex=0xC026
   wValue=0x0000  wIndex=0xC01D
   wValue=0x0100  wIndex=0xC027
```

Each has `wLength=0` (no data stage). Total transfer time: ~30 ms.

Optional prelude (rekordbox does this, our unlock script doesn't):
```
bmRequestType=0xC0, bRequest=0x00, wValue=0x0000, wIndex=0xC001, wLength=2
```

Returns 2 bytes; appears to be a status check. Not strictly required.

**Important Linux note**: the unlock script (`flx10_unlock_v2.py`) unbinds
`snd-usb-audio` before the handshake to prevent the driver from interfering,
then rebinds it after. On some kernel versions interfaces re-enumerate during
the handshake; the script tolerates partial rebind failures because audio
typically comes up regardless.

On modern Linux (kernel 6.x), audio may work *without* the unlock at all —
the snd-usb-audio improvements have been catching up. The unlock is still
the safest workflow.

## MIDI input/output (interface 4)

In driver-unlocked mode, MIDI flows over **EP2 bulk**, not the standard EP3.
In class-compliant mode (default Linux state without the rekordbox driver
utility), MIDI flows over EP3 bulk per USB-MIDI Class spec.

Either way, **the MIDI bytes are exactly what the official MIDI message list
PDF specifies**. We verified with a 14-bit EQ HI sweep capture: 2,558 events
decoded as 1,279 × `CC 0x07` (MSB) + 1,279 × `CC 0x27` (LSB) on channel 1,
which matches the spec for Deck 1 EQ HI exactly.

Channel layout (from official spec):

| Ch (dec) | Section                              |
| -------- | ------------------------------------ |
| 1–4      | Decks 1–4 non-pad controls           |
| 5        | Beat FX                              |
| 7        | Browser, mixer (master/booth/mic), CFX |
| 8/9      | Deck 1 pads (no shift / shift)       |
| 10/11    | Deck 2 pads                          |
| 12/13    | Deck 3 pads                          |
| 14/15    | Deck 4 pads                          |
| 16       | MIDI-OUT only — display/illumination |

Channel 16 is the **on-controller display feedback channel**. The mapping
script uses it to drive jog-wheel BPM, time, marker position, etc. via
ordinary MIDI CC and Note messages. These work without any special
session — just open the MIDI device and send.

## Display protocol: three layers

The FLX10 jog wheel displays content from three independent sources:

### Layer 1 — MIDI-driven scalar fields (channel 16, EP3 bulk MIDI)

Small numeric fields rendered locally by the controller firmware:
- BPM (per deck) — 14-bit CC pair, BPM × 10
- Playing speed % — 14-bit CC pair
- Time minutes / seconds — single CCs, 0–99 each
- Time mode (elapsed/remaining) — Note, 0x00/0x7F
- Rotating playhead marker — 14-bit CC pair, 0–359 degrees
- Sync MASTER/SYNC indicators, key lock, jog ring color

**This layer is fully working and used by the mapping script.** No special
setup required beyond opening the MIDI device.

### Layer 2 — Static background image (EP5 OUT interrupt)

Set via the rekordbox FLX10 settings utility. **JPEG images** stored to one of
two slots (presumably one per jog wheel).

Protocol — sequence of 128-byte HID output reports on EP5:

```
HEADER packet:
  byte[0] = 0x00
  byte[1] = 0xD0           "begin upload"
  byte[2] = slot           0x01 = first jog, 0x02 = second (inferred)
  byte[3..4] = size LE16   JPEG byte length
  byte[5..127] = zeros

DATA packets (N follow):
  byte[0] = 0x00
  byte[1] = 0xD1           "data chunk"
  byte[2] = segment number (1-indexed)
  byte[3] = 0x00
  byte[4] = total segments
  byte[5..127] = 123 bytes of payload

  First data packet's payload begins with a 4-byte sub-header
  (00 SS SS 00 where SSSS is size LE16 again), then JPEG bytes.
  Subsequent data packets carry pure JPEG continuation bytes.

ACK (from device on EP4 IN):
  byte[0] = 0x00
  byte[1] = 0xD8           upload mode acknowledged
  byte[2..63] = zeros
  
  Two ACK reports are sent in response to the header packet, ~5ms apart.
  The host must consume these before the device's state machine progresses.

DELETE:
  Header packet with size=0  (00 D0 00 00 00 ...)
  Then a commit packet       (00 D7 00 00 00 ...)
```

Settings utility uploads typical 600–1,100 byte JPEGs at quality ~60.
Image dimensions: **240×240**. Color is **converted to grayscale** by the
utility before upload (presumably to keep backgrounds subtle behind colored
overlays).

**The static background protocol is fully decoded but blocked by Layer 3
gating (see below).**

### Layer 3 — Live waveform / playback display (EP5 OUT interrupt)

A completely different command set drives the dynamic on-screen content
during playback. Decoded packet types:

| `(byte[0], byte[1])` | Meaning                              | Segments | Rate         |
| -------------------- | ------------------------------------ | -------- | ------------ |
| `00 3D`              | Heartbeat / clear                    | 5 ×128B  | ~50 Hz cont. |
| `10 21` and per-deck | Track metadata state                 | bursty   | event-driven |
| `xx 2C` (per deck)   | Bulk data — partially loaded         | 35 × 128B| on track load|
| `xx 2E` (per deck)   | Bulk data — full content (≈52 KB)    | 169–256 × 128B | on track load |
| `xx 2F` (per deck)   | Finalize / refresh                   | ~13 ×128B| after 2E     |
| `xx 39` (per deck)   | ASCII pad-mode labels ("HOT CUE")    | 3 × 128B | mode change  |
| `xx 30/3B/3D/3E`     | Various small commands               | 1–5 × 128B | various    |

`byte[0]` encodes the target deck: `0x00` = global, `0x10/0x20/0x30/0x40` =
deck 1/2/3/4.

`byte[2]` is segment number, `byte[4]` is total segments — for multi-segment
commands the host sends segments 1..N back-to-back at ~1ms intervals.

**The pixel format inside `xx 2E` payloads is not yet decoded.** Raw RGB565
decode produces visual noise, suggesting the format is byte-swapped 565,
palette-indexed, run-length-encoded, tiled/planar, or some Pioneer-specific
encoding.

## The state gate

This is what's blocking Linux-side screen writes. Even with correctly
formatted packets, even sending exact byte-for-byte replays of captured
rekordbox sessions, the device silently ignores screen writes (no errors,
no ACKs, no visible change).

Cause: **the device requires a specific Audio Class state transition
immediately before each screen write.**

Captured precondition for each EP5 OUT burst:

```
1. CLASS GET_CUR  on iface 0 entity 0x0100, wLength=16  → read sample rate range
2. CLASS GET_CUR  on iface 0 entity 0x0100, wLength=14  → read second range
3. CLASS SET_CUR  on iface 0 entity 0x0100, wLength=4   → set 44100 Hz
4. SET_INTERFACE  alt=1 on iface 1                       → enable audio playback
5. SET_INTERFACE  alt=1 on iface 2                       → enable audio capture
6. (repeat 1–5 a few times)
7. SET_INTERFACE  alt=0 on iface 1                       → disable audio playback
8. SET_INTERFACE  alt=0 on iface 2                       → disable audio capture
9. wait ~1 second
10. EP5 OUT burst (D0 + D1 chunks + D7 if delete)
```

Without steps 1–9, step 10's writes are silently discarded by the device.

**This sequence happens before EVERY screen write in the capture, not just at
boot.** The settings utility re-cycles the audio interfaces before each
upload.

## Linux implementation barriers

Replicating the state gate from Linux requires fighting `snd-usb-audio`:

1. `snd-usb-audio` claims interfaces 0–2 by default
2. The SET_INTERFACE calls in the state gate require those interfaces to be
   un-claimed (or to use the kernel's ALSA API to drive the transition,
   which doesn't expose alt-setting toggles cleanly)
3. The Audio Class control transfers can be done via pyusb on EP0 without
   claiming an interface — but the SET_INTERFACE calls cannot
4. PulseAudio / PipeWire may probe the device and grab interfaces back
   between our SET_INTERFACE calls and our screen write

Possible approaches, in order of practicality:

- **Accept the limitation**: ship the Mixxx mapping with screen support
  disabled. MIDI side is fully working.
- **Wrap the dance in a privileged tool**: unbind snd-usb-audio, do the
  control transfers via pyusb, do the screen write via hidraw, rebind
  snd-usb-audio. Brittle, but possibly workable for one-shot operations
  like setting a background per track-load.
- **Write a kernel module**: emulate enough of the rekordbox session that
  the device accepts screen writes alongside normal audio operation. This
  is significant engineering effort.

## What we've verified works on Linux

- Audio playback / capture via `snd-usb-audio` after the unlock script
- MIDI input from controller to host via standard ALSA MIDI (matches spec PDF
  byte-for-byte)
- MIDI output from host to controller for channel 16 display feedback
  (BPM, time, marker, etc.) — driven by the existing Mixxx mapping script
- Writing 128-byte HID output reports to `/dev/hidrawN` for interface 5
  — the writes are accepted at the USB layer, just silently ignored by the
  device's firmware state machine

## What we've NOT been able to do on Linux

- Trigger any visible response on the jog wheel display from EP5 OUT writes
- Decode the `xx 2E` pixel format
- Set a static background image (protocol decoded, gate not bypassable)

## Captured artifacts in this repo (or wherever you keep them)

- `flx10_unlock_v2.py` — vendor handshake script (working, audio comes up)
- `flx10_set_background.py` — JPEG upload tool (correct protocol, blocked by gate)
- `flx10_replay.py` — generic pcapng EP5 OUT replayer (writes accepted, no visible effect)
- `flx10_ctrl_dump.py` — control transfer extractor for further analysis
- `flx10_jpegs/` — three reference JPEGs extracted from a settings-utility capture
- This document

## Recommended next steps for anyone picking this up

1. Reproduce the audio-class state gate via libusb + manual snd-usb-audio
   unbinding. Verify that the JPEG protocol then works.
2. With JPEG uploads confirmed working, attempt the live waveform protocol
   replay with the same gate workflow.
3. If live waveform replay works, capture more interactions (track scrub,
   loop in/out, beat sync) to decode the per-frame state updates.
4. Eventually: derive the pixel encoding from controlled test images
   uploaded via the live waveform protocol (if possible) or from a memory
   dump of the device firmware (much harder).

The most valuable single next capture: **load 4 different tracks into the 4
decks simultaneously**, ideally with one rapid action (drag-multi or
load-from-prepared-tracks). All four `xx 2E` bursts will fire in close
succession, and diffing them gives the cleanest possible signal for which
bytes are per-track waveform content versus shared framing.

## Credits / sources

- Loui1979 / community reverse engineering of FLX10 MIDI display CCs
  (channel 16 — confirmed by direct testing in the mapping script)
- Marc Zischka (Zim) — original DDJ-FLX10 Mixxx mapping
- Arnold Kalambani — DDJ-1000 Mixxx mapping (ancestor of this fork)
- Veezuhz — all of the screen-protocol findings above
