#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyserial>=3.5"]
# ///
"""clawd-mood daemon — TCP server forwarding state to ESP32 via USB CDC.

Replaces the POSIX FIFO (/tmp/clawd-mood.fifo) used in 0.1.x with a TCP
localhost socket. The actual listening port is written to a portfile at
<tempdir>/clawd-mood.port so the hook can discover it. Cross-platform:
macOS / Linux / Windows.

Aggregates multiple CLI sessions (keyed by session_id) into one summary
{state, count} per push. Survives USB hot-unplug: a serial error no longer
kills the daemon — it keeps the TCP server and session state alive, then
auto-reconnects and re-pushes the current state when the cable is replugged.
"""

import atexit
import json
import os
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

import serial
from serial.tools import list_ports

BAUD_RATE = 115200
DEFAULT_TCP_PORT = 48756
PORTFILE = Path(tempfile.gettempdir()) / "clawd-mood.port"
STATUS_FILE = Path(tempfile.gettempdir()) / "clawd-mood-status.log"  # live snapshot for watching
EVENTLOG = Path(tempfile.gettempdir()) / "clawd-mood-events.log"     # append: every incoming event

# Multi-session aggregation. The daemon tracks each CLI session's latest state
# and pushes a single summary {state, count} to the firmware:
#   count   = sessions actively running a turn (the number shown when >= 2)
#   state   = the highest-priority live session state (the face to display)
#
# count counts {working, error, waiting} ("a task in a turn"). waiting is now
# counted — it is a genuine task paused for my confirmation, not idle. error is
# counted because a tool failure mid-turn is transient (Claude reads it and
# continues), so excluding it would make the number flicker on every failure.
# idle/done are not counted. NOTE: 'thinking' was removed entirely and folded
# into 'working' (incoming 'thinking' is normalized to 'working' on ingestion).
#
# Face priority: waiting > error > working > done > idle.
SESSION_TTL = 600.0            # drop a session with no event for 10 min (crash safety net)
PRUNE_INTERVAL = 5.0           # how often the accept loop wakes to prune/repush
COUNTED_STATES = {"working", "error", "waiting"}
STATE_PRIORITY = {
    "waiting": 5, "error": 4, "working": 3,
    "done": 2, "idle": 1, "sleeping": 0,
}

# Staleness demotion — DEFAULT OFF. When CLAWD_MOOD_STALE_SEC > 0, a working/
# error session silent that long is treated as idle (drops out of the count,
# stops driving the face) until it emits again — clearing turns whose end never
# reached us (interrupt / crash / stale window). Trade-off: a genuinely running
# but event-silent session (long single command, long no-tool reply) would be
# falsely shown idle — so it is OFF by default. 'waiting' is always exempt.
# Stop->done, idle_prompt (~60s) and the 600s TTL still clear sessions regardless.
STALE_RUNNING_SEC = float(os.environ.get("CLAWD_MOOD_STALE_SEC", "0"))
STALE_STATES = {"working", "error"}


def display(state: str, count: int) -> dict:
    """Display policy (pure): map the summary (state, count) to what the device
    should show. Kept here (not in firmware) so it is unit-testable and tunable
    without a reflash.
      color : load level by count — 0 green, 1 orange, >=2 red
      blink : True only while a session is waiting for my confirmation
      bottom: "" when nothing is running, else the count as a string (the device
              appends the animated dots, e.g. "1.."/"2..")
    """
    color = "red" if count >= 2 else "orange" if count == 1 else "green"
    return {
        "color": color,
        "blink": state == "waiting",
        "bottom": "" if count == 0 else str(count),
    }


def prune_sessions(sessions: dict, now: float) -> None:
    stale = [sid for sid, (_, ts) in sessions.items() if now - ts > SESSION_TTL]
    for sid in stale:
        del sessions[sid]


def demote_stale(sessions: dict, now: float) -> dict:
    """Return a view of sessions where a working/error session silent for longer
    than STALE_RUNNING_SEC is treated as idle. Pure (does not mutate the input);
    the real state is kept in the live table and reappears on the next event.
    Disabled (no-op) when STALE_RUNNING_SEC <= 0."""
    if STALE_RUNNING_SEC <= 0:
        return dict(sessions)
    out = {}
    for sid, (state, ts) in sessions.items():
        if state in STALE_STATES and now - ts > STALE_RUNNING_SEC:
            out[sid] = ("idle", ts)
        else:
            out[sid] = (state, ts)
    return out


