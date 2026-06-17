#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyserial>=3.5"]
# ///
"""
BLACK-BOX test suite for the multi-session state machine v3.
Derived ENTIRELY from the requirements spec; zero source-code inspection.

Run:  uv run plugin/scripts/test_state_machine_v3.py

Requirements source:
  * Per-session states: idle, working, waiting, done, error  (NO "thinking")
  * Event→state mapping (hook.classify)
  * Aggregation (daemon.summarize): count = |{working,error,waiting}| sessions
  * Display policy (daemon.display): color / blink / bottom
  * SessionEnd lifecycle
  * Priority: waiting > error > working > done > idle
"""

import importlib.util
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace(".py", "_mod"), HERE / name
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook   = _load("hook.py")
daemon = _load("daemon.py")

# ---------------------------------------------------------------------------
# check() helper
# ---------------------------------------------------------------------------

_PASS = 0
_FAIL = 0


def check(name: str, got, want) -> None:
    global _PASS, _FAIL
    if got == want:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}")
        print(f"        got : {got!r}")
        print(f"        want: {want!r}")


# ---------------------------------------------------------------------------
# Helper: build a minimal payload dict
# ---------------------------------------------------------------------------

def _evt(event_name, *, session_id="s1", tool_name=None,
         notification_type=None, message=None):
    p = {"hook_event_name": event_name, "session_id": session_id}
    if tool_name is not None:
        p["tool_name"] = tool_name
    if notification_type is not None:
        p["notification_type"] = notification_type
    if message is not None:
        p["message"] = message
    return p


def _classify(event_name, **kw):
    return hook.classify(_evt(event_name, **kw))


# ---------------------------------------------------------------------------
# SECTION 1 — Event → state mapping
# ---------------------------------------------------------------------------

print("\n=== Section 1: Event → state mapping ===")

# --- Straightforward events -----------------------------------------------

check("SessionStart -> idle",
      _classify("SessionStart")["state"], "idle")

# EXPLICIT: UserPromptSubmit MUST be "working", NOT "thinking"
check("UserPromptSubmit -> working (not thinking)",
      _classify("UserPromptSubmit")["state"], "working")

check("PreToolUse -> working",
      _classify("PreToolUse", tool_name="Bash")["state"], "working")

check("PostToolUse -> working",
      _classify("PostToolUse", tool_name="Bash")["state"], "working")

check("SubagentStart -> None (ignored; recap/away_summary fires it)",
      _classify("SubagentStart"), None)

check("SubagentStop -> None (ignored; recap/away_summary fires it)",
      _classify("SubagentStop"), None)

check("PostToolUseFailure -> error",
      _classify("PostToolUseFailure")["state"], "error")

check("Stop -> done",
      _classify("Stop")["state"], "done")

check("PermissionRequest -> waiting (Codex event)",
      _classify("PermissionRequest")["state"], "waiting")

check("PreCompact -> working",
      _classify("PreCompact")["state"], "working")

check("PostCompact -> idle",
      _classify("PostCompact")["state"], "idle")

# No event ever yields "thinking"
ALL_EVENTS = [
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "SubagentStart", "SubagentStop", "PostToolUseFailure", "Stop",
    "PermissionRequest", "PreCompact", "PostCompact",
]
for ev in ALL_EVENTS:
    result = _classify(ev)
    if result is not None:
        check(f"NO 'thinking' from {ev}",
              result.get("state") != "thinking", True)

# Unknown event -> None
check("unknown event -> None",
      _classify("SomeFutureUnknownEvent"), None)

check("empty string event -> None",
      hook.classify({"hook_event_name": "", "session_id": "s1"}), None)

# --- SessionEnd shape -------------------------------------------------
result_end = hook.classify({"hook_event_name": "SessionEnd", "session_id": "s99"})
check("SessionEnd returns a dict",
      isinstance(result_end, dict), True)
check("SessionEnd has event=='SessionEnd'",
      result_end.get("event"), "SessionEnd")
check("SessionEnd has session_id",
      result_end.get("session_id"), "s99")
check("SessionEnd has NO 'state' key",
      "state" not in result_end, True)

# --- Notification: notification_type field --------------------------------
check("Notification(permission_prompt) -> waiting",
      _classify("Notification", notification_type="permission_prompt")["state"], "waiting")

check("Notification(elicitation_dialog) -> waiting",
      _classify("Notification", notification_type="elicitation_dialog")["state"], "waiting")

