#!/usr/bin/env python3
"""
flx10_rb_replay.py — full-libusb rekordbox replay driver for the DDJ-FLX10.

Reproduces the rekordbox first-connect sequence BYTE-FOR-BYTE from the capture
flx10_rekordbox3.pcapng, all through ONE libusb handle (like rekordbox does —
no ALSA MIDI, no hidraw):

  1. ep0  — 7 vendor CTRL transfers   = MIDI->HID MODE SWITCH
  2. ep4  — read to CONFIRM HID (INT IN only flows while host polls)
  3. ep3  — replay 323 USB-MIDI frames (keepalive, 0B31, 0C, 0A blob, 0301
            mode-enable, LED banks, pad LEDs, ch16 jog-display CCs) VERBATIM
  4. ep5  — the single 128-byte 30 21 00 00 20 01... HID report
  5. keepalive loop (ep3) + ep4 read loop

The init frames come from HID/rb_init_sequence.json (extracted from the capture,
raw USB-MIDI-framed bytes — no re-framing guesswork).

Endpoint addresses (from capture / descriptors):
  ep3 OUT = 0x03 (BULK, MIDI)   ep5 OUT = 0x05 (INT, HID)   ep4 IN = 0x84 (INT)

This OWNS the whole device — snd-usb-audio + usbhid are detached. Mixxx can't
use ALSA MIDI/audio while this runs. Coexistence is a later problem; first we
prove the screens light.

Run as root, Mixxx closed, FLX10 on host (not passed to the VM).
"""

import os
import sys
import time
import glob
import json
import errno
import argparse
import threading
import usb.core
import usb.util

VID = 0x2B73
PID = 0x0041

EP3_OUT = 0x03   # BULK  — MIDI OUT (SysEx, LEDs, jog CCs)
EP5_OUT = 0x05   # INT   — vendor HID OUT (the 30 21 packet)
EP4_IN  = 0x84   # INT   — vendor HID IN (state stream = mode confirm)

VENDOR_CMDS = [
    (0x0100, 0xC028), (0x0000, 0xC029), (0x0200, 0xC013), (0x0000, 0xC02B),
    (0x0100, 0xC026), (0x0000, 0xC01D), (0x0100, 0xC027),
]

# The one keepalive frame (USB-MIDI framed) — resent every ~200ms after init.
KEEPALIVE_FRAME = bytes.fromhex("04f000400405000004040100075000f7")

HERE = os.path.dirname(os.path.abspath(__file__))
INIT_JSON = os.path.join(HERE, "rb_init_sequence.json")


def log(msg):
    print(f"[rb-replay] {msg}", flush=True)


# ===== kernel driver detach (sysfs, like flx10_unlock_v2.py) =================

def find_sysfs_device(vid, pid):
    for vf in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            if int(open(vf).read().strip(), 16) != vid:
                continue
            if int(open(vf.replace("idVendor", "idProduct")).read().strip(), 16) == pid:
                return os.path.dirname(vf)
        except (OSError, ValueError):
            continue
    return None


def unbind_all_kernel_drivers(sysfs):
    """Unbind snd-usb-audio + usbhid from every FLX10 interface so libusb owns
    the device. Returns list of (interface, driver) we unbound (for rebind)."""
    unbound = []
    for intf_dir in sorted(glob.glob(f"{sysfs}/*:*")):
        drv_link = os.path.join(intf_dir, "driver")
        if not os.path.islink(drv_link):
            continue
        drv = os.path.basename(os.readlink(drv_link))
        intf = os.path.basename(intf_dir)
        unbind_path = f"/sys/bus/usb/drivers/{drv}/unbind"
        try:
            with open(unbind_path, "w") as f:
                f.write(intf)
            log(f"  unbound {intf} from {drv}")
            unbound.append((intf, drv))
        except OSError as e:
            log(f"  unbind {intf} ({drv}) FAILED: {e}")
    return unbound


def rebind_kernel_drivers(unbound):
    """Re-attach the kernel drivers we unbound (hand device back to the kernel:
    snd-usb-audio -> Mixxx MIDI+audio, usbhid -> /dev/hidraw0)."""
    for intf, drv in unbound:
        bind_path = f"/sys/bus/usb/drivers/{drv}/bind"
        try:
            with open(bind_path, "w") as f:
                f.write(intf)
            log(f"  rebound {intf} -> {drv}")
        except OSError as e:
            log(f"  rebind {intf} ({drv}) FAILED: {e}")


# ===== USB helpers ===========================================================

def send_mode_switch(dev):
    log("ep0: sending 7 vendor mode-switch transfers (MIDI -> HID)")
    for i, (wv, wi) in enumerate(VENDOR_CMDS, 1):
        try:
            dev.ctrl_transfer(0x40, 3, wv, wi, None)
            log(f"  [{i}/7] wValue={wv:04x} wIndex={wi:04x} OK")
        except usb.core.USBError as e:
            log(f"  [{i}/7] wValue={wv:04x} wIndex={wi:04x} FAIL: {e}")
        time.sleep(0.005)


