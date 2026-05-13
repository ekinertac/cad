"""
features/live/detection.py — discover which claude processes are
running, and tag the corresponding sessions.

Two layers:

- :func:`find_live_claude_state`: shell out to ``pgrep`` / ``lsof`` /
  ``ps`` and return a structured snapshot of every running claude
  process. Best-effort with a hard wall-clock budget so a stuck lsof
  can't block the picker.
- :func:`_annotate_sessions_with_live_state`: pure function that
  takes that snapshot plus a flat session list and tags each session
  with ``live`` / ``state`` / ``pid`` in place. No I/O of its own
  beyond reading ``session["mtime"]``.

Plus :func:`default_annotator`, a thin wrapper that runs detection
and annotation together — passed to ``core.projects.find_local_projects``
so core stays ignorant of pgrep/lsof.

State buckets by JSONL mtime: ``working`` (<10s), ``input`` (10s-5min),
``idle`` (5min+). The two cutoffs balance "is the agent actively
producing output" vs "is it sitting at the prompt waiting on me" vs
"probably-abandoned tab I should close."

May import from: stdlib. May NOT import from: ``core/`` (read-only
detection on already-discovered sessions) or sibling features.
"""

import os
import re
import subprocess
import time
from collections import defaultdict


# Most claude builds set argv[0] to the version string ("2.1.138") and the
# real binary is "claude". `pgrep -x claude` matches the basename, which is
# the most portable signal we have. The regex extracts the resume UUID
# from argv so we can map a process directly to a session id.
_CLAUDE_RESUME_ARG_RE = re.compile(r"--resume\s+([0-9a-f-]{36})")


# Total wall-clock budget for live detection. lsof can hang on a single
# weird PID (NFS, locked fd, kernel state); a hard ceiling guarantees
# the picker is never blocked for more than this even if one lsof call
# stalls. Set CAD_NO_LIVE=1 in env to skip detection entirely.
_LIVE_DETECTION_BUDGET_SEC = 2.0
_LIVE_DETECTION_PER_CALL_SEC = 1.0


