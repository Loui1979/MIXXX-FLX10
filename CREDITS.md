# Credits

## Victor Pineda — **Veezuhz**
Upstream: https://github.com/Veezuhz/Mixxx_FLX10_Controller_Mapping

Veezuhz did the hard reverse-engineering that keeps the Pioneer DDJ-FLX10 alive
on Mixxx. Specifically, both of these are **his work**:

- **The unlock / handshake script** (`HID/flx10_unlock_v2.py`) — he reverse-
  engineered the vendor handshake between the Pioneer settings utility and the
  FLX10 that stops the jog wheels reporting "no audio driver" on Linux. Our
  Sep 2026 USB capture confirmed his 7 vendor control commands match rekordbox's
  own handshake exactly, in order.
- **The jog-screen (HID) scripts** — `HID/PioneerDDJFLX10-screen.js`,
  `PioneerDDJFLX10-screen.hid.xml`, and the waveform daemon groundwork, plus his
  screen-protocol investigation notes (`vendor/Veezuhz-Mixxx_FLX10/DDJ-FLX10
  Screen Protocol/`).

Snapshot of his repo lives at `vendor/Veezuhz-Mixxx_FLX10/`
@ commit `c668a0b0aa3d1230e19806380f1a711ea7a0f271` (cloned 2026-09-12).
Screen fixes flow back upstream to him via the fork
`Loui1979/Mixxx_FLX10_Controller_Mapping`.

**Thank you, Veezuhz, for keeping this controller alive on open-source software.**

## Lougazi — **Loui1979** (this repo)
- The MIDI mapping in `Controllers/` (Pioneer DDJ-FLX10 for Mixxx), including the
  channel-16 MIDI outputs.
- USB-capture tooling and protocol findings in `tools/` and `captures/`.

## Reference
- Pioneer / AlphaTheta DDJ-FLX10 MIDI Message List (official) — see
  `MIXXX-FLX10 INDEX`.
