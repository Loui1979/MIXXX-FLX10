#!/usr/bin/env python3
"""
flx10_modeswitch.py — put the DDJ-FLX10 into HID mode and CONFIRM it.

Finding (Sep 2026, from flx10_rekordbox3.pcapng, 386k pkts):
  The 7 vendor CTRL transfers on ep0 are the MIDI->HID MODE SWITCH, not an
  "audio unlock." In the rekordbox capture:
    pkt 116622  ep4 IN stream STARTS   (HID state reports)
    pkt 116646  ep0 vendor transfers   (the switch — 24 pkts later)
    pkt 126553  ep3 SysEx  (~10k pkts later = display content)
    pkt 132675  ep5 OUT    (~16k pkts later = the 30 21 packet)
  So ep4 IN + ep0 vendor happen together at connect; SysEx/ep5 are display
  data sent AFTER we're already in HID mode.

Difference vs flx10_unlock_v2.py:
  - unlock_v2 REBINDS snd-usb-audio after the transfers (step 3). That re-probe
    may kick the controller back OUT of HID mode. This script does NOT rebind by
    default (audio card stays unbound during the HID session; rebind later).
  - After the transfers it OPENS /dev/hidraw0 and READS it, counting ep4 IN
    bytes. That both CONFIRMS HID mode and generates the ep4 traffic (INT IN
    only flows when the host polls — so nobody reading = ~0 pkts even in HID).

Exit code 0 = HID stream confirmed. 2 = no ep4 data (not in HID mode / not
reading). Run as root, Mixxx closed.

Usage:
  sudo python3 flx10_modeswitch.py                 # switch + confirm, no rebind
  sudo python3 flx10_modeswitch.py --then-rebind   # switch, confirm, rebind
                                                    #   audio, re-confirm (tests
                                                    #   whether rebind breaks HID)
  sudo python3 flx10_modeswitch.py --read-secs 3   # how long to sample ep4
"""

import os
import sys
import time
import glob
import errno
import argparse
import usb.core
import usb.util

VID = 0x2B73
PID = 0x0041
HIDRAW = "/dev/hidraw0"

# The mode switch — 7 vendor OUT control transfers (bmRequestType=0x40,
# bRequest=0x03). Verified byte-for-byte against rekordbox capture.
VENDOR_CMDS = [
    (0x0100, 0xC028),
    (0x0000, 0xC029),
    (0x0200, 0xC013),
    (0x0000, 0xC02B),
    (0x0100, 0xC026),
    (0x0000, 0xC01D),
    (0x0100, 0xC027),
]


def find_sysfs_device(vid, pid):
    for vendor_file in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            with open(vendor_file) as f:
                v = int(f.read().strip(), 16)
            if v != vid:
                continue
            product_file = vendor_file.replace("idVendor", "idProduct")
            with open(product_file) as f:
                p = int(f.read().strip(), 16)
            if p == pid:
                return os.path.dirname(vendor_file)
        except (OSError, ValueError):
            continue
    return None


def interfaces_bound_to(sysfs_dev, driver_name):
    intfs = []
    for intf_dir in sorted(glob.glob(f"{sysfs_dev}/*:*")):
        drv_link = os.path.join(intf_dir, "driver")
        if os.path.islink(drv_link):
            drv = os.path.basename(os.readlink(drv_link))
            if drv == driver_name:
                intfs.append(os.path.basename(intf_dir))
    return intfs


def write_sysfs(path, value):
    try:
        with open(path, "w") as f:
            f.write(value)
        return True, None
    except OSError as e:
        return False, str(e)


def send_mode_switch():
    print("[2] Sending vendor mode-switch handshake (MIDI -> HID)...")
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        print("ERROR: pyusb can't see the device. Aborting.")
        sys.exit(1)
    ok = 0
    for i, (wValue, wIndex) in enumerate(VENDOR_CMDS, 1):
        try:
            dev.ctrl_transfer(0x40, 3, wValue, wIndex, None)
            print(f"    [{i}/7] vendor OUT wValue=0x{wValue:04X} "
                  f"wIndex=0x{wIndex:04X}  OK")
            ok += 1
        except usb.core.USBError as e:
            print(f"    [{i}/7] vendor OUT wValue=0x{wValue:04X} "
                  f"wIndex=0x{wIndex:04X}  FAIL: {e}")
        time.sleep(0.005)
    usb.util.dispose_resources(dev)
    return ok