check("Notification(idle_prompt) -> idle",
      _classify("Notification", notification_type="idle_prompt")["state"], "idle")

# NOTE: per requirements, idle_prompt must produce "idle" not "waiting"
check("Notification(idle_prompt) is NOT waiting",
      _classify("Notification", notification_type="idle_prompt")["state"] != "waiting", True)

check("Notification(auth_success) -> None",
      _classify("Notification", notification_type="auth_success"), None)

check("Notification(elicitation_complete) -> None",
      _classify("Notification", notification_type="elicitation_complete"), None)

check("Notification(elicitation_response) -> None",
      _classify("Notification", notification_type="elicitation_response"), None)

check("Notification(unknown_type) -> None",
      _classify("Notification", notification_type="this_type_does_not_exist"), None)

# --- Notification: message text fallback (no notification_type) -----------
check("Notification fallback: contains 'permission' -> waiting",
      _classify("Notification", message="Claude needs permission to run this")["state"], "waiting")

check("Notification fallback: starts with 'allow ' -> waiting",
      _classify("Notification", message="allow running shell commands")["state"], "waiting")

check("Notification fallback: contains 'needs your' -> waiting",
      _classify("Notification", message="This action needs your approval")["state"], "waiting")

check("Notification fallback: contains 'waiting for your input' -> idle",
      _classify("Notification", message="Waiting for your input now")["state"], "idle")

# NOTE: "waiting for your input" check is case-insensitive per the requirement
# ("lowercased" is applied before matching), so we test with mixed case too.
check("Notification fallback: 'Waiting for your input' uppercase -> idle",
      _classify("Notification", message="Waiting for your input")["state"], "idle")

check("Notification fallback: unrecognised message -> None",
      _classify("Notification", message="everything is fine"), None)

check("Notification fallback: empty message -> None",
      _classify("Notification", message=""), None)

# --- Return shape for normal events ----------------------------------------
r = _classify("PreToolUse", session_id="abc", tool_name="Read")
check("classify shape has 'state'",  "state" in r,        True)
check("classify shape has 'event'",  "event" in r,        True)
check("classify shape has 'tool'",   "tool"  in r,        True)
check("classify shape has 'session_id'", "session_id" in r, True)
check("classify: tool name propagated", r["tool"],         "Read")
check("classify: session_id propagated", r["session_id"],  "abc")

# Event with no tool_name -> tool defaults to ""
r2 = _classify("SessionStart", session_id="s2")
check("classify: missing tool -> ''", r2["tool"], "")

# Event with no session_id -> session_id defaults to ""
r3 = hook.classify({"hook_event_name": "PreToolUse"})
check("classify: missing session_id -> ''", r3["session_id"], "")

# cwd (project dir) is propagated for the status snapshot
r4 = hook.classify({"hook_event_name": "PreToolUse", "session_id": "x", "cwd": "/proj/foo"})
check("classify: cwd propagated", r4.get("cwd"), "/proj/foo")
r5 = hook.classify({"hook_event_name": "SessionEnd", "session_id": "x", "cwd": "/proj/foo"})
check("classify: SessionEnd carries cwd", r5.get("cwd"), "/proj/foo")

# ---------------------------------------------------------------------------
# SECTION 2 — daemon.summarize()
# ---------------------------------------------------------------------------

print("\n=== Section 2: daemon.summarize() ===")

T0 = time.time()


def _sessions(**kwargs):
    """Build sessions dict: keyword arg = session_id=state."""
    return {sid: (state, T0) for sid, state in kwargs.items()}


# Empty
check("empty sessions -> ('idle', 0)",
      daemon.summarize({}), ("idle", 0))

# Single-session each state
check("single idle    -> ('idle',    0)", daemon.summarize(_sessions(a="idle")),    ("idle",    0))
check("single working -> ('working', 1)", daemon.summarize(_sessions(a="working")), ("working", 1))
check("single waiting -> ('waiting', 1)", daemon.summarize(_sessions(a="waiting")), ("waiting", 1))
check("single done    -> ('done',    0)", daemon.summarize(_sessions(a="done")),    ("done",    0))
check("single error   -> ('error',   1)", daemon.summarize(_sessions(a="error")),   ("error",   1))

# NOTE: "done" and "idle" are NOT counted per requirements.
check("idle not counted (count==0)",  daemon.summarize(_sessions(a="idle"))[1],  0)
check("done not counted (count==0)",  daemon.summarize(_sessions(a="done"))[1],  0)

