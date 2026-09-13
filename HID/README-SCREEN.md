# Pioneer DDJ-FLX10 — Mixxx Jog Screen Mode (HID)

Companion to the standard MIDI mapping ([README.md](README.md)). This document
covers the **HID screen extension** — running the FLX10's jog wheel LCDs with
real waveforms, BPM, and time display driven from Mixxx, on Linux.

The standard MIDI-only setup gives you a fully functional controller. This
extension adds the visual jog displays that come "for free" with Serato or
Rekordbox.

---

## Quick start

1. Copy the standard MIDI mapping files first (see [README.md](README.md)):
   - `Pioneer-DDJ-FLX10-scripts.js`
   - `Pioneer-DDJ-FLX10-midi.xml`
   - `common-controller-scripts.js`

2. Copy the HID screen files into `~/.mixxx/controllers/`:
   - `PioneerDDJFLX10-screen.js`     — HID screen mapping (xx 27 state ping)
   - `PioneerDDJFLX10-screen.hid.xml` — HID screen descriptor

3. Copy the daemon and unlock script into `~/.mixxx/controllers/`:
   - `flx10_screen_daemon.py`     — the waveform daemon
   - `flx10_unlock_v2.py`         — one-time vendor unlock script

4. In **Mixxx → Preferences → Controllers**, enable BOTH controllers:
   - `DDJ-FLX10 PROD` (the MIDI mapping)
   - `DDJ-FLX10 Screen` (the HID mapping)

5. Plug in the FLX10. Run the **vendor unlock once per plug-in event**:
   ```bash
   pkill -f mixxx 2>/dev/null
   sudo python3 ~/.mixxx/controllers/flx10_unlock_v2.py
   ```

6. Launch Mixxx **with developer mode** (required — see "Why dev mode" below):
   ```bash
   mixxx --developer &
   ```

7. Start the daemon:
   ```bash
   sudo python3 -u ~/.mixxx/controllers/flx10_screen_daemon.py --unlock 2>&1 | tee /tmp/daemon.log
   ```

Load a track in Mixxx → the FLX10 jog screens should display a real waveform,
the track's live BPM, and the remaining time.

---

## What this adds beyond the MIDI mapping

| Feature | Source |
|---|---|
| Jog screen waveform (real, from Mixxx analysis) | Daemon (Python) |
| BPM on jog screen, rate-adjusted | screen.js HID |
| Remaining time on jog screen | screen.js HID |
| Wave shape updates on track change | Daemon (auto-detects via `mixxx.log` tail) |

All the standard buttons, LEDs, jog rotation, and effects from the MIDI
mapping still work normally.

---

## Architecture

```
Mixxx (--developer mode required)
  ├── scripts.js (MIDI mapping)
  │     ├── Buttons / LEDs / jog wheel / effects
  │     ├── SysEx handshake (one-shot + 200ms keepalive) — unlocks HID screen
  │     ├── Serato-mode per-deck SysEx (experimental — see "Open research")
  │     └── console.log("FLX10_TRACK_LOAD ...") → mixxx.log (daemon IPC)
  │
  ├── screen.js (HID screen mapping)
  │     └── Sends xx 27 every 100 ms with live Mixxx data:
  │           [5..7]  BE24 playhead position
  │           [9..12] remaining time (min/sec/ms)
  │           [13..14] BPM (integer + decimal tenths nibble)
  │       Reads Mixxx COs directly — zero log-tail lag
  │
  └── mixxx.log
        ↓
flx10_screen_daemon.py (root, opens /dev/hidrawN non-exclusively)
  ├── Tails mixxx.log for FLX10_TRACK_LOAD events
  ├── Looks up track in mixxxdb.sqlite by samples + file_bpm
  ├── Parses ~/.mixxx/analysis/<id> (zlib + protobuf, in-memory)
  ├── Downsamples Mixxx waveform → Pioneer PWV5 format (LE16 entries)
  ├── Uploads via xx 30/35/39/33/2f init + xx 36 waveform data
  └── 5 Hz xx 36 trickle keeps the firmware's wave buffer alive
```