def find_live_claude_state():
    """Inspect running claude processes to discover which sessions are
    live. Returns a dict::

        {
            "bound_uuids": {uuid: {"pid": int, "cwd": str}, ...},
            "unbound_cwds": {cwd: pid_count, ...},
        }

    "Bound" means the process was started with ``--resume <uuid>`` so we
    can map it precisely. "Unbound" means a fresh ``claude`` (no resume
    flag); we know which project is live but not which specific JSONL —
    the caller resolves that heuristically by binding to the most recent
    JSONL(s) under the project's folder.

    Best-effort with a hard total time budget: any subprocess error
    (pgrep/lsof/ps missing, slow, or denied), or breaching the budget,
    silently returns the empty state. The picker still works; it just
    won't show live indicators. Set ``CAD_NO_LIVE=1`` to skip entirely.
    """
    empty = {"bound_uuids": {}, "unbound_cwds": {}}
    if os.environ.get("CAD_NO_LIVE"):
        return empty

    deadline = time.monotonic() + _LIVE_DETECTION_BUDGET_SEC

    try:
        result = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True,
            text=True,
            timeout=_LIVE_DETECTION_PER_CALL_SEC,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return empty
    if result.returncode not in (0, 1):
        return empty

    bound = {}
    unbound = {}
    for pid_str in result.stdout.split():
        if time.monotonic() > deadline:
            # Out of budget. Return whatever we've gathered so far rather
            # than block the picker any longer.
            break
        try:
            pid = int(pid_str)
        except ValueError:
            continue

        cwd = None
        try:
            # `-P -n` skips port-number and IP-to-hostname resolution.
            # Without them lsof does blocking reverse-DNS for every open
            # network socket — measured at 8s vs 0.03s for one claude on
            # the developer's machine. We only care about the `cwd` row
            # so DNS is pure overhead.
            lsof_out = subprocess.run(
                ["lsof", "-Pn", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=_LIVE_DETECTION_PER_CALL_SEC,
            ).stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        for line in lsof_out.splitlines():
            parts = line.split(None, 8)
            # lsof column layout: COMMAND PID USER FD TYPE DEVICE SIZE NODE NAME
            if len(parts) >= 9 and parts[3] == "cwd":
                cwd = parts[-1]
                break
        if not cwd:
            continue

        args = ""
        try:
            args = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=_LIVE_DETECTION_PER_CALL_SEC,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        match = _CLAUDE_RESUME_ARG_RE.search(args)
        if match:
            bound[match.group(1)] = {"pid": pid, "cwd": cwd}
        else:
            unbound[cwd] = unbound.get(cwd, 0) + 1

    return {"bound_uuids": bound, "unbound_cwds": unbound}


def _annotate_sessions_with_live_state(sessions, live_state, now=None):
    """Tag each session in place with ``live`` (bool) and ``state``
    (``working`` / ``input`` / ``idle``). Pure function (no I/O beyond
    reading session["mtime"]) so it's trivially testable.

    State classification for live (process-alive) sessions by JSONL mtime:
    - ``working`` (<10s): still streaming tokens or running a tool
    - ``input`` (10s-5min): claude printed its turn and is at the prompt
      waiting for the user
    - ``idle`` (>5min): alive but stale — probably forgotten about

    Non-live sessions are always ``idle``.
    """
    if now is None:
        now = time.time()
    bound = live_state.get("bound_uuids", {})
    unbound = live_state.get("unbound_cwds", {})
    WORKING_WINDOW = 10  # seconds — still streaming
    INPUT_WINDOW = 300  # 5 minutes — within reach of user; older = idle

    def _state_from_mtime(s):
        age = now - s["mtime"]
        if age < WORKING_WINDOW:
            return "working"
        if age < INPUT_WINDOW:
            return "input"
        return "idle"

    # Default everything to idle first.
    for s in sessions:
        s["live"] = False
        s["state"] = "idle"
        s["pid"] = None

    # Bound: each --resume uuid maps to exactly one session.
    for s in sessions:
        if s["provider"] == "claude" and s["session_id"] in bound:
            s["live"] = True
            s["state"] = _state_from_mtime(s)
            # Carry the PID forward — downstream features (terminal
            # focus, future kill / attach actions) need to reach the
            # actual process and can't realistically re-shell pgrep.
            s["pid"] = bound[s["session_id"]].get("pid")

    # Unbound: for each cwd with N fresh claudes, bind to the N most
    # recently-modified claude JSONLs in that cwd that aren't already
    # bound by a --resume match.
    by_cwd = defaultdict(list)
    for s in sessions:
        if s["provider"] == "claude" and not s["live"]:
            by_cwd[s["cwd"]].append(s)
    for cwd, n_unbound in unbound.items():
        candidates = sorted(
            by_cwd.get(cwd, []), key=lambda x: x["mtime"], reverse=True
        )[:n_unbound]
        for s in candidates:
            s["live"] = True
            s["state"] = _state_from_mtime(s)


def default_annotator(sessions):
    """Convenience wrapper: detect live state once, then annotate the
    given sessions in place. Passed to
    :func:`core.projects.find_local_projects` so callers don't need to
    know detection exists — they just get sessions tagged with
    ``live``/``state``/``pid``.

    Looks names up on the top-level ``cad`` module at call time so
    existing tests that ``monkeypatch.setattr(cad, "find_live_claude_state", …)``
    or ``…, "_annotate_sessions_with_live_state", …)`` still take
    effect. The canonical implementations live in this file; the
    re-exports in ``cad/__init__.py`` are the patch surface.
    """
    from ... import _annotate_sessions_with_live_state as _annotate
    from ... import find_live_claude_state as _detect

    _annotate(sessions, _detect())