# Waiting IS counted
check("one waiting -> count==1",
      daemon.summarize(_sessions(a="waiting"))[1], 1)

# Multi-session priority
check("waiting+error -> ('waiting', 2)",
      daemon.summarize(_sessions(a="waiting", b="error")), ("waiting", 2))

check("waiting+working -> ('waiting', 2)",
      daemon.summarize(_sessions(a="waiting", b="working")), ("waiting", 2))

check("working+one waiting -> ('waiting', 2)",
      daemon.summarize(_sessions(a="working", b="waiting")), ("waiting", 2))

check("waiting+error+working -> ('waiting', 3)",
      daemon.summarize(_sessions(a="waiting", b="error", c="working")), ("waiting", 3))

check("error+working -> ('error', 2)",
      daemon.summarize(_sessions(a="error", b="working")), ("error", 2))

check("error+idle -> ('error', 1)",
      daemon.summarize(_sessions(a="error", b="idle")), ("error", 1))

check("working+idle -> ('working', 1)",
      daemon.summarize(_sessions(a="working", b="idle")), ("working", 1))

check("working+done -> ('working', 1)",
      daemon.summarize(_sessions(a="working", b="done")), ("working", 1))

check("done+idle -> ('done', 0)",
      daemon.summarize(_sessions(a="done", b="idle")), ("done", 0))

# done beats idle in priority
check("done priority over idle",
      daemon.summarize(_sessions(a="idle", b="done"))[0], "done")

# All states present
check("all states -> ('waiting', 3) (waiting+error+working counted)",
      daemon.summarize(_sessions(a="waiting", b="error", c="working", d="done", e="idle")),
      ("waiting", 3))

# Multiple sessions same active state
check("two working -> ('working', 2)",
      daemon.summarize(_sessions(a="working", b="working")), ("working", 2))

check("three working -> ('working', 3)",
      daemon.summarize(_sessions(a="working", b="working", c="working")), ("working", 3))

check("two waiting -> ('waiting', 2)",
      daemon.summarize(_sessions(a="waiting", b="waiting")), ("waiting", 2))

check("two idle -> ('idle', 0)",
      daemon.summarize(_sessions(a="idle", b="idle")), ("idle", 0))

# Two errors
check("two error -> ('error', 2)",
      daemon.summarize(_sessions(a="error", b="error")), ("error", 2))

# ---------------------------------------------------------------------------
# SECTION 3 — daemon.display()
# ---------------------------------------------------------------------------

print("\n=== Section 3: daemon.display() ===")

# color rules
check("display count=0 -> green",  daemon.display("idle",    0)["color"], "green")
check("display count=1 -> orange", daemon.display("working", 1)["color"], "orange")
check("display count=2 -> red",    daemon.display("working", 2)["color"], "red")
check("display count=3 -> red",    daemon.display("error",   3)["color"], "red")

# blink rules: only True when state=="waiting"
check("display state=waiting -> blink=True",  daemon.display("waiting", 1)["blink"], True)
check("display state=working -> blink=False", daemon.display("working", 1)["blink"], False)
check("display state=idle    -> blink=False", daemon.display("idle",    0)["blink"], False)
check("display state=error   -> blink=False", daemon.display("error",   1)["blink"], False)
check("display state=done    -> blink=False", daemon.display("done",    0)["blink"], False)

# NOTE: waiting is counted, so a session in waiting always has count>=1,
# meaning it never shows green. We test count=1 and count=2 with waiting.
check("waiting count=1 -> orange + blink",
      (daemon.display("waiting", 1)["color"], daemon.display("waiting", 1)["blink"]),
      ("orange", True))

check("waiting count=2 -> red + blink",
      (daemon.display("waiting", 2)["color"], daemon.display("waiting", 2)["blink"]),
      ("red", True))

# The spec says a waiting session is always counted, so count>=1 for waiting.
# But display() is a pure function; test the boundary anyway for robustness.
# NOTE: hypothetical waiting count=0 -> green but blink=True (spec says only
# color depends on count; blink depends on state). Documenting per literal spec.
check("waiting count=0 (hypothetical) -> green + blink",
      (daemon.display("waiting", 0)["color"], daemon.display("waiting", 0)["blink"]),
      ("green", True))

# bottom field
check("display count=0 -> bottom=''",  daemon.display("idle",    0)["bottom"], "")
check("display count=1 -> bottom='1'", daemon.display("working", 1)["bottom"], "1")
check("display count=2 -> bottom='2'", daemon.display("error",   2)["bottom"], "2")
check("display count=3 -> bottom='3'", daemon.display("waiting", 3)["bottom"], "3")

