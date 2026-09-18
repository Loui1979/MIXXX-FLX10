#!/usr/bin/env python3
"""
FULL rekordbox handshake test, native drivers, no detach, no Mixxx:
  ep0 unlock -> replay 323-frame SysEx init via native ALSA MIDI -> keepalive
  thread -> hidraw waveform. This is the whole sequence rekordbox uses.

  sudo env HOME=/home/Lou SUDO_USER=Lou .venv-relay/bin/python flx10_hidraw_render2.py [deck]
"""
import os, sys, glob, time, json, sqlite3, threading
import usb.core, usb.util, rtmidi
import flx10_relay_daemon as D

KEEPALIVE = [0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01, 0x00, 0x50, 0x00, 0xF7]
INIT_JSON = os.path.join(os.path.dirname(__file__), "rb_init_sequence.json")


def hidraw_node(sysfs, iface=5):
    hits = glob.glob(f"{sysfs}:1.{iface}/*/hidraw/hidraw*")
    return ("/dev/" + os.path.basename(hits[0])) if hits else None


def open_native_midi_out():
    mo = rtmidi.MidiOut()
    ports = mo.get_ports()
    print("MIDI out ports:", ports)
    for i, name in enumerate(ports):
        # native kernel port, not a leftover virtual (type=user) one
        if "DDJ-FLX10" in name and "MIDI" in name:
            mo.open_port(i)
            print(f"opened native MIDI out: {name}")
            return mo
    # fallback: any DDJ-FLX10
    for i, name in enumerate(ports):
        if "DDJ-FLX10" in name:
            mo.open_port(i)
            print(f"opened MIDI out (fallback): {name}")
            return mo
    return None


def pick_track():
    con = sqlite3.connect(D.MIXXX_DB); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id,samplerate,duration,bpm,title FROM library "
                       "WHERE duration>30 AND samplerate>0 ORDER BY id LIMIT 400").fetchall()
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
        sys.exit("no hidraw node")

    # 1) unlock via ep0 (no detach)
    dev = usb.core.find(idVendor=D.VID, idProduct=D.PID)
    for wv, wi in D.VENDOR_CMDS:
        try: dev.ctrl_transfer(0x40, 3, wv, wi, None)
        except Exception as e: print("unlock fail:", e)
    usb.util.dispose_resources(dev)
    time.sleep(0.3)

    # 2) native MIDI out + replay the 323 ep3 SysEx init frames
    mo = open_native_midi_out()
    if mo is None:
        sys.exit("no native DDJ-FLX10 MIDI out port (Mixxx holding it? close Mixxx)")
    seq = json.load(open(INIT_JSON))
    n = 0
    for ep, hexb in seq:
        if ep != "ep3":
            continue
        for msg in D.usbmidi_decode(bytes.fromhex(hexb)):
            mo.send_message(list(msg)); n += 1
            time.sleep(0.002)
    print(f"sent {n} init MIDI messages via native port")

    # 3) keepalive thread
    stop = threading.Event()
    def ka():
        while not stop.is_set():
            mo.send_message(KEEPALIVE)
            stop.wait(0.2)
    threading.Thread(target=ka, daemon=True).start()
    time.sleep(0.5)

    # 4) hidraw waveform
    picked = pick_track()
    if not picked:
        sys.exit("no analyzed track")
    tid, dur, bpm, title, pwv5 = picked
    print(f"track {tid} '{title}' dur={dur:.1f}s entries={len(pwv5)//2}")
    apath = D.analysis_path_for_track(tid)
    print("analysis blob:", apath)
    if not apath:
        print("WARNING: no analysis blob -> will fall back to Serato xx36 (wave won't paint)")
    fd = os.open(node, os.O_RDWR); cnt = {"ok": 0, "err": 0}
    def send(pkt):
        b = bytes(pkt); b = b.ljust(128, b"\x00") if len(b) < 128 else b
        try: os.write(fd, b"\x00" + b); cnt["ok"] += 1   # prepend report-ID 0 (Veezuhz)
        except OSError as e:
            cnt["err"] += 1
            if cnt["err"] <= 3: print("hidraw err:", e)
    st = D.DECKS[deck]
    st.loaded = True; st.duration = dur; st.bpm = bpm; st.pwv5 = bytes(pwv5)
    st.last_pos_val = 0.0; st.last_pos_ts = time.time()
    st.prev_pos_val = 0.0; st.prev_pos_ts = time.time()
    D.send_xx3d_display_mode(send, 1)
    D.handle_track_load(send, deck, st.pwv5, label=title, duration_sec=dur,
                        file_bpm=bpm, analysis_path=apath)
    print(f"hidraw waveform: {cnt['ok']} OK, {cnt['err']} err")

    # rekordbox mode: 0x37/0x38 bulk wave already uploaded above; the firmware
    # self-scrolls it off the xx27 playhead — so drive ONLY xx27 here (no Serato xx36).
    print(">>> 20s: xx27 playhead running. CYCLE TO A WAVE VIEW. Ctrl+C to stop.")
    t0 = time.time()
    try:
        while time.time() - t0 < 20:
            pos = ((time.time() - t0) / dur) % 1.0        # slow advancing playhead
            send(D.build_xx27(D.DECK_BYTES[deck], True, bpm, pos, dur))
            time.sleep(0.033)
    except KeyboardInterrupt:
        pass
    stop.set(); os.close(fd)
    print(f"done ({cnt['ok']} total hidraw writes, {cnt['err']} err)")


if __name__ == "__main__":
    main()
