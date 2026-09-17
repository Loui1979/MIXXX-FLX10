#!/usr/bin/env python3
"""
Render a REAL track waveform to the FLX10 jog screen via /dev/hidraw
(usbhid stays bound; unlock over ep0; NO detach, NO libusb endpoint I/O).

Proves the full screen path end-to-end with the ported Veezuhz builders.
  sudo .venv-relay/bin/python flx10_hidraw_render.py [deck]
"""
import os, sys, glob, time, sqlite3
import usb.core, usb.util
import flx10_relay_daemon as D


def hidraw_node(sysfs, iface=5):
    hits = glob.glob(f"{sysfs}:1.{iface}/*/hidraw/hidraw*")
    return ("/dev/" + os.path.basename(hits[0])) if hits else None


def pick_track():
    """First library track that actually has a Waveform-5.0 analysis blob."""
    con = sqlite3.connect(D.MIXXX_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT l.id, l.samplerate, l.duration, l.bpm, l.title "
        "FROM library l WHERE l.duration > 30 AND l.samplerate > 0 "
        "ORDER BY l.id LIMIT 400").fetchall()
    con.close()
    for r in rows:
        pwv5, err = D.waveform_for_track(r["id"], duration_sec=r["duration"])
        if not err and pwv5:
            return r["id"], float(r["duration"]), float(r["bpm"] or 0), r["title"], pwv5
    return None


def main():
    if os.geteuid() != 0:
        sys.exit("need root")
    deck = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    sysfs = D.find_sysfs_device(D.VID, D.PID)
    node = hidraw_node(sysfs)
    print("hidraw:", node)
    if not node:
        sys.exit("no hidraw node — replug FLX10 (usbhid must be bound to iface 5)")

    # unlock via ep0 (no detach)
    dev = usb.core.find(idVendor=D.VID, idProduct=D.PID)
    for wv, wi in D.VENDOR_CMDS:
        try:
            dev.ctrl_transfer(0x40, 3, wv, wi, None)
        except Exception as e:
            print("unlock xfer fail:", e)
    usb.util.dispose_resources(dev)
    time.sleep(0.3)

    picked = pick_track()
    if not picked:
        sys.exit("no analyzed track found in library")
    tid, dur, bpm, title, pwv5 = picked
    print(f"track_id={tid} '{title}' dur={dur:.1f}s bpm={bpm} entries={len(pwv5)//2}")

    fd = os.open(node, os.O_RDWR)
    sent = {"ok": 0, "err": 0}

    def send(pkt):
        b = bytes(pkt)
        if len(b) < 128:
            b = b.ljust(128, b"\x00")
        try:
            os.write(fd, b)
            sent["ok"] += 1
        except OSError as e:
            sent["err"] += 1
            if sent["err"] <= 3:
                print("  hidraw write err:", e)

    st = D.DECKS[deck]
    st.loaded = True
    st.duration = dur
    st.bpm = bpm
    st.pwv5 = bytes(pwv5)
    st.last_pos_val = 0.0
    st.last_pos_ts = time.time()
    st.prev_pos_val = 0.0
    st.prev_pos_ts = time.time()

    print("rendering (xx3d page + xx30 + xx35 + xx36 upload)...")
    D.send_xx3d_display_mode(send, 1)          # waveform page
    D.handle_track_load(send, deck, st.pwv5,
                        label=f"({title})", duration_sec=dur, file_bpm=bpm)
    os.close(fd)
    print(f"done: {sent['ok']} hidraw writes OK, {sent['err']} errors")
    print(">>> LOOK AT DECK", deck, "JOG SCREEN — waveform should be visible.")


if __name__ == "__main__":
    main()