def confirm_hid(dev, secs):
    log(f"ep4: reading {secs}s to confirm HID state stream")
    n = 0
    first = None
    deadline = time.time() + secs
    while time.time() < deadline:
        try:
            data = dev.read(EP4_IN, 64, timeout=200)
            if len(data):
                n += 1
                if first is None:
                    first = bytes(data)[:16].hex()
        except usb.core.USBError as e:
            if e.errno == errno.ETIMEDOUT:
                continue
            break
    log(f"  ep4 IN: {n} reports" + (f", first={first}" if first else " (NONE)"))
    return n


def replay_init(dev, seq, frame_delay):
    log(f"ep3/ep5: replaying {len(seq)} init frames verbatim")
    n3 = n5 = 0
    for ep, hexbytes in seq:
        data = bytes.fromhex(hexbytes)
        try:
            if ep == "ep3":
                dev.write(EP3_OUT, data, timeout=500)
                n3 += 1
            elif ep == "ep5":
                dev.write(EP5_OUT, data, timeout=500)
                n5 += 1
                log(f"  ep5 HID report sent ({len(data)} bytes)")
        except usb.core.USBError as e:
            log(f"  {ep} write FAILED: {e}")
        if frame_delay:
            time.sleep(frame_delay)
    log(f"  replayed ep3={n3} ep5={n5}")


# ===== keepalive + ep4 reader threads =======================================

class Runner:
    def __init__(self, dev):
        self.dev = dev
        self.stop = threading.Event()

    def keepalive_loop(self):
        while not self.stop.is_set():
            try:
                self.dev.write(EP3_OUT, KEEPALIVE_FRAME, timeout=500)
            except usb.core.USBError:
                pass
            self.stop.wait(0.2)

    def ep4_loop(self):
        while not self.stop.is_set():
            try:
                self.dev.read(EP4_IN, 64, timeout=200)
            except usb.core.USBError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-secs", type=float, default=2.0)
    ap.add_argument("--frame-delay", type=float, default=0.002,
                    help="delay between init frames (s), default 2ms")
    ap.add_argument("--no-keepalive", action="store_true")
    ap.add_argument("--handoff", action="store_true",
                    help="after lighting screens, rebind kernel drivers "
                         "(snd-usb-audio+usbhid) and hold — tests if HID mode "
                         "survives so Mixxx can coexist. Watch the screens.")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("Need root. Re-run with sudo.")

    if not os.path.exists(INIT_JSON):
        sys.exit(f"Missing {INIT_JSON} — extract it from the capture first.")
    seq = json.load(open(INIT_JSON))

    sysfs = find_sysfs_device(VID, PID)
    if not sysfs:
        sys.exit("FLX10 not in sysfs. Plug it in.")
    log(f"sysfs: {sysfs}")

    log("detaching kernel drivers (snd-usb-audio, usbhid)...")
    unbound = unbind_all_kernel_drivers(sysfs)
    time.sleep(0.3)

    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("pyusb can't see the device.")

    # Detach anything still attached at the libusb level, then claim.
    try:
        dev.set_configuration()
    except usb.core.USBError as e:
        log(f"set_configuration note: {e}")

    # 1. mode switch
    send_mode_switch(dev)
    time.sleep(0.2)

    # 2. confirm HID
    n = confirm_hid(dev, args.confirm_secs)
    if n == 0:
        log("WARNING: no ep4 IN before init — continuing anyway (screens may show wheels)")

    # 3+4. replay init (ep3 frames + ep5 packet) verbatim
    replay_init(dev, seq, args.frame_delay)

    if args.handoff:
        # HANDOFF TEST: release the device back to the kernel and see if the
        # HID display mode is sticky. If the screens stay lit after rebind,
        # Mixxx (snd-usb-audio) + a hidraw daemon (usbhid ep4/ep5) can coexist.
        log("HANDOFF: releasing libusb, rebinding kernel drivers...")
        usb.util.dispose_resources(dev)
        time.sleep(0.3)
        rebind_kernel_drivers(unbound)
        log("=== HANDOFF DONE ===")
        log(">>> WATCH THE SCREENS: still lit = mode is STICKY (coexistence OK)")
        log(">>> Now start Mixxx — it should see DDJ-FLX10 MIDI + audio again.")
        log(">>> hidraw for ep4/ep5 is back (usbhid rebound) -> screen daemon can use it.")
        log("Holding (no libusb activity). Ctrl+C to exit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log("done")
        return

    # 5. keepalive + ep4 read, hold the session open
    runner = Runner(dev)
    t1 = threading.Thread(target=runner.ep4_loop, daemon=True)
    t1.start()
    if not args.no_keepalive:
        t2 = threading.Thread(target=runner.keepalive_loop, daemon=True)
        t2.start()

    log("session live — keepalive + ep4 running. Ctrl+C to stop.")
    log(">>> LOOK AT THE JOG SCREENS NOW <<<")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        runner.stop.set()
        log("stopping")


if __name__ == "__main__":
    main()