def summarize(sessions: dict) -> tuple[str, int]:
    """Return (summary_state, counted_session_count)."""
    if not sessions:
        return ("idle", 0)
    states = [st for st, _ in sessions.values()]
    summary = max(states, key=lambda s: STATE_PRIORITY.get(s, 0))
    count = sum(1 for s in states if s in COUNTED_STATES)
    return (summary, count)


def write_status(sessions: dict, meta: dict, now: float) -> None:
    """Overwrite STATUS_FILE with a human-readable live snapshot: wall-clock
    time, the aggregate (face/count/color/blink), and a per-session breakdown
    (id, project dir, state, age, counted/stale flags). Watch it with watch.sh."""
    for sid in [s for s in meta if s not in sessions]:  # drop gone sessions
        del meta[sid]
    summary, count = summarize(demote_stale(sessions, now))
    d = display(summary, count)
    blink = 1 if d["blink"] else 0
    lines = [
        f"clawd-mood @ {time.strftime('%Y-%m-%d %H:%M:%S')}   (STALE_RUNNING_SEC={STALE_RUNNING_SEC:g})",
        f"  AGGREGATE  face={summary}  count={count}  color={d['color']}  blink={blink}",
        f"  sessions: {len(sessions)}",
    ]
    if not sessions:
        lines.append("    (none)")
    for sid, (state, ts) in sorted(sessions.items(), key=lambda kv: kv[1][1]):
        age = int(now - ts)
        stale = STALE_RUNNING_SEC > 0 and state in STALE_STATES and (now - ts) > STALE_RUNNING_SEC
        counted = state in COUNTED_STATES and not stale
        flag = "STALE->idle" if stale else ("counted" if counted else "")
        lines.append(f"    {sid[:8]:<8}  {state:<8} age={age:>4}s  {flag:<11}  {meta.get(sid, '')}")
    try:
        STATUS_FILE.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


def find_port() -> str | None:
    override = os.environ.get("CLAWD_MOOD_PORT")
    if override:
        return override
    candidates: list[str] = []
    for p in list_ports.comports():
        if sys.platform == "darwin" and p.device.startswith("/dev/cu.usbmodem"):
            candidates.append(p.device)
        elif sys.platform == "linux" and (
            p.device.startswith("/dev/ttyACM") or p.device.startswith("/dev/ttyUSB")
        ):
            candidates.append(p.device)
        elif (
            sys.platform == "win32"
            and p.device.upper().startswith("COM")
            and p.vid is not None
        ):
            candidates.append(p.device)
    candidates.sort()
    if not candidates:
        return None
    if len(candidates) > 1:
        print(
            f"  warning: multiple devices found {candidates}, using {candidates[0]}",
            file=sys.stderr,
        )
    return candidates[0]


def port_present(path: str) -> bool:
    """Best-effort check that the serial device is still enumerated, so an
    unplug can be detected even when no state change triggers a write."""
    if any(p.device == path for p in list_ports.comports()):
        return True
    return os.path.exists(path)  # mac/linux device nodes vanish on unplug


def open_serial(port: str) -> serial.Serial:
    ser = serial.Serial(
        port, BAUD_RATE, timeout=1, dsrdtr=False, rtscts=False,
    )
    # Suppress reset on open
    ser.dtr = False
    ser.rts = False
    time.sleep(0.5)
    ser.read(1000)  # drain boot output
    time.sleep(1.0)
    ser.read(1000)
    return ser


def bind_listen(preferred: int, env_forced: bool) -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", preferred))
    except OSError:
        if env_forced:
            raise
        s.bind(("127.0.0.1", 0))
    s.listen(8)
    return s, s.getsockname()[1]


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except (AttributeError, ValueError):
        pass  # Windows lacks a real SIGTERM


