#!/usr/bin/env python3
"""
Confirm the screens work the Veezuhz way: usbhid STAYS bound to interface 5,
vendor unlock via ep0 control (no detach), screen packet via /dev/hidraw.

REPLUG the FLX10 first so usbhid is freshly bound (a prior libusb run may have
left interfaces unbound). Then:
  sudo .venv-relay/bin/python flx10_hidraw_test.py
"""
import os, sys, glob, time
import usb.core, usb.util
import flx10_relay_daemon as D

# captured rekordbox ep5 init report (rb_init_sequence.json idx 323), 128 bytes
PKT = bytes.fromhex(
    "302100002001000080100000000000000000000000000018fc00"
).ljust(128, b"\x00")[:128]


def hidraw_nodes(sysfs, iface=5):
    # e.g. /sys/bus/usb/devices/3-9:1.5/0003:2B73:0041.XXXX/hidraw/hidrawN
    base = f"{sysfs}:1.{iface}"
    hits = glob.glob(f"{base}/*/hidraw/hidraw*")
    return sorted("/dev/" + os.path.basename(h) for h in hits)


def main():
    if os.geteuid() != 0:
        sys.exit("need root: sudo .venv-relay/bin/python flx10_hidraw_test.py")
    sysfs = D.find_sysfs_device(D.VID, D.PID)
    if not sysfs:
        sys.exit("FLX10 not found")
    print("sysfs:", sysfs)

    drvlink = os.path.join(f"{sysfs}:1.5", "driver")
    drv = os.path.basename(os.readlink(drvlink)) if os.path.islink(drvlink) else "(none)"
    print("interface 5 driver:", drv)

    nodes = hidraw_nodes(sysfs, 5)
    print("hidraw node(s) for interface 5:", nodes or "NONE")
    if not nodes:
        print(">>> No hidraw node — usbhid is NOT bound to interface 5.")
        print(">>> UNPLUG and REPLUG the FLX10, then run this again.")
        return

    # vendor unlock via ep0 — control transfers, no interface detach/claim
    dev = usb.core.find(idVendor=D.VID, idProduct=D.PID)
    print("\nep0 vendor unlock (7 transfers, NO detach):")
    for i, (wv, wi) in enumerate(D.VENDOR_CMDS, 1):
        try:
            dev.ctrl_transfer(0x40, 3, wv, wi, None)
            print(f"  [{i}/7] OK")
        except Exception as e:
            print(f"  [{i}/7] FAIL {e}")
    usb.util.dispose_resources(dev)
    time.sleep(0.3)

    node = nodes[0]
    print(f"\nwriting 30 21 packet to {node}:")
    for prefix, label in [(b"", "128B no reportID"), (b"\x00", "129B reportID00")]:
        try:
            fd = os.open(node, os.O_RDWR)
            n = os.write(fd, prefix + PKT)
            os.close(fd)
            print(f"  [OK]   hidraw write {label}: {n} bytes  <-- screens should react")
        except OSError as e:
            print(f"  [FAIL] hidraw write {label}: {e}")


if __name__ == "__main__":
    main()