# bottom is a string, not an int
check("bottom is str type for count=1",
      type(daemon.display("working", 1)["bottom"]), str)

# Keys present
d = daemon.display("working", 1)
check("display returns 'color' key",  "color"  in d, True)
check("display returns 'blink' key",  "blink"  in d, True)
check("display returns 'bottom' key", "bottom" in d, True)

# ---------------------------------------------------------------------------
# SECTION 4 — Scenario trajectories (simulate)
# ---------------------------------------------------------------------------

print("\n=== Section 4: Scenario trajectories ===")


def simulate(events):
    """
    Feed a list of payload dicts through hook.classify, apply update/remove
    lifecycle to a single sessions dict, and record daemon.summarize after
    each event that produces output.

    Returns: list of (event_name, summarize_result) for every event that
             caused a state update or session removal.
    """
    sessions = {}
    history  = []
    now      = time.time()
    for payload in events:
        result = hook.classify(payload)
        if result is None:
            continue
        ev   = result.get("event", "")
        sid  = result.get("session_id", "")
        if ev == "SessionEnd":
            sessions.pop(sid, None)
        else:
            state = result["state"]
            sessions[sid] = (state, now)
        history.append((ev, daemon.summarize(sessions)))
    return history


# --- 4a. Queued requests: several prompts + tool events, single session ----
events_queue = [
    _evt("SessionStart",      session_id="s1"),
    _evt("UserPromptSubmit",  session_id="s1"),
    _evt("PreToolUse",        session_id="s1", tool_name="Bash"),
    _evt("PostToolUse",       session_id="s1", tool_name="Bash"),
    _evt("PreToolUse",        session_id="s1", tool_name="Read"),
    _evt("PostToolUse",       session_id="s1", tool_name="Read"),
    _evt("UserPromptSubmit",  session_id="s1"),
    _evt("PreToolUse",        session_id="s1", tool_name="Write"),
]
hist_q = simulate(events_queue)
# After SessionStart -> idle, count=0
check("queue: after SessionStart -> (idle,0)",
      hist_q[0], ("SessionStart", ("idle", 0)))
# All subsequent events are working, count stays 1
for i, (ev, summary) in enumerate(hist_q[1:], start=1):
    check(f"queue: step {i} ({ev}) -> working, count=1",
          summary, ("working", 1))

# --- 4b. Compact flow ------------------------------------------------------
events_compact = [
    _evt("UserPromptSubmit", session_id="s1"),
    _evt("PreCompact",       session_id="s1"),
    _evt("PostCompact",      session_id="s1"),
]
hist_c = simulate(events_compact)
check("compact: UserPromptSubmit -> working",
      hist_c[0][1], ("working", 1))
check("compact: PreCompact -> working (still running)",
      hist_c[1][1], ("working", 1))
check("compact: PostCompact -> idle",
      hist_c[2][1], ("idle", 0))

# --- 4c. Waiting-confirm: permission_prompt --------------------------------
events_wait_perm = [
    _evt("UserPromptSubmit", session_id="s1"),
    _evt("Notification",     session_id="s1", notification_type="permission_prompt"),
]
hist_wp = simulate(events_wait_perm)
check("wait-perm: working then permission_prompt -> waiting",
      hist_wp[1][1], ("waiting", 1))

# --- 4c2. elicitation_dialog -----------------------------------------------
events_wait_eli = [
    _evt("UserPromptSubmit", session_id="s1"),
    _evt("Notification",     session_id="s1", notification_type="elicitation_dialog"),
]
hist_we = simulate(events_wait_eli)
check("wait-eli: working then elicitation_dialog -> waiting",
      hist_we[1][1], ("waiting", 1))

# --- 4c3. idle_prompt must produce idle, NOT waiting -----------------------
events_idle_prompt = [
    _evt("UserPromptSubmit", session_id="s1"),
    _evt("Notification",     session_id="s1", notification_type="idle_prompt"),
]
hist_ip = simulate(events_idle_prompt)
check("idle_prompt: becomes idle (not waiting)",
      hist_ip[1][1], ("idle", 0))