### Why both `screen.js` AND a daemon

Mixxx's controller-script JavaScript sandbox can't:
- Read binary files (Mixxx's waveform analysis is `~/.mixxx/analysis/<id>` —
  zlib-compressed protobuf)
- Do zlib decompression
- Open raw USB endpoints with arbitrary-sized HID reports

The daemon does all the waveform processing in Python, writes PWV5 bytes
directly to `/dev/hidrawN`. It uses **hidraw** (not exclusive libusb claim)
so Mixxx's own HID screen controller can write to the same device — the
kernel multiplexes.

The screen.js handles only the state-ping packet (xx 27) which carries the
position / time / BPM bytes. Doing that from inside Mixxx gives us **zero
log-tail lag** — the displayed time matches Mixxx exactly.

### Why hidraw and not libusb

Older versions of the daemon claimed USB interface 5 exclusively via libusb.
This blocked Mixxx's HID screen.js from writing to the same interface.
Switched 2026-05-24 to opening `/dev/hidrawN` directly (auto-discovered by
VID `0x2B73` / PID `0x0041`). Kernel-level hidraw allows multiple writers.

Vendor unlock still uses libusb control transfers (which don't require an
interface claim).

---

## Known limitations

### Wave doesn't scroll during playback

The firmware uses xx 27 bytes `[5..7]` (position) for **both** wave-scroll
AND time-display interpretation. When those bytes are non-zero, wave scrolls
but time drifts catastrophically (firmware derives time from position at a
non-1x internal rate). When zero, time matches Mixxx exactly but wave is
static.

**Current default: position bytes = 0** (DJ-priority is exact time). Wave
shape still renders so you can see the track's structure. To re-enable
wave-scroll (and accept time drift), restore the `p[5]/[6]/[7]` encoding in
`PioneerDDJFLX10Screen._buildState` in `PioneerDDJFLX10-screen.js`.

### Time has a fixed ~1s offset from Mixxx UI