def confirm_hid_stream(read_secs, label="confirm"):
    """Open /dev/hidraw0 and read ep4 IN reports for read_secs. Returns byte
    count + packet count. INT IN only flows while the host polls, so reading
    here both proves HID mode AND generates the ep4 traffic."""
    print(f"[{label}] Reading {HIDRAW} for {read_secs}s to confirm ep4 IN "
          f"HID stream...")
    try:
        fd = os.open(HIDRAW, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        print(f"    can't open {HIDRAW}: {e}")
        print(f"    (is the vendor HID iface bound to hidraw? is perms 666?)")
        return 0, 0
    total_bytes = 0
    pkts = 0
    first_hex = None
    deadline = time.time() + read_secs
    try:
        while time.time() < deadline:
            try:
                data = os.read(fd, 512)
                if data:
                    pkts += 1
                    total_bytes += len(data)
                    if first_hex is None:
                        first_hex = data[:16].hex()
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    time.sleep(0.002)
                    continue
                print(f"    read error: {e}")
                break
    finally:
        os.close(fd)
    print(f"    ep4 IN: {pkts} reports, {total_bytes} bytes")
    if first_hex:
        print(f"    first report: {first_hex}")
    return pkts, total_bytes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--then-rebind", action="store_true",
                    help="after confirming HID, rebind snd-usb-audio and "
                         "re-confirm (tests if rebind breaks HID mode)")
    ap.add_argument("--read-secs", type=float, default=2.0,
                    help="seconds to sample ep4 IN (default 2)")
    ap.add_argument("--keep-audio", action="store_true",
                    help="do NOT unbind snd-usb-audio (try mode switch with the "
                         "audio driver still attached)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("Need root. Re-run with sudo.")
        sys.exit(1)

    print(f"Looking for FLX10 (VID=0x{VID:04X} PID=0x{PID:04X})...")
    sysfs = find_sysfs_device(VID, PID)
    if not sysfs:
        print("ERROR: FLX10 not present in sysfs. Plug it in.")
        sys.exit(1)
    print(f"  sysfs:  {sysfs}")

    audio_intfs = interfaces_bound_to(sysfs, "snd-usb-audio")
    print(f"  snd-usb-audio bound to: {audio_intfs}")

    if not args.keep_audio and audio_intfs:
        print("\n[1] Unbinding snd-usb-audio (so pyusb can drive ep0)...")
        for intf in audio_intfs:
            ok, err = write_sysfs("/sys/bus/usb/drivers/snd-usb-audio/unbind", intf)
            print(f"    {intf}: {'OK' if ok else f'FAIL ({err})'}")
        time.sleep(0.3)
    else:
        print("\n[1] Keeping snd-usb-audio bound (--keep-audio).")

    send_mode_switch()
    time.sleep(0.3)

    # [3] Confirm — NO rebind. This is the key difference from unlock_v2.
    pkts, nbytes = confirm_hid_stream(args.read_secs, label="3")
    verdict_hid = pkts > 0

    if args.then_rebind and audio_intfs:
        print("\n[4] Rebinding snd-usb-audio (testing if it breaks HID)...")
        for intf in audio_intfs:
            ok, err = write_sysfs("/sys/bus/usb/drivers/snd-usb-audio/bind", intf)
            print(f"    {intf}: {'OK' if ok else f'FAIL ({err})'}")
        time.sleep(0.5)
        pkts2, _ = confirm_hid_stream(args.read_secs, label="5")
        print("\n=== REBIND TEST RESULT ===")
        print(f"    before rebind: {pkts} ep4 reports")
        print(f"    after  rebind: {pkts2} ep4 reports")
        if pkts > 0 and pkts2 == 0:
            print("    >>> REBIND KILLS HID MODE. Do not rebind during session.")
        elif pkts2 > 0:
            print("    >>> HID survives rebind. Rebind is safe.")

    print("\n=== VERDICT ===")
    if verdict_hid:
        print(f"    HID MODE CONFIRMED — ep4 IN streaming ({pkts} reports).")
        sys.exit(0)
    else:
        print("    NO ep4 IN DATA — not in HID mode, or hidraw not readable.")
        print("    Check: (a) vendor HID iface bound to hidraw, (b) perms on")
        print(f"    {HIDRAW}, (c) whether the mode switch actually took.")
        sys.exit(2)


if __name__ == "__main__":
    main()
