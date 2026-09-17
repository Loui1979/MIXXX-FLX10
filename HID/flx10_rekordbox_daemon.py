#!/usr/bin/env python3
"""
flx10_rekordbox_daemon.py - rekordbox-mode jog display driver.

Mirrors tinywin11 rekordbox USB capture exactly:
- ep5 one-shot: single 128-byte INT OUT (deck 0x30, type 0x21)
- then firmware opens ep4 IN for replies

Listens to:
  - /tmp/flx10_rekordbox.sock  (your PioneerDDJFLX10-script.js _sendSysex hook)
  - tail of mixxx.log         (FLX10_TRACK_LOAD / FLX10_POS)

Translates into rekordbox ep5 HID (NOT serato xx30/36/39).
See HID/REKORDBOX-PROTOCOL.md for packet format.
"""

import os, sys, time, json, socket, struct, logging, threading, argparse, subprocess
import fcntl, errno

# ===== Config =====

# rekordbox endpoint map (tiny rekordbox capture):
# ep1 ISO audio (ignored here), ep2 MIDI IN, ep3 MIDI OUT (sysex lives here)
# ep4 INT IN (firmware replies), ep5 INT OUT (our writes)
HIDRAW_DEFAULT = "/dev/hidraw0"
SOCKET_PATH = "/tmp/flx10_rekordbox_sock"
DEFAULT_MIXXX_LOG = "/home/Lou/.var/app/org.mixxx.Mixxx/.mixxx/mixxx.log"

# rekordbox deck header byte
RKBOX_DECK_BYTE = {1: 0x30, 2: 0x31, 3: 0x32, 4: 0x33}

# rekordbox ep5 init (mirrors REKORDBOX_EP5_OUT from serato daemon, reused here)
RKBOX_EP5_INIT = bytes.fromhex(
    "30210000200100008010000000000000"
    "0000000000000018fc00000000000000"
    + "00" * 96
)

# rekordbox ep5 waveform/position packet template (from FINDINGS_rekordbox3.md)
# deck byte + type 0x21 + frame data
RKBOX_EP5_PLAYHEAD = 0x36   # position counter in ep5
RKBOX_EP5_WAVE     = 0x35   # waveform entry count
RKBOX_EP5_BLOB     = 0x0A   # waveform blob payload (in sysex, not ep5 - ep5 carries ptr)

logging.basicConfig(
    level=logging.INFO,
    format="[rkbox-daemon %(levelname)s] %(message)s",
)
log = logging.getLogger("flx10_rekordbox")


# ===== USB/hidraw =====
# Uses the same vendor CTRL unlock as the serato daemon (verified = Veezuhz),
# but sends rekordbox ep5 instead of serato xx30/36/39.

class DeckState:
    def __init__(self):
        self.bpm          = 0.0
        self.file_bpm     = 0.0
        self.pos          = 0.0     # 0..1
        self.track_id     = None
        self.duration     = 0.0
        self.pwv5         = b""
        self.wave_blob    = b""     # last 00 0A sysex blob (your _sendSysex)
        self.loaded       = False


DECKS = {1: DeckState(), 2: DeckState(), 3: DeckState(), 4: DeckState()}


# ===== Socket listener (feeds from your JS _sendSysex hook) =====

def socket_server():
    "Listen for JSON from your PioneerDDJFLX10-script.js _sendSysex hook."
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    srv.listen(1)
    srv.settimeout(1.0)
    log.info("socket listener on %s — your _sendSysex hook should connect here", SOCKET_PATH)
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=socket_consumer, args=(conn,), daemon=True).start()


def socket_consumer(conn):
    buf = b""
    while True:
        try:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        except OSError:
            break
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("socket: bad JSON")
                continue
            handle_sysex(msg)


def handle_sysex(msg):
    """Your _sendSysex hook sends: {"deck":1, "name":"RKBOX_MODE_CONFIG", "sysex":[...]}"""
    deck = msg.get("deck", 0)
    name = msg.get("name", "")
    sysex = msg.get("sysex", [])
    if deck not in DECKS:
        log.warning("socket: unknown deck %r", deck)
        return
    st = DECKS[deck]
    # rekordbox MODE_CONFIG (00 0A) carries the waveform blob bytes
    if "0A" in name or name == "RKBOX_MODE_CONFIG":
        # bytes[10:] = the blob; decode length at [10][11] (LE16) per capture
        blob = bytes(sysex[10:50]) if len(sysex) > 50 else b""
        n_entries = blob[0] | (blob[1] << 8)  # per FINDINGS: entry count at blob[0:2]
        st.wave_blob = blob
        log.info("deck %d: rekordbox 00 0A blob, %d entries", deck, n_entries)
    log.debug("deck %d sysex %s: %s", deck, name, " ".join(f"{b:02x}" for b in sysex[:16]))


