#!/usr/bin/env python3
"""
flx10_live_daemon.py — LIVE, native-driver FLX10 daemon (Sep 18 2026).

Merges the two halves proven separately:
  * flx10_relay_daemon.py  -> MIDI/LED/VU relay + F0 7D IPC parsing  (WORKS)
  * flx10_hidraw_render2.py -> hidraw 0x37/0x38 waveform screens     (WORKS)

Architecture (NO detach, drivers stay bound -> audio + hidraw both alive):
  - ep0 vendor unlock (control transfer, no detach).
  - 323-frame SysEx handshake + keepalive over the NATIVE ALSA MIDI port.
  - Daemon owns a VIRTUAL MIDI port 'DDJ-FLX10 (live)'. Mixxx points its
    controller at THAT port. Daemon:
        Mixxx  --(virtual in)-->  daemon  --(rtmidi)-->  native 20:0  (LEDs/VU)
        native 20:0 --(rtmidi)--> daemon --(virtual out)--> Mixxx     (buttons/jog)
    and CONSUMES F0 7D IPC (never forwards it) to drive the screens.
  - Screens: /dev/hidraw0, 128-byte packets, PREPEND 0x00 report-ID (Veezuhz).
    Track-load -> 0x37/0x38 rekordbox waveform from Mixxx's analysis blob.
    StatePingThread -> xx27 digits/BPM/playhead from live IPC position.

Run (Mixxx CLOSED at start so we can grab the native MIDI port):
  sudo env HOME=/home/Lou SUDO_USER=Lou .venv-relay/bin/python flx10_live_daemon.py
Then: start Mixxx, select controller port 'DDJ-FLX10 (live)', load a track.
Power-cycle the FLX10 first (it wedges).
"""
import os, sys, glob, time, json, threading
import usb.core, usb.util, rtmidi
import flx10_relay_daemon as D   # reuse: waveform engine, screen builders, DECKS, IPC parse

# ---- config ----------------------------------------------------------------
LIVE_VPORT   = "DDJ-FLX10 (live)"   # what Mixxx selects (distinct from the old relay name)
NATIVE_MATCH = "DDJ-FLX10 MIDI 1"   # the kernel ALSA port (client 20)
KEEPALIVE    = [0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01, 0x00, 0x50, 0x00, 0xF7]
INIT_JSON    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rb_init_sequence.json")


def log(m):
    print(f"[live] {m}", flush=True)


def hidraw_node(sysfs, iface=5):
    hits = glob.glob(f"{sysfs}:1.{iface}/*/hidraw/hidraw*")
    return ("/dev/" + os.path.basename(hits[0])) if hits else None


def _find_port(ports, needle):
    for i, name in enumerate(ports):
        if needle in name:
            return i
    return None


