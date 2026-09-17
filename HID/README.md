# DDJ-FLX10 jog screens — relay daemon

Drives the FLX10 jog-wheel LCDs (deck info + scrolling waveform) **while Mixxx
keeps full MIDI control and audio**, from a single process: `flx10_relay_daemon.py`.

This replaces the old "separate HID controller + log-tailing daemon" approach.
There is **no `screen.js`, no hidraw, no log tailing, no Mixxx developer mode.**

## Why a relay

The FLX10's jog LCDs only accept waveform data in **HID display mode**. Entering
that mode is a set of 7 vendor control transfers on **ep0** (captured from
rekordbox). The catch: the mode does **not** survive a kernel-driver rebind, so
the daemon must **own the whole device via libusb** and never hand it back.

But if libusb owns the device, `snd-usb-audio` can't bind → Mixxx loses MIDI +
audio. The fix is a **relay**: the daemon owns the USB device and exposes a
**virtual ALSA MIDI port** (`DDJ-FLX10`) that Mixxx connects to instead of the
real controller. The daemon bridges MIDI both ways and drives the screens.

```
                    ┌─────────────────── flx10_relay_daemon.py ───────────────────┐
                    │  owns FLX10 via libusb (HID mode held; never rebinds)        │
   Mixxx  ◄──MIDI──►│  virtual ALSA port "DDJ-FLX10"                               │
   (mapping JS)     │    ep2 ► decode ► vport   (buttons/jogs → Mixxx)             │
                    │    vport ► encode ► ep3   (LEDs/VU/SysEx → controller)       │
                    │    F0 7D … IPC ► parse ► DeckState (CONSUMED, not forwarded) │
                    │    DeckState ► xx27/xx30/xx35/xx36 ► ep5  (jog screens)      │
                    │    ep4 read + ep3 keepalive (hold HID session)               │
                    └──────────────────────────────────────────────────────────────┘
```

Audio: the FLX10 is **not** available as a soundcard while the daemon owns it —
route Mixxx master/cue to onboard/other output. (ISO-audio relay is a TODO.)

## Deck data — private SysEx IPC (no logs)

Mixxx core holds `playposition` / `duration` / `file_bpm` / `track_samples`.
The **mapping JS** reads them and sends them to the daemon as a private SysEx
(`0x7D` = reserved private/educational ID, never collides with Pioneer or the
mapping spreadsheet). The daemon intercepts them off the relay and **consumes**
them (never forwards `F0 7D…` to the controller):

| Event | SysEx | Daemon action |
|---|---|---|
| Track load | `F0 7D 01 <deck> <samples:4×7> <bpm×100:3> <dur_ms:3> F7` | library lookup → parse waveform → upload |
| Position | `F0 7D 02 <deck> <pos×1e6:3×7> F7` | update playhead → scroll |
| BPM | `F0 7D 03 <deck> <bpm×100:3×7> F7` | xx27 |

Emitter lives in `../Controllers/Pioneer-DDJ-FLX10-script.js`
(`_ipcTrackLoad` / `_ipcPos` / `_ipcBpm`, gated by `USER_CONFIG.enableHidDaemonIpc`).

## The waveform engine (ported from Veezuhz, credited in-file)

`flx10_relay_daemon.py` embeds Veezuhz's screen engine, adapted to write ep5 via
libusb (his hidraw/report-ID machinery dropped — we own the device):

- **§1** Mixxx analysis file → **LE16 PWV5** downsample (150 fps) + library lookup
- **§2** screen packet builders: `xx27` (state/time/BPM), `xx30` (length),
  `xx35` (entry count), `xx36` (waveform), `xx3d` (jog page)
- **§3** `DeckState`, `interp_pos`, `StatePingThread` (xx27, re-enabled — no
  screen.js now), `RefreshThread` (xx36 trickle), `handle_track_load`

PWV5 = 16-bit LE per entry: 5-bit height + 3-bit each low/mid/high band.

## Files

| File | Role |
|---|---|
| `flx10_relay_daemon.py` | **the daemon** — libusb owner + MIDI relay + screen engine |
| `rb_init_sequence.json` | 324 verbatim rekordbox connect frames (ep3/ep5 display init) |
| `flx10_modeswitch.py` | ep0 mode-switch test / confirm (`--then-rebind`, `--keep-audio`) |
| `flx10_rb_replay.py` | full-libusb replay driver (mode switch + init; `--handoff` test) |
| `flx10_unlock_v2.py` | original vendor unlock (control transfers) |
| `flx10_screen_daemon.py` | Veezuhz's original hidraw daemon (reference; superseded) |
| `.venv-relay/` | venv with `pyusb` + `python-rtmidi` (gitignored) |

## Run

FLX10 on the **host** (not the tinywin11 VM — disconnect it there first).
Mixxx closed at start:

```bash
sudo ~/Desktop/Hermes\ Projects/MIXXX-FLX10/HID/.venv-relay/bin/python \
     ~/Desktop/Hermes\ Projects/MIXXX-FLX10/HID/flx10_relay_daemon.py
```

Then: screens light → start Mixxx → **Preferences → Controllers → DDJ-FLX10**
(the virtual relay port) → load the mapping. In **Sound Hardware**, set output to
onboard (`sof-hda-dsp`), **not** the FLX10. Load a track → the jog screens fill.

Mixxx (Flatpak) launch, controllers dir:

```bash
flatpak run --branch=nightly --arch=x86_64 --command=mixxx org.mixxx.Mixxx
# /home/Lou/.var/app/org.mixxx.Mixxx/.mixxx/controllers/
```

Ctrl+C stops the daemon (releases the device; replug to return to normal MIDI mode).

## Status

- ✅ mode switch, screens in HID mode, MIDI relay, LED/VU output, SysEx IPC
- ✅ waveform engine ported + verified offline against the Mixxx library
- ⏳ hardware test of live waveform upload/scroll
- ⏳ FLX10-as-soundcard (ISO-audio relay)
- ⏳ deferred screen extras: `xx39` hotcue, `xx33` album art, `xx2f` beatgrid

## Credit

Waveform/screen protocol + engine: **Veezuhz** —
[Veezuhz/Mixxx_FLX10_Controller_Mapping](https://github.com/Veezuhz/Mixxx_FLX10_Controller_Mapping)
@ `c668a0b0`. Upstream snapshot: `../vendor/Veezuhz-Mixxx_FLX10/`.
