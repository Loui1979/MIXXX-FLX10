# tools/ — FLX10 USB capture & decode

Reverse-engineering the DDJ-FLX10 ↔ rekordbox USB protocol so we can teach Mixxx
the undocumented bits (jog displays, LED banks, unlock, keep-alive).

**Reference source = rekordbox.** We deliberately standardized on rekordbox
captures (over Serato/Lexicon) because rekordbox drives the FLX10's displays and
LEDs with the richest, most complete feature set — its traffic reveals the most
of the undocumented protocol.

rekordbox runs in the tiny11 VMware guest; we capture on the **Linux host** with
`usbmon`, which taps the USB stack *below* VMware's passthrough. Nothing to
install in the VM. Full method: `usb-packet-capture` skill. FLX10 specifics:
`mixxx-flx10` skill.

## Files

| File | What |
|---|---|
| `flx10-capture.sh` | Auto-detects the FLX10's bus + device address, captures to `../captures/<name>.pcapng`, writes a `.devaddr.txt` for later filtering. |
| `flx10-parse.py` | **Pure-Python** pcapng decoder (no tshark, no root). Endpoint summary or `--sysex` SysEx reassembly. |
| `flx10-decode.sh` | tshark-based grouped dump (control / MIDI / interrupt / bulk). Needs `sudo apt install tshark`. |
| `WIRESHARK-GUIDE.md` | GUI + display-filter cheat sheet and read workflow. |

## Quick start

**First-time setup (run once, needs sudo):**
```bash
sudo modprobe usbmon
echo usbmon | sudo tee /etc/modules-load.d/usbmon.conf
sudo dpkg-reconfigure wireshark-common      # answer "Yes"
sudo usermod -aG wireshark Lou
sudo setcap cap_net_raw,cap_net_admin+eip /usr/bin/dumpcap
# then log out/in (or: newgrp wireshark)
```

**Capture (GUI — the normal way):** open Wireshark, pick interface `usbmon3`
(bus 3 = where the FLX10 lives), start, do the action, stop, filter
`usb.device_address == N`.

**Capture (script):**
```bash
lsusb | grep -i 2b73                                   # confirm host sees it
./tools/flx10-capture.sh load_deck1                    # start
# → VMware: VM ▸ Removable Devices ▸ DDJ-FLX10 ▸ Connect
# → do ONE action in rekordbox, then Ctrl+C
```

**Decode (headless, no tshark):**
```bash
python3 tools/flx10-parse.py captures/load_deck1.pcapng          # endpoint map
python3 tools/flx10-parse.py captures/load_deck1.pcapng --sysex  # distinct SysEx
```

Or just hand the `.pcapng` to the agent and it'll decode + write a
`captures/FINDINGS_*.md`.

## dumpcap sudo gotcha

`sudo dumpcap -w ~/f.pcapng` → "Permission denied" (drops privileges before
opening the file). Write to `/tmp` then `sudo chown Lou:Lou` + move, OR finish
the setcap setup above and run without sudo.

## What we've learned so far

See `../captures/FINDINGS_rekordbox3.md`. Headlines:

- **Audio unlock = VERIFIED.** 7 vendor control transfers (`bmRequestType=0x40,
  bRequest=0x03`) match `HID/flx10_unlock_v2.py`'s `VENDOR_CMDS` exactly, in
  order. The unlock script is confirmed correct against real rekordbox.
- **MIDI** = standard USB-MIDI on BULK ep2 IN / ep3 OUT.
- **Jog displays / hi-res jog** = vendor HID (INT ep4 IN / ep5 OUT, usage page
  0xFFA0) — confirms HID stays a separate Mixxx controller.
- **Keep-alive** SysEx `F0 00 40 05 00 00 04 01 00 50 00 F7` sent repeatedly.
- The FLX10's **soundcard floods captures** (ISO ep1, ~270k pkts) — capture
  connect-only with no audio playing.

## Endpoint reference (this device)

| Endpoint | Type | Dir | Purpose |
|---|---|---|---|
| ep1 | ISO | IN/OUT | soundcard (ignore) |
| ep2 | BULK | IN | MIDI in (controls) |
| ep3 | BULK | OUT | MIDI out (LEDs/displays), Pioneer SysEx |
| ep4 | INT | IN | vendor HID hi-res state (jogs/faders) |
| ep5 | INT | OUT | vendor HID output report |
| ep0 | CTRL | — | descriptors, UAC audio setup, **vendor unlock** |
