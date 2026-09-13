# DDJ-FLX10 Screen (HID only)

Separate Mixxx controller for jog LCDs. Does **not** replace our MIDI mapping.

Enable later as a second controller: **DDJ-FLX10 Screen**
alongside our **Pioneer DDJ-FLX10** MIDI preset. Do not load Veezuhz MIDI.

| File | Role |
|---|---|
| `PioneerDDJFLX10-screen.hid.xml` | Mixxx HID preset — VID `0x2B73` PID `0x0041` |
| `PioneerDDJFLX10-screen.js` | HID `xx 27` state ping (playhead / time / BPM) |
| `flx10_unlock_v2.py` | Linux vendor unlock (every plug-in, Mixxx closed) |
| `flx10_screen_daemon.py` | Waveform upload via hidraw (PWV5) |
| `README-SCREEN.md` | Upstream HID/jog-screen docs (Veezuhz) |
| `SOURCE.txt` | Upstream credit |

Upstream: [Veezuhz/Mixxx_FLX10_Controller_Mapping](https://github.com/Veezuhz/Mixxx_FLX10_Controller_Mapping) @ `c668a0b0`. Research dump (includes *their* MIDI — do not load): `../vendor/Veezuhz-Mixxx_FLX10/`

## Integrate later — do not touch MIDI mapping yet

Screens also need, in *a* MIDI script (not ours until we decide):

1. Pioneer SysEx handshake (unlocks HID screens)
2. `console.log("FLX10_TRACK_LOAD ...")` so the daemon sees loads

Source for those hooks: `vendor/Veezuhz-Mixxx_FLX10/Pioneer-DDJ-FLX10-scripts.js` (block `HID screen SysEx handshake`).

Until that exists, this folder is a standalone HID module: copy these four files into Mixxx’s controllers dir, enable **DDJ-FLX10 Screen**, run unlock + daemon.

HID daemon needs Mixxx **developer mode** so `console.log` from scripts lands in `mixxx.log`. Quit Mixxx, then either:

- Menu: **Mixxx (developer)**
- Terminal:

```bash
flatpak run --branch=nightly --arch=x86_64 --command=mixxx org.mixxx.Mixxx --developer
```

`--controller-debug` is extra (every MIDI message). Use that only when hunting mapping bugs.

Lou-MX Mixxx is Flatpak. Controllers dir:

`/home/Lou/.var/app/org.mixxx.Mixxx/.mixxx/controllers/`

Unlock (FLX10 plugged, Mixxx closed):

```bash
flatpak kill org.mixxx.Mixxx 2>/dev/null
sudo python3 "/home/Lou/Desktop/Hermes Projects/MIXXX-FLX10/HID/flx10_unlock_v2.py"
```