# --- 4d. Interrupt recovery (Esc fires NO event) ---------------------------
# Esc: no event arrives; session remains "working".
# Then idle_prompt Notification arrives -> idle.
events_interrupt = [
    _evt("UserPromptSubmit", session_id="s1"),
    # (Esc pressed by user: NO event)
    _evt("Notification",     session_id="s1", notification_type="idle_prompt"),
]
hist_int = simulate(events_interrupt)
check("interrupt: after UserPromptSubmit -> working",
      hist_int[0][1], ("working", 1))
check("interrupt: idle_prompt -> idle (self-healed)",
      hist_int[1][1], ("idle", 0))

# --- 4e. Terminate / SessionEnd: survivor still counted -------------------
# Two sessions working; remove one; survivor remains ("working", 1).
events_terminate = [
    _evt("UserPromptSubmit", session_id="sA"),
    _evt("UserPromptSubmit", session_id="sB"),
    # Both working -> ("working", 2)
    _evt("SessionEnd",       session_id="sA"),
    # sA gone; sB working -> ("working", 1)
]
hist_t = simulate(events_terminate)
check("terminate: both sessions working -> ('working', 2)",
      hist_t[1][1], ("working", 2))
check("terminate: after SessionEnd of sA -> ('working', 1)",
      hist_t[2][1], ("working", 1))

# --- 4f. Survivor in transient error still counts -------------------------
events_err = [
    _evt("UserPromptSubmit",   session_id="sA"),
    _evt("UserPromptSubmit",   session_id="sB"),
    _evt("PostToolUseFailure", session_id="sA"),  # sA -> error
    _evt("SessionEnd",         session_id="sB"),  # remove sB
    # only sA (error) remains
]
hist_e = simulate(events_err)
check("survivor error still counted: ('error', 1)",
      hist_e[3][1], ("error", 1))

# --- 4g. Full happy path: Idle->working->done->idle -----------------------
events_full = [
    _evt("SessionStart",     session_id="s1"),
    _evt("UserPromptSubmit", session_id="s1"),
    _evt("PreToolUse",       session_id="s1", tool_name="Bash"),
    _evt("PostToolUse",      session_id="s1", tool_name="Bash"),
    _evt("Stop",             session_id="s1"),   # done
    _evt("SessionEnd",       session_id="s1"),   # removed
]
hist_f = simulate(events_full)
check("full path: SessionStart -> idle",     hist_f[0][1], ("idle", 0))
check("full path: UserPromptSubmit -> working", hist_f[1][1], ("working", 1))
check("full path: PreToolUse -> working",    hist_f[2][1], ("working", 1))
check("full path: PostToolUse -> working",   hist_f[3][1], ("working", 1))
check("full path: Stop -> done",             hist_f[4][1], ("done", 0))
check("full path: SessionEnd -> idle",       hist_f[5][1], ("idle", 0))

# --- 4h. auth_success notification -> no update (skipped by simulate) -----
events_auth = [
    _evt("UserPromptSubmit", session_id="s1"),
    _evt("Notification",     session_id="s1", notification_type="auth_success"),
]
hist_auth = simulate(events_auth)
# auth_success returns None, so simulate skips it; only 1 entry in history.
check("auth_success: no state change (simulate skips it)",
      len(hist_auth), 1)
check("auth_success: session still working",
      hist_auth[0][1], ("working", 1))

# ---------------------------------------------------------------------------
# SECTION 5 — Randomised oracle
# ---------------------------------------------------------------------------

print("\n=== Section 5: Randomised oracle ===")

ALL_STATES = ["idle", "working", "waiting", "done", "error"]
PRIORITY   = ["waiting", "error", "working", "done", "idle"]
COUNTED    = {"working", "error", "waiting"}


def oracle_summarize(sessions: dict):
    """Reference implementation derived directly from requirements."""
    if not sessions:
        return ("idle", 0)
    states = [v[0] for v in sessions.values()]
    count  = sum(1 for s in states if s in COUNTED)
    # Highest-priority state present
    state_set = set(states)
    for p in PRIORITY:
        if p in state_set:
            return (p, count)
    # Fallback (should never reach here with valid states)
    return ("idle", count)


rng = random.Random(20260618)

for group in range(6):
    n_timepoints = rng.randint(10, 20)
    for t in range(n_timepoints):
        n_sessions = 5
        sessions   = {}
        now        = time.time()
        for i in range(n_sessions):
            sid   = f"g{group}_s{i}"
            state = rng.choice(ALL_STATES)
            sessions[sid] = (state, now)

        got  = daemon.summarize(sessions)
        want = oracle_summarize(sessions)
        check(
            f"oracle group={group} t={t} n={n_sessions}",
            got, want
        )

        # Print sample data for group 0
        if group == 0:
            state_snapshot = {sid: v[0] for sid, v in sessions.items()}
            print(f"    [group=0 t={t}] sessions={state_snapshot} "
                  f"got={got} want={want}")