Even with position bytes zeroed, the FLX10 time display lagged Mixxx UI by
~1 second. Cause unclear (audio buffer is only 23ms at 44.1kHz, so it
shouldn't be that). Likely a combination of Mixxx UI smoothing + JS timer
tick rounding + firmware display refresh. We compensate by subtracting a
fixed constant `_TIME_OFFSET_SEC` (default 0.96) from the computed
remaining time. Tune to your setup if needed.

### 2-minute wave reset

The firmware drops the waveform display after roughly `BPM² × 0.44 / 128`
seconds of playback (verified empirically across three BPM values). At
174 BPM that's about 104 s; at 188 BPM about 121 s. Independent of how
many entries we upload — it's firmware-side timing.

### `--developer` mode required

The daemon needs to see `FLX10_TRACK_LOAD` lines in `mixxx.log` to know
when a new track loads. Mixxx only writes `Debug [Controller]` lines
(where `console.log` from scripts.js goes) when launched with
`--developer`. Without it, the daemon never picks up new tracks.

The log size grows quickly under `--developer`; the daemon's tail handles
this fine, but if you want to keep the log lean, periodically truncate
`mixxx.log`.

### Vendor unlock on each plug-in event

On Linux, the FLX10 needs a 7-command vendor control-transfer sequence
before its screen LCDs accept HID data. Must be re-run every plug-in. The
daemon's `--unlock` flag does this on startup; the standalone
`flx10_unlock_v2.py` works too.

---

## Serato-mode SysEx (added 2026-05-24 — partial breakthrough)

While analyzing `flx10-driverrutil-then-serato.pcapng`, we discovered Serato
sends **5+ additional SysEx commands** we weren't sending:

```
F0 00 40 05 00 00 04 01  00 11 00 00 02 0e 0e 05  F7   # deck 1 init
F0 00 40 05 00 00 04 01  00 12 00 00 02 0e 0e 05  F7   # deck 2 init
F0 00 40 05 00 00 04 01  00 13 00 00 02 0e 0e 05  F7   # deck 3 init
F0 00 40 05 00 00 04 01  00 14 00 00 02 0e 0e 05  F7   # deck 4 init
F0 00 40 05 00 00 04 01  00 0b 31 00 00 00 00 00  F7   # global B
F0 00 40 05 00 00 04 01  00 0c 00 00 02 0e 0e 05 00 01 F7   # global C
```

Adding these to our scripts.js init **partially decoupled wave-scroll from
time-display**. Before: wave-scroll → 35+ second time drift that grew over
playback. After: wave scrolls AND time is in the right ballpark (~20s
inconsistent wobble). This is the current state.

Search for `_SYSEX_DECK_INIT`, `_SYSEX_GLOBAL_B`, `_SYSEX_GLOBAL_C` in
scripts.js.

### Remaining open question

We still don't fully understand the 20s wobble. Possibilities:
- One of the SysEx payloads above needs the per-track variant (Serato sends
  later per-deck updates like `02 0f 04 0b`, `03 00 05 05` — these appear to
  be per-loaded-track config and we send the generic `02 0e 0e 05` always)
- USB queue depth variability causing packet processing jitter
- Firmware re-syncing its internal time counter occasionally

---

## Files

| Path | Purpose |
|---|---|
| `PioneerDDJFLX10-screen.js` | HID screen mapping (xx 27 with live data) |
| `PioneerDDJFLX10-screen.hid.xml` | HID controller descriptor |
| `flx10_unlock_v2.py` | Vendor unlock (once per plug-in) |
| `flx10_screen_daemon.py` | Waveform daemon (production) |
| `DDJ-FLX10 Screen Protocol/` | Research / experimental scripts + screen-protocol findings |
| `DDJ-FLX10 Screen Protocol/FLX10-SCREEN-PROTOCOL-FINDINGS.md` | Long-form reverse-engineering notes |
| `DDJ-FLX10 Screen Protocol/flx10_prerender_waveform.py` | Standalone PWV5 cache (older path) |
| `findings/FLX10-INTEGRATION-NOTES.md` | General Mixxx integration notes |
| `FLX10-SCREEN-PROTOCOL-FINDINGS.md` | Long-form reverse-engineering notes |

---

## Daemon command-line flags

```bash
sudo python3 flx10_screen_daemon.py [--unlock] [--pwv5-fps N] [--pos-rate N] [--log PATH]
```

- `--unlock` — run vendor-unlock sequence at startup
- `--pwv5-fps N` — waveform entries per second of audio (default 73, empirically
  best for visual wave-vs-playhead sync)
- `--pos-rate N` — position BE24 encoding rate (default 128 ticks/sec)
- `--log PATH` — path to mixxx.log (defaults to `$SUDO_USER`'s `~/.mixxx/mixxx.log`)

---

## Troubleshooting

**Wave doesn't render at all:**
- Did you enable the `DDJ-FLX10 Screen` HID controller in Mixxx prefs?
- Did you run the vendor unlock after plug-in?
- Did you launch Mixxx with `--developer`?

**Wave renders but doesn't scroll:**
- Expected if `[5..7]` is being set to zero (see "Known limitations")
- Or the Serato-mode SysEx experiment hasn't unlocked decoupled mode

**Time drifts from Mixxx:**
- Expected if position bytes are non-zero (see "Known limitations")

**Daemon fails to claim interface 5:**
- It shouldn't anymore — daemon uses hidraw now. If it still fails, check
  `ls /sys/class/hidraw/*/device/uevent | xargs grep -l 2B73` to confirm the
  device is being detected

**Daemon can't write to `/dev/hidraw7`:**
- Run as root, or `sudo chmod a+w /dev/hidraw7` (resets on plug-in)

**Mixxx HID screen.js's writes aren't reaching the device:**
- Restart the daemon — sometimes it needs a fresh handle after Mixxx-side
  changes

---

## Credits

- Original Mixxx MIDI mapping: Victor Pineda (Veezuhz)
- Screen / HID protocol reverse engineering: 2026-05 collaborative effort
- Serato SysEx capture analysis: 2026-05-24
- Pioneer's protocol docs: nonexistent — entire HID layer reverse-engineered
  from USB packet captures of Serato driving the same hardware
