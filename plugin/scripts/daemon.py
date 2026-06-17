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

# Multi-session aggregation. The daemon tracks each CLI session's latest state
# and pushes a single summary {state, count} to the firmware:
#   count   = sessions currently working or waiting (shown on screen when >= 2)
#   state   = the highest-priority live session state (the face to display)
SESSION_TTL = 600.0            # drop a session with no event for 10 min (crash safety net)
PRUNE_INTERVAL = 5.0           # how often the accept loop wakes to prune/repush
COUNTED_STATES = {"working", "waiting"}
STATE_PRIORITY = {
    "error": 6, "waiting": 5, "working": 4,
    "thinking": 3, "done": 2, "idle": 1, "sleeping": 0,
}


def prune_sessions(sessions: dict, now: float) -> None:
    stale = [sid for sid, (_, ts) in sessions.items() if now - ts > SESSION_TTL]
    for sid in stale:
        del sessions[sid]


def summarize(sessions: dict) -> tuple[str, int]:
    """Return (summary_state, counted_session_count)."""
    if not sessions:
        return ("idle", 0)
    states = [st for st, _ in sessions.values()]
    summary = max(states, key=lambda s: STATE_PRIORITY.get(s, 0))
    count = sum(1 for s in states if s in COUNTED_STATES)
    return (summary, count)


def detect_port() -> str:
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
        sys.exit(
            "No ESP32-like USB CDC device found. Plug it in or set "
            "CLAWD_MOOD_PORT (mac: /dev/cu.xxx, linux: /dev/ttyACM0, windows: COM3)."
        )
    if len(candidates) > 1:
        print(
            f"  warning: multiple devices found {candidates}, using {candidates[0]}",
            file=sys.stderr,
        )
    return candidates[0]


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

    serial_port = detect_port()
    ser = open_serial(serial_port)
    print("clawd-mood daemon started")
    print(f"  TCP:    127.0.0.1:{actual_port}")
    print(f"  Portfile: {PORTFILE}")
    print(f"  Serial: {serial_port}")
    print("  Ready!")

    sessions: dict[str, tuple[str, float]] = {}
    last_sent: tuple[str, int] | None = None

    def push() -> None:
        nonlocal last_sent
        summary, count = summarize(sessions)
        cur = (summary, count)
        if cur == last_sent:
            return  # nothing changed — don't spam the serial line
        line = json.dumps({"state": summary, "count": count})
        try:
            ser.write((line + "\n").encode())
            ser.flush()
            print(f"  -> {line}  (sessions={len(sessions)})")
            last_sent = cur
        except serial.SerialException as e:
            print(f"  !! serial error: {e}", file=sys.stderr)
            sys.exit(1)

    server.settimeout(PRUNE_INTERVAL)
    while True:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            # periodic tick: reap crashed sessions, repush if the summary changed
            prune_sessions(sessions, time.monotonic())
            push()
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
            if msg.get("event") == "SessionEnd":
                sessions.pop(sid, None)
                continue
            state = msg.get("state")
            if not state:
                continue
            sessions[sid] = (state, now)
        prune_sessions(sessions, now)
        push()


if __name__ == "__main__":
    main()
