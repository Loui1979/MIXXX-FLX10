#!/usr/bin/env python3
"""
Clean ep5 (jog-screen HID OUT) probe — NO Mixxx, NO MIDI relay.

Isolates the one question the relay can't answer through the noise:
  does the FLX10 accept HID-OUT writes on ep5, and at what size/ordering?

Sequence (matches the rekordbox capture ordering):
  own device -> ep0 mode-switch -> START ep4-IN polling -> THEN try ep5 OUT.

Run:  sudo .venv-relay/bin/python flx10_ep5_probe.py
"""
import sys, time, threading
import usb.core, usb.util

# reuse the daemon's proven device-acquisition helpers + constants
import flx10_relay_daemon as D

EP5_OUT = D.EP5_OUT
EP4_IN  = D.EP4_IN

# the single captured rekordbox ep5 init report (from rb_init_sequence.json idx 323)
INIT_30_21 = bytes.fromhex(
    "302100002001000080100000000000000000000000000018fc000000000000"
    "00000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000"  # padded/truncated below
)[:128]


def dump_endpoints(dev):
    print("\n=== USB config / endpoint descriptors ===")
    for cfg in dev:
        for intf in cfg:
            print(f"interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting} "
                  f"class={intf.bInterfaceClass:#04x}")
            for ep in intf:
                addr = ep.bEndpointAddress
                d = "IN" if addr & 0x80 else "OUT"
                ttype = usb.util.endpoint_type(ep.bmAttributes)
                tname = {0:"CTRL",1:"ISO",2:"BULK",3:"INT"}.get(ttype, ttype)
                print(f"   ep {addr:#04x} {d:3} {tname:4} "
                      f"wMaxPacketSize={ep.wMaxPacketSize} bInterval={ep.bInterval}")


def try_ep5(dev, data, label):
    try:
        n = dev.write(EP5_OUT, data, timeout=1000)
        print(f"   [OK]   ep5 {label}: wrote {n} bytes")
        return True
    except usb.core.USBError as e:
        print(f"   [FAIL] ep5 {label}: {e}")
        return False


def main():
    import os
    if os.geteuid() != 0:
        sys.exit("Need root: sudo .venv-relay/bin/python flx10_ep5_probe.py")

    sysfs = D.find_sysfs_device(D.VID, D.PID)
    if not sysfs:
        sys.exit("FLX10 not in sysfs (plugged in? on host, not VM?)")
    print(f"sysfs {sysfs}; detaching kernel drivers")
    D.unbind_all(sysfs)
    time.sleep(0.3)

    dev = usb.core.find(idVendor=D.VID, idProduct=D.PID)
    if dev is None:
        sys.exit("pyusb can't see device")
    dev.set_configuration()
    dump_endpoints(dev)

    print("\n=== ep0: 7 vendor mode-switch transfers ===")
    D.mode_switch(dev)
    time.sleep(0.3)

    # --- NEW: bring interface 5 (HID: ep5 OUT + ep84 IN) up ourselves ---
    HID_IF = 5
    try:
        if dev.is_kernel_driver_active(HID_IF):
            dev.detach_kernel_driver(HID_IF)
            print(f"detached kernel driver from interface {HID_IF}")
    except Exception as e:
        print(f"detach iface {HID_IF}: {e}")
    try:
        usb.util.claim_interface(dev, HID_IF)
        print(f"claimed interface {HID_IF}")
    except Exception as e:
        print(f"claim iface {HID_IF}: {e}")
    # HID SET_IDLE(0) on interface 5 — makes many HID devices start reporting
    try:
        dev.ctrl_transfer(0x21, 0x0A, 0x0000, HID_IF, None)  # bmReq=class/iface/OUT, SET_IDLE
        print("SET_IDLE(0) iface 5 OK")
    except Exception as e:
        print(f"SET_IDLE: {e}")
    time.sleep(0.2)

    # START ep4 IN polling FIRST (capture order: ep4 streams before ep5 OUT)
    stop = threading.Event()
    ep4_count = [0]
    def ep4_loop():
        while not stop.is_set():
            try:
                dev.read(EP4_IN, 64, timeout=200)
                ep4_count[0] += 1
            except usb.core.USBError:
                pass
    t = threading.Thread(target=ep4_loop, daemon=True)
    t.start()
    time.sleep(0.5)
    print(f"\n=== ep4 IN polling active ({ep4_count[0]} reports in 0.5s) ===")

    print("\n=== ep5 OUT write attempts (30 21 init packet) ===")
    pkt128 = INIT_30_21[:128].ljust(128, b"\x00")
    pkt256 = INIT_30_21[:128].ljust(256, b"\x00")
    pkt64  = INIT_30_21[:64].ljust(64, b"\x00")
    r64  = try_ep5(dev, pkt64,  "64 bytes")
    r128 = try_ep5(dev, pkt128, "128 bytes")
    r256 = try_ep5(dev, pkt256, "256 bytes")
    # with a report-ID 0x00 prefix (HID convention)
    r128p = try_ep5(dev, b"\x00" + pkt128, "128 bytes +reportID00 (129)")

    print("\n=== result ===")
    print(f"  ep4 reports seen: {ep4_count[0]}")
    print(f"  ep5 64B={r64} 128B={r128} 256B={r256} 129B(+rid)={r128p}")
    if any([r64, r128, r256, r128p]):
        print("  -> ep5 ACCEPTS writes; winning size(s) above. Screens should react.")
    else:
        print("  -> ep5 rejects ALL sizes: not a size bug. Likely ordering/state or")
        print("     wrong endpoint. Check descriptor above (is 0x05 really INT OUT?).")

    stop.set()
    time.sleep(0.3)
    usb.util.dispose_resources(dev)


if __name__ == "__main__":
    main()
