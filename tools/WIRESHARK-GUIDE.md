# Capturing & Reading DDJ-FLX10 ↔ rekordbox USB Traffic

Goal: see the raw USB packets Pioneer's rekordbox exchanges with the DDJ-FLX10,
so we can reverse-engineer the jog displays, pad LEDs, and any HID/bulk protocol
that isn't documented in the MIDI message list PDF.

rekordbox runs inside the **tiny11 VMware guest**. We capture on the **Linux
host** with `usbmon`, which taps the kernel USB stack *below* VMware's USB
passthrough — so it records the real controller traffic regardless of the VM.

---

## First-time setup (run once, needs sudo — you run these)

```bash
# 1. Load the USB monitoring module
sudo modprobe usbmon

# 2. Make it load automatically on every boot
echo usbmon | sudo tee /etc/modules-load.d/usbmon.conf

# 3. Let your user capture without being root
sudo dpkg-reconfigure wireshark-common      # answer "Yes"
sudo usermod -aG wireshark Lou
sudo setcap cap_net_raw,cap_net_admin+eip /usr/bin/dumpcap
```

Then **log out and back in** (or run `newgrp wireshark` in the shell you'll
capture from) so the group membership applies.

Verify it took:

```bash
id | grep -o wireshark          # should print: wireshark
ls /sys/kernel/debug/usb/usbmon/  # should list 0u 1u 2u ...
getcap /usr/bin/dumpcap         # should show cap_net_admin,cap_net_raw
```

---

## Capturing (every session)

1. Plug the FLX10 into the **USB-C / Thunderbolt** port (it doesn't enumerate
   on plain USB-A on this ThinkPad) and power it on.
2. Confirm the host sees it:
   ```bash
   lsusb | grep -i 2b73        # 2b73 = AlphaTheta/Pioneer DJ
   ```
3. Run the capture helper — it auto-detects the bus + device address:
   ```bash
   cd "/home/Lou/Desktop/Hermes Projects/MIXXX-FLX10"
   ./tools/flx10-capture.sh jog_display_test
   ```
4. In VMware: **VM ▸ Removable Devices ▸ AlphaTheta DDJ-FLX10 ▸ Connect (to VM)**.
   The device disappears from host `lsusb` — that's expected; usbmon still sees
   the bus traffic.
5. In rekordbox, do the ONE thing you want to decode (e.g. load a track and
   watch the jog display, or press a single pad). Keep it short and isolated —
   a 5-second capture of one action is far easier to read than a 2-minute dump.
6. Back in the terminal, press **Ctrl+C** to stop.

Output lands in `captures/jog_display_test.pcapng` plus a
`.devaddr.txt` noting the device address to filter on.

> Tip: capture ONE action per file with a descriptive name
> (`pad1_press.pcapng`, `jog_spin.pcapng`, `load_deck1.pcapng`). Diffing clean
> single-action captures is how you isolate which bytes mean what.

---

## Reading it in Wireshark

Open the file:

```bash
wireshark captures/jog_display_test.pcapng
```

### The layout
- **Top pane** — packet list (one row per URB = USB Request Block).
- **Middle pane** — decoded fields of the selected packet (expandable tree).
- **Bottom pane** — raw hex + ASCII bytes.

### Step 1 — filter to just the FLX10
In the green **display filter** bar at the top, type the device address the
script saved (check the `.devaddr.txt`, e.g. `7`):

```
usb.device_address == 7
```

Press Enter. Now you only see FLX10 traffic.

### Step 2 — understand the direction
Add a column or read the **Source/Destination**:
- `host` → `N.M`  = **OUT** (rekordbox → controller): LEDs, jog displays.
- `N.M` → `host`  = **IN** (controller → rekordbox): jogs, faders, buttons.

Handy direction filters:
```
usb.endpoint_address.direction == 1    # IN  (device → host)
usb.endpoint_address.direction == 0    # OUT (host → device)
```

### Step 3 — find the payload
Click a packet, expand in the middle pane:
- **USB URB** → transfer type tells you the pipe:
  - *Interrupt* → MIDI-style button/knob events and LED writes
  - *Bulk* → likely the jog-display / screen image data (big payloads)
- The actual bytes are under **Leftover Capture Data** (or *URB data*) at the
  bottom. That hex is the message.

### Step 4 — MIDI decoding
FLX10 MIDI rides on **USB-MIDI** (USB Audio Class). Wireshark decodes it:
```
usbaudio                # show only USB-MIDI events
```
Each MIDI event packet shows the cable number + the 3 MIDI bytes
(status, data1, data2) — cross-check those against your spreadsheet
(`FLX10- Midi messages - spreadsheetfed.xlsx`). MIDI-IN rows = the IN packets,
MIDI-OUT rows (incl. Sheet 6 jog/display) = the OUT packets.

### Step 5 — isolate meaning by diffing
1. Capture `nothing.pcapng` (controller idle).
2. Capture `pad1.pcapng` (press only pad 1).
3. Compare — the packets present in #2 but not #1 are pad-1's message.

Export selected packet bytes for notes: right-click a packet ▸
**Copy ▸ … as Hex Stream**, or **File ▸ Export Packet Dissections ▸ As Plain
Text** for a filtered subset.

---

## Useful display filters (cheat sheet)

| Filter | Shows |
|---|---|
| `usb.device_address == N` | just the FLX10 (N from `.devaddr.txt`) |
| `usb.transfer_type == 0x01` | interrupt transfers (buttons/knobs/LEDs) |
| `usb.transfer_type == 0x03` | bulk transfers (screen/jog image data) |
| `usbaudio` | decoded USB-MIDI events |
| `usb.endpoint_address.direction == 1` | IN (controller → rekordbox) |
| `usb.endpoint_address.direction == 0` | OUT (rekordbox → controller) |
| `usb.capdata` | packets that carry a raw data payload |
| `frame.len > 64` | big packets (candidate screen/graphics frames) |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `lsusb` doesn't show 2b73 | Use the USB-C/Thunderbolt port, not USB-A; power-cycle the FLX10 |
| No `usbmonN` interface | `sudo modprobe usbmon`; check `/sys/kernel/debug/usb/usbmon/` |
| dumpcap "permission denied" | Finish first-time setup; log out/in for the group to apply |
| Capture is empty | Wrong bus — re-run the script after plugging on the host so it reads the right bus number |
| Device passed to VM, host lost it | Normal. usbmon still captures. If the script can't find the bus, briefly connect back to host, note the bus, then hand to VM |
| Flooded with unrelated URBs | Filter `usb.device_address == N` |

---

## What we're hunting

- **Jog displays** — almost certainly bulk transfers with framebuffer-like
  payloads (large, periodic). Compare a static screen vs. a spinning platter.
- **Pad / button LEDs** — short OUT interrupt writes; diff on/off states.
- **HID vs MIDI split** — the FLX10 exposes both. MIDI shows under `usbaudio`;
  anything else (SysEx unlock, screen data) shows as raw interrupt/bulk.
  Cross-reference our existing HID/ notes in the project.
