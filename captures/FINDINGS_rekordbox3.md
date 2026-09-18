# DDJ-FLX10 ↔ rekordbox USB Protocol — Capture Findings

> **Decision:** I standardized on **rekordbox** captures (not Serato/Lexicon)
> as the reference source — rekordbox drives the FLX10's displays and LEDs with
> the richest, most complete feature set, so its traffic exposes the most of the
> undocumented protocol. All findings here and going forward are from rekordbox.


Source: `flx10_rekordbox3.pcapng` (299 MB, connect-moment capture, Sep 13 2026).
Decoded on Lou-MX with a pure-Python usbmon parser (`tools/flx10-parse.py`).
FLX10 enumerated as **bus 3, device 11** in this capture (address changes each connect).

## Device endpoint map (interfaces the FLX10 exposes)

| Endpoint | Type | Dir | Traffic in capture | Purpose |
|---|---|---|---|---|
| ep1 | ISO | IN/OUT | ~270k pkts | **Audio soundcard** (44.1 kHz) — ignore for mapping |
| **ep3** | BULK | OUT | 1518 | **USB-MIDI OUT** (host→controller: LEDs, jog displays, unlock) |
| **ep2** | BULK | IN | 195 | **USB-MIDI IN** (controller→host: buttons/knobs/jogs) |
| **ep4** | INT | IN | 113k | **High-rate state reports** (jog position/pressure, faders) — HID-style |
| **ep5** | INT | OUT | 1 | **Vendor HID output report** (see below) |
| ep0 | CTRL | — | 340 | Standard USB descriptors + UAC audio setup (NOT unlock) |

Most control transfers are standard UAC audio setup: `44ac0000` = 44100 Hz
sample-rate set, `010b` SET_INTERFACE, `2101/a102` audio clock.

**BUT the audio-unlock IS a set of 7 vendor control transfers**
(`bmRequestType=0x40`, `bRequest=0x03`, vendor OUT), confirmed at frames
116646–116682, in this exact order:

| # | wValue | wIndex |
|---|--------|--------|
| 1 | 0x0100 | 0xC028 |
| 2 | 0x0000 | 0xC029 |
| 3 | 0x0200 | 0xC013 |
| 4 | 0x0000 | 0xC02B |
| 5 | 0x0100 | 0xC026 |
| 6 | 0x0000 | 0xC01D |
| 7 | 0x0100 | 0xC027 |

**This matches `HID/flx10_unlock_v2.py`'s `VENDOR_CMDS` list EXACTLY, in order.**
That unlock script is **Veezuhz's** reverse-engineering (Victor Pineda) — this
capture independently verifies his handshake is correct against real rekordbox.
These are the commands that make snd-usb-audio see the FLX10's sample rates and
PCM substreams. Separately, the SysEx on ep3 (below) drives LEDs/displays/
performance-mode init — a different layer from the audio unlock.

## Vendor HID interface
Interface descriptor shows HID usage page **0xFFA0** (Pioneer private page):
`06a0ff 0901 a101 0902 a100 06a1ff 0903 0904 ...`
One INT ep5 OUT report was sent (#132675), 256 bytes, starting:
`30 21 00 00 20 01 00 00 80 10 00 00 ... 18 fc 00 ...` (rest zero-padded).

## Pioneer SysEx messages (host → controller, ep3 MIDI-OUT)

Manufacturer/prefix: **`F0 00 40 05 00 00 04 01 00`** … `F7`
(`00 40 05` = AlphaTheta/Pioneer region; `00 00 04 01 00` = product/model selector)

Distinct messages, in first-seen order:

| First frame | Count | Message (hex) | Likely meaning |
|---|---|---|---|
| 126553 | 281 | `f000400500000401005000f7` | **Keep-alive / heartbeat** (…`50 00`…) — sent all session |
| 126623 | 6 | `f00040050000040100000b31 00000000 00f7` | init/config A |
| 126643 | 5 | `f00040050000040100000c00 00020e0e 000000f7` | init/config B |
| 128373 | 4 | `f00040050000040100000a00 2800 2600 2800 490a6469 14 00…(zpad)…f7` | **display/state blob** (long, zero-padded) |
| 128427 | 2 | `f000400500000401000301f7` | short command (…`03 01`) |
| 128537 | 1 | `f00040050000040100110f0f0f0f0f0ff7` | LED/segment set (group 0x11) |
| 128587 | 1 | `f00040050000040100120f0f0f0f0f0ff7` | LED/segment set (group 0x12) |
| 128645 | 1 | `f00040050000040100130f0f0f0f0f0ff7` | LED/segment set (group 0x13) |
| 128677 | 1 | `f00040050000040100140f0f0f0f0f0ff7` | LED/segment set (group 0x14) |

The `…11/12/13/14 0f0f0f0f0f0f` group looks like bulk LED-bank illumination
(all-on `0f` values across 4 banks) — classic controller "light everything up"
init flourish. The `00 0a` long blob (#128373) is the display/state payload.

## MIDI IN (controller → host, ep2 BULK) — sanity check
Standard USB-MIDI, cable 0, CC on ch4 (`b4`): e.g. `0994117f` `0994257f`
`0bb43019` … These are normal control-change events → cross-check against
`FLX10- Midi messages - spreadsheetfed.xlsx` (MIDI-IN rows). Confirms the
FLX10's controls speak plain USB-MIDI; nothing exotic on the input side.

## INT ep4 IN — high-rate state (jogs/faders)
Fixed 30-byte reports, e.g.
`00 2014 80c9 0000 16f6 0000 ff03 ff03 ff03 ff03 0000000000000000000000`
The `2014`/`2012` field changes = live jog/analog state. This is the
high-resolution stream rekordbox uses for smooth jog/waveform — likely the
same data our HID daemon wants for waveforms. NOT MIDI; it's the vendor HID IN.

## Conclusions for the Mixxx mapping

1. **MIDI is standard USB-MIDI** on bulk ep2(IN)/ep3(OUT) — matches my
   spreadsheet approach; no surprises on button/knob input.
2. **Unlock/init = a sequence of Pioneer SysEx on ep3**, not a control transfer.
   The candidate init sequence (send in order after connect):
   - `f000400500000401005000f7` (heartbeat, then repeat periodically)
   - `f00040050000040100000b3100000000 00f7`
   - `f00040050000040100000c0000020e0e 000000f7`
   - `f000400500000401000301f7`
   Compare against `HID/flx10_unlock_v2.py` — that script uses a HID/control
   unlock; rekordbox instead appears to drive everything over MIDI SysEx.
3. **Jog displays / high-res jog** ride the **vendor HID** interface
   (INT ep4 IN + ep5 OUT, usage page 0xFFA0) and the long `00 0a` SysEx —
   NOT plain MIDI. This is the target for the HID screen daemon.
4. The ISO audio stream is what bloated the capture to 299 MB and likely
   contributed to rekordbox freezing (host usbmon + VMware passthrough both
   hammering the ISO firehose). For future captures: don't play audio.
   Or set audio to an internal output, like laptop speakers.

## Next captures to nail specifics (one isolated action each, no audio playing)
- Load a track on deck 1 → isolates the jog-display SysEx/HID framebuffer.
- Press one pad → isolates that pad's LED-set SysEx.
- Spin jog slowly → confirms ep4 IN jog encoding.