def check_singleton() -> None:
    if not PORTFILE.exists():
        return
    try:
        port = int(PORTFILE.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            sys.exit("Another clawd-mood daemon is already running.")
    except OSError:
        pass  # stale portfile — take over


def main() -> None:
    install_signal_handlers()
    check_singleton()

    forced = os.environ.get("CLAWD_MOOD_PORT_TCP")
    preferred = int(forced) if forced else DEFAULT_TCP_PORT
    server, actual_port = bind_listen(preferred, forced is not None)
    PORTFILE.write_text(str(actual_port))
    atexit.register(lambda: PORTFILE.unlink(missing_ok=True))

    # Start even with no device attached: bind TCP / track sessions headless,
    # and let the reconnect loop attach the ESP32 whenever it is plugged in.
    # (A serial error mid-run already never kills us; startup matches that.)
    serial_port = find_port()
    ser: serial.Serial | None = None
    if serial_port is not None:
        try:
            ser = open_serial(serial_port)
        except (serial.SerialException, OSError) as e:
            print(f"  !! could not open {serial_port}: {e}", file=sys.stderr)
    print("clawd-mood daemon started")
    print(f"  TCP:    127.0.0.1:{actual_port}")
    print(f"  Portfile: {PORTFILE}")
    print(f"  Status: {STATUS_FILE}")
    if ser is not None:
        print(f"  Serial: {serial_port}")
        print("  Ready!")
    else:
        print("  Serial: (none — will auto-attach when an ESP32 is plugged in)")
        print("  Ready (headless)!")

    sessions: dict[str, tuple[str, float]] = {}
    meta: dict[str, str] = {}            # session_id -> cwd, for the status snapshot
    last_sent: tuple[str, int] | None = None

    def drop_serial(reason: str) -> None:
        """Mark the serial link as down without killing the daemon, so the TCP
        server and session state survive an unplug."""
        nonlocal ser
        if ser is None:
            return
        print(f"  !! serial {reason}: {ser.port}", file=sys.stderr)
        try:
            ser.close()
        except Exception:
            pass
        ser = None

    def reconnect_serial() -> None:
        """Re-open the port after an unplug. On success, force a re-push so the
        freshly rebooted ESP32 (which powers up at idle) shows current state."""
        nonlocal ser, last_sent
        path = find_port()
        if path is None:
            return
        try:
            ser = open_serial(path)
        except (serial.SerialException, OSError):
            ser = None
            return
        print(f"  serial reconnected: {path}")
        last_sent = None  # device rebooted to idle → resend the current state

    def push() -> None:
        nonlocal last_sent, ser
        if ser is None:
            return  # serial down; the summary is re-pushed after reconnect
        summary, count = summarize(demote_stale(sessions, time.monotonic()))
        cur = (summary, count)
        if cur == last_sent:
            return  # nothing changed — don't spam the serial line
        d = display(summary, count)
        line = json.dumps({
            "state": summary, "count": count,
            "color": d["color"], "blink": d["blink"], "bottom": d["bottom"],
        })
        try:
            ser.write((line + "\n").encode())
            ser.flush()
            print(f"  [{time.strftime('%H:%M:%S')}] -> {line}  (sessions={len(sessions)})")
            last_sent = cur
        except (serial.SerialException, OSError) as e:
            drop_serial(f"lost ({e})")  # keep running; reconnect on next tick

    server.settimeout(PRUNE_INTERVAL)
    while True:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            # periodic tick: detect unplug, attempt reconnect, reap crashed
            # sessions, and repush if the summary (or the link) changed.
            if ser is not None and not port_present(ser.port):
                drop_serial("unplugged")
            if ser is None:
                reconnect_serial()
            prune_sessions(sessions, time.monotonic())
            push()
            write_status(sessions, meta, time.monotonic())
            continue
        with conn:
            data = conn.recv(4096)
        now = time.monotonic()
        for line in data.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"  !! bad JSON: {line}", file=sys.stderr)
                continue
            sid = msg.get("session_id") or "_anon"
            # Per-event trace (append) so we can see exactly what each session
            # fires — incl. recap/away_summary-triggered events.
            try:
                with open(EVENTLOG, "a") as _f:
                    _f.write(f"[{time.strftime('%H:%M:%S')}] {sid[:8]} "
                             f"event={msg.get('event','')!r:24} "
                             f"state={msg.get('state','')!r}  {msg.get('cwd','')}\n")
            except OSError:
                pass
            if msg.get("event") == "SessionEnd":
                sessions.pop(sid, None)
                meta.pop(sid, None)
                continue
            state = msg.get("state")
            if not state:
                continue
            if state == "thinking":  # thinking was removed → fold into working
                state = "working"
            sessions[sid] = (state, now)
            meta[sid] = msg.get("cwd", "")
        prune_sessions(sessions, now)
        push()
        write_status(sessions, meta, now)


if __name__ == "__main__":
    main()