# ===== ep5 HID writers =====
# ALL writes use rekordbox ep5 format here. No serato xx30/35/36/39.

def send_rekordbox_ep5_init(ep):
    pkt = bytearray(128)
    pkt[:128] = RKBOX_EP5_INIT
    send_pkt(ep, pkt)
    log.info("ep5 replay (rekordbox mode-init)")


def send_rekordbox_playhead(ep, deck, pos01, duration_sec, bpm):
    """rekordbox ep5 position packet.
    deck byte 0x30 + type 0x36 + playhead counter.
    Position encodes as BE24: pos * duration_sec * 128 (per screen.py notes)."""
    if not (0.0 <= pos01 <= 1.0):
        return
    p = bytearray(128)
    p[0] = RKBOX_DECK_BYTE[deck]
    p[1] = RKBOX_EP5_PLAYHEAD   # 0x36
    # playhead byte: rekordbox uses the same BE24 playhead as serato xx27 [5,6,7]
    ph = int(pos01 * duration_sec * 128.0)
    p[5] = (ph >> 16) & 0xFF
    p[6] = (ph >> 8) & 0xFF
    p[7] = ph & 0xFF
    send_pkt(ep, p)


# ===== main =====

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unlock", action="store_true", help="run flx10_unlock_v2.py first (sudo)")
    ap.add_argument("--replay-ep5", action="store_true", help="send rekordbox ep5 init once")
    ap.add_argument("--tail-log", action="store_true", help="also tail mixxx.log")
    ap.add_argument("--replay-only", action="store_true", help="send ep5 init then exit (launcher pre-flight)")
    ap.add_argument("--hidraw", default=HIDRAW_DEFAULT)
    ap.add_argument("--log", default=DEFAULT_MIXXX_LOG, help="mixxx.log path to tail")
    args = ap.parse_args()

    if args.unlock:
        log.info("unlock via sudo (7 vendor CTRL)")
        subprocess.run(["sudo", "python3",
                        "/home/Lou/Desktop/Hermes Projects/MIXXX-FLX10/HID/flx10_unlock_v2.py"],
                       check=True)

    ep = None
    if args.replay_ep5:
        ep = open_hidraw(args.hidraw)
        send_rekordbox_ep5_init(ep)
        if args.replay_only:
            log.info("ep5 replay done, exiting (--replay-only)")
            return

    t_sock = threading.Thread(target=socket_server, daemon=True)
    t_sock.start()

    if args.tail_log:
        t_log = threading.Thread(
            target=tail_log,
            args=(args.hidraw, args.log, args.replay_ep5),
            daemon=True,
        )
        t_log.start()

    log.info("rekordbox daemon live — waiting for sysex via socket + log events")
    while True:
        time.sleep(1)


def open_hidraw(path):
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        sys.exit(f"Failed to open {path}: {e}\n  Try `sudo chmod a+w {path}`")
    log.info("opened %s for rekordbox ep5 writes", path)
    return fd


def send_pkt(ep, p):
    try:
        os.write(ep, bytes(p))
    except OSError as e:
        if e.errno == errno.ETIMEDOUT:
            log.warning("ep5 write timeout — firmware not in rekordbox session yet")
        elif e.errno == errno.EBUSY:
            log.warning("ep5 busy — Mixxx Screen.js still grabbing hidraw0?")
        else:
            log.error("ep5 write failed: %s", e)


def tail_log(hidraw, log_path, fire_ep5_after_mode_enable=False):
    log.info("tailing %s for FLX10_TRACK_LOAD / FLX10_POS / mode-enable", log_path)
    p = subprocess.Popen(["tail", "-F", log_path], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
    ep = open_hidraw(hidraw)
    ep5_fired = False
    for line in p.stdout:
        # rekordbox mode-enable:  f0…0301f7  (your JS logs "rekordbox LATE SysEx sent")
        if fire_ep5_after_mode_enable and not ep5_fired:
            if "0301f7" in line.replace(" ", "") or "mode-enable" in line:
                send_rekordbox_ep5_init(ep)
                ep5_fired = True
                log.info("mode-enable detected → ep5 replay fired")
        for deck, st in DECKS.items():
            if f"FLX10_TRACK_LOAD deck={deck}" in line:
                st.loaded = True
                log.info("deck %d track load", deck)
            elif f"FLX10_POS deck={deck}" in line:
                if "pos=" in line:
                    try:
                        v = float(line.split("pos=")[1].strip())
                        st.pos = v
                        send_rekordbox_playhead(ep, deck, st.pos, st.duration, st.bpm)
                    except (ValueError, IndexError):
                        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("shutdown")