class LiveDaemon:
    def __init__(self):
        self.stop = threading.Event()
        self.hid_fd = None
        self.hidlock = threading.Lock()
        self._sent = {"ok": 0, "err": 0}
        # native hardware ports (rtmidi) — daemon <-> FLX10 kernel port
        self.hw_out = rtmidi.MidiOut()     # daemon -> hardware (LEDs/VU/handshake)
        self.hw_in  = rtmidi.MidiIn()      # hardware -> daemon (buttons/jogs)
        self.hw_in.ignore_types(sysex=False, timing=False, active_sense=False)
        # virtual port Mixxx connects to
        self.vout = rtmidi.MidiOut(name=LIVE_VPORT)   # daemon -> Mixxx (buttons/jog)
        self.vin  = rtmidi.MidiIn(name=LIVE_VPORT)    # Mixxx -> daemon (LEDs/VU + F0 7D)
        self.vin.ignore_types(sysex=False, timing=False, active_sense=False)
        self._threads = []
        self._ipc_count = 0
        self._fwd_count = 0

    # ---- hidraw screen sender (render2-proven: prepend 0x00) ----------------
    def send_screen(self, pkt):
        if self.stop.is_set() or self.hid_fd is None:
            return
        b = bytes(pkt)
        b = b.ljust(128, b"\x00") if len(b) < 128 else b
        with self.hidlock:
            try:
                os.write(self.hid_fd, b"\x00" + b)
                self._sent["ok"] += 1
            except OSError as e:
                self._sent["err"] += 1
                if self._sent["err"] <= 3:
                    log(f"hidraw err: {e}")

    # ---- USB unlock + native MIDI handshake --------------------------------
    def unlock_and_handshake(self):
        dev = usb.core.find(idVendor=D.VID, idProduct=D.PID)
        if dev is None:
            sys.exit("FLX10 not found on USB")
        log("ep0: vendor unlock (no detach)")
        for wv, wi in D.VENDOR_CMDS:
            try:
                dev.ctrl_transfer(0x40, 3, wv, wi, None)
            except Exception as e:
                log(f"  unlock fail: {e}")
        usb.util.dispose_resources(dev)
        time.sleep(0.3)

        # open native hardware ports
        op = self.hw_out.get_ports()
        ip = self.hw_in.get_ports()
        oi = _find_port(op, NATIVE_MATCH)
        ii = _find_port(ip, NATIVE_MATCH)
        if oi is None or ii is None:
            sys.exit(f"native '{NATIVE_MATCH}' not found (Mixxx holding it? close Mixxx). "
                     f"out={op} in={ip}")
        self.hw_out.open_port(oi)
        self.hw_in.open_port(ii)
        log(f"native MIDI open: out[{oi}] in[{ii}]")

        # replay the 323-frame ep3 SysEx init over the native port
        seq = json.load(open(INIT_JSON))
        n = 0
        for ep, hexb in seq:
            if ep != "ep3":
                continue
            for msg in D.usbmidi_decode(bytes.fromhex(hexb)):
                self.hw_out.send_message(list(msg))
                n += 1
                time.sleep(0.002)
        log(f"sent {n} init MIDI msgs via native port")

    # ---- open virtual port for Mixxx ---------------------------------------
    def open_vport(self):
        self.vout.open_virtual_port(LIVE_VPORT)
        self.vin.open_virtual_port(LIVE_VPORT)
        self.vin.set_callback(self.on_mixxx_midi)
        self.hw_in.set_callback(self.on_hw_midi)
        log(f"virtual port '{LIVE_VPORT}' open — select it in Mixxx (in+out)")

    # ---- Mixxx -> daemon: consume F0 7D, forward the rest to hardware -------
    def on_mixxx_midi(self, event, data=None):
        msg, _ = event
        if len(msg) >= 3 and msg[0] == 0xF0 and msg[1] == 0x7D:
            self._handle_ipc(msg)
            return
        # everything else (LEDs, VU, real Pioneer SysEx) -> hardware
        try:
            self.hw_out.send_message(list(msg))
            self._fwd_count += 1
        except Exception:
            pass

    # ---- hardware -> daemon -> Mixxx (buttons/jogs) ------------------------
    def on_hw_midi(self, event, data=None):
        msg, _ = event
        try:
            self.vout.send_message(list(msg))
        except Exception:
            pass

    # ---- IPC parse (mirrors D.Relay._handle_ipc; screens via our hidraw) ---
    def _handle_ipc(self, msg):
        if len(msg) < 5 or msg[-1] != 0xF7:
            return
        typ = msg[2]; deck = msg[3]
        st = D.DECKS.get(deck)
        if st is None:
            return
        body = msg[4:-1]
        if typ == 0x01 and len(body) >= 10:            # track load
            samples  = D._unpack7(body[0:4])
            file_bpm = D._unpack7(body[4:7]) / 100.0
            duration = D._unpack7(body[7:10]) / 1000.0
            self._ipc_count += 1
            log(f"IPC track-load deck={deck} samples={samples} bpm={file_bpm} dur={duration:.1f}s")
            self.load_q.put((deck, samples, file_bpm, duration))
        elif typ == 0x02 and len(body) >= 3:           # position
            pos = D._unpack7(body[0:3]) / 1000000.0
            st.prev_pos_val = st.last_pos_val
            st.prev_pos_ts  = st.last_pos_ts
            st.last_pos_val = pos
            st.last_pos_ts  = time.time()
            st.pos = pos
            self._pos_count = getattr(self, "_pos_count", 0) + 1
            if self._pos_count % 50 == 1:
                log(f"  pos deck={deck} pos={pos:.4f} (count={self._pos_count})")
        elif typ == 0x03 and len(body) >= 3:           # bpm
            st.bpm = D._unpack7(body[0:3]) / 100.0

    # ---- track-load worker (heavy 0x37/0x38 upload off the MIDI thread) ----
    def loader_loop(self):
        import queue as _q
        while not self.stop.is_set():
            try:
                item = self.load_q.get(timeout=0.3)
            except _q.Empty:
                continue
            if item is None:
                break
            deck, samples, file_bpm, duration = item
            track_id = D.find_track_id(samples, file_bpm, duration)
            st = D.DECKS[deck]
            if track_id is None:
                log(f"  [deck {deck}] no library match "
                    f"(samples={samples} bpm={file_bpm} dur={duration:.1f})")
                continue
            if st.track_id == track_id and st.has_wave:
                continue
            st.track_id = track_id
            st.bpm = file_bpm
            st.duration = duration
            st.loaded = True
            apath = D.analysis_path_for_track(track_id)
            if not apath:
                log(f"  [deck {deck}] no analysis blob for track_id={track_id}")
                continue
            # seed playhead so StatePingThread xx27 is valid immediately (render2 parity)
            if st.last_pos_ts == 0.0:
                st.last_pos_val = 0.0; st.last_pos_ts = time.time()
                st.prev_pos_val = 0.0; st.prev_pos_ts = time.time()
            # force wave display mode BEFORE the upload — this is what render2 did
            # right before it painted a scrolling waveform (the RB branch alone omits it)
            D.send_xx3d_display_mode(self.send_screen, 1)
            # rekordbox 0x37/0x38 upload via OUR hidraw sender
            D.handle_track_load(self.send_screen, deck, b"",
                                label=f"(track_id={track_id})",
                                duration_sec=duration, file_bpm=file_bpm,
                                analysis_path=apath)
            log(f"  [deck {deck}] waveform uploaded ({self._sent['ok']} ok, {self._sent['err']} err)")

    def keepalive_loop(self):
        while not self.stop.is_set():
            try:
                self.hw_out.send_message(KEEPALIVE)
            except Exception:
                pass
            self.stop.wait(0.2)

    def run(self):
        import queue
        self.load_q = queue.Queue()
        # open hidraw
        sysfs = D.find_sysfs_device(D.VID, D.PID)
        node = hidraw_node(sysfs)
        if not node:
            sys.exit("no hidraw node (drivers not bound? replug + do NOT run the libusb relay)")
        log(f"hidraw: {node}")
        self.unlock_and_handshake()
        self.hid_fd = os.open(node, os.O_RDWR)
        self.open_vport()

        # threads: keepalive, loader, xx27 state ping (reuse D.StatePingThread)
        ka = threading.Thread(target=self.keepalive_loop, daemon=True)
        ld = threading.Thread(target=self.loader_loop, daemon=True)
        ping = D.StatePingThread(self.send_screen)   # xx27 digits/BPM/playhead from DECKS[*]
        for t in (ka, ld):
            t.start()
        ping.start()
        self._threads = [ka, ld, ping]

        log("=== LIVE ===  select 'DDJ-FLX10 (live)' in Mixxx, load a track.")
        log("Ctrl+C to stop.")
        try:
            while not self.stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def close(self):
        self.stop.set()
        try:
            self.load_q.put(None)
        except Exception:
            pass
        time.sleep(0.3)
        for c in (self.vin, self.vout, self.hw_in, self.hw_out):
            try:
                c.close_port()
            except Exception:
                pass
        if self.hid_fd is not None:
            try:
                os.close(self.hid_fd)
            except Exception:
                pass
        log(f"stopped ({self._sent['ok']} screen writes, {self._sent['err']} err, "
            f"{self._fwd_count} midi forwarded)")


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("need root (hidraw + ep0 unlock)")
    LiveDaemon().run()