# ---------------------------------------------------------------------------
# SECTION 6 — TTL prune (crash safety net)
# ---------------------------------------------------------------------------

print("\n=== Section 6: prune_sessions (TTL) ===")

ttl = daemon.SESSION_TTL
_p = {"old": ("working", 0.0), "fresh": ("working", ttl)}
daemon.prune_sessions(_p, now=ttl + 1.0)   # old age = ttl+1 > ttl; fresh age = 1
check("prune: stale (>TTL no events) removed", "old" in _p, False)
check("prune: fresh session kept", "fresh" in _p, True)

_p2 = {"a": ("working", 100.0)}
daemon.prune_sessions(_p2, now=100.0 + ttl - 1)  # just under TTL
check("prune: session just under TTL kept", "a" in _p2, True)

# ---------------------------------------------------------------------------
# SECTION 7 — staleness demotion (clears stuck working/error sessions)
# ---------------------------------------------------------------------------

print("\n=== Section 7: demote_stale ===")

# Staleness is OFF by default (STALE_RUNNING_SEC=0). Enable it explicitly to
# test the demotion logic, then restore.
_orig_stale = daemon.STALE_RUNNING_SEC
daemon.STALE_RUNNING_SEC = 120.0
S = daemon.STALE_RUNNING_SEC
NOW = 10000.0

# working silent longer than N -> treated as idle (drops out of the count)
stale_w = {"a": ("working", NOW - S - 1)}
check("stale working -> demoted to idle", daemon.demote_stale(stale_w, NOW)["a"][0], "idle")
check("stale working -> count 0",
      daemon.summarize(daemon.demote_stale(stale_w, NOW)), ("idle", 0))

# fresh working -> kept and counted
fresh_w = {"a": ("working", NOW - 1)}
check("fresh working -> kept", daemon.demote_stale(fresh_w, NOW)["a"][0], "working")
check("fresh working -> count 1",
      daemon.summarize(daemon.demote_stale(fresh_w, NOW)), ("working", 1))

# stale error -> demoted
check("stale error -> demoted to idle",
      daemon.demote_stale({"a": ("error", NOW - S - 1)}, NOW)["a"][0], "idle")

# waiting is EXEMPT — a pending confirmation keeps showing
check("stale waiting -> NOT demoted (exempt)",
      daemon.demote_stale({"a": ("waiting", NOW - S - 100)}, NOW)["a"][0], "waiting")
check("stale waiting -> still counts",
      daemon.summarize(daemon.demote_stale({"a": ("waiting", NOW - S - 100)}, NOW)), ("waiting", 1))

# mixed: one fresh + one stale working -> count drops to 1
mixed = {"a": ("working", NOW - 1), "b": ("working", NOW - S - 1)}
check("mixed fresh+stale working -> count 1",
      daemon.summarize(daemon.demote_stale(mixed, NOW)), ("working", 1))

# recovery: a session that emits again (fresh ts) is counted again
check("recovered (fresh ts) -> counts again",
      daemon.summarize(daemon.demote_stale({"a": ("working", NOW)}, NOW)), ("working", 1))

# demote_stale must not mutate its input
_orig = {"a": ("working", NOW - S - 1)}
daemon.demote_stale(_orig, NOW)
check("demote_stale is pure (no mutation)", _orig["a"][0], "working")

# disabled (STALE_RUNNING_SEC <= 0) -> demotion is a no-op (this is the default)
daemon.STALE_RUNNING_SEC = 0
check("disabled: stale working NOT demoted",
      daemon.demote_stale({"a": ("working", NOW - 99999)}, NOW)["a"][0], "working")
check("disabled: stale working still counts",
      daemon.summarize(daemon.demote_stale({"a": ("working", NOW - 99999)}, NOW)), ("working", 1))
daemon.STALE_RUNNING_SEC = _orig_stale  # restore module default

# ---------------------------------------------------------------------------
# Final tally
# ---------------------------------------------------------------------------

print(f"\n{'='*50}")
print(f"Results: {_PASS} passed, {_FAIL} failed out of {_PASS + _FAIL} checks")
if _FAIL:
    print("SOME TESTS FAILED (expected if implementation not yet updated)")
    sys.exit(1)
else:
    print("All tests passed.")
