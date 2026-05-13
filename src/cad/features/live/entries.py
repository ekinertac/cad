"""
features/live/entries.py — build the entry list the `cad live` picker renders.

Takes the output of :func:`core.projects.find_local_projects` (with
live annotations applied — see :mod:`features.live.detection`) and
turns it into a flat list of picker rows, with non-selectable
``header: True`` rows leading each project group and a blank spacer
between groups for visual separation.

Within each group, sessions are sorted by state priority: ``working``
first (most likely needing attention), then ``input``, then ``idle``.
Outer order matches the project picker (most-recent-activity first).

May import from: ``core.projects``. May NOT import from: ``features/``
other than itself.
"""

from ...core.projects import find_local_projects
from ...core.projects import load_session_summary


# Text labels shown next to the coloured dot on each live-picker row.
# Padded to a fixed width so columns stay aligned regardless of state.
_STATE_TEXT_LABELS = {
    "working": "[working]",
    "input": "[input]  ",
    "idle": "[idle]   ",
}


def _build_live_entries():
    """Snapshot of every live agent session across all projects, ready
    to feed into ``select_entry``. Each project group is led by a
    non-selectable header row (``header: True``) carrying the project
    name; its sessions follow, indented, in state-priority order
    (working → input → idle) so the row most likely needing attention
    sits at the top of the group. Outer order matches the project
    picker (most-recent-activity first).

    The header row is what gives ``cad live`` its visual hierarchy —
    without it, sessions from many projects blur into one undifferentiated
    list. ``select_entry`` knows to skip rows tagged ``header: True``
    during cursor navigation.
    """
    # Local import to avoid a circular dependency: __init__.py's
    # find_local_projects shim imports from this feature, so importing
    # the shim at module top would create a cycle. Going through
    # cad.find_local_projects (the public name) at call time picks up
    # whichever shim version is installed.
    from ... import find_local_projects as _find_with_live

    projects = _find_with_live()
    entries = []
    state_order = {"working": 0, "input": 1, "idle": 2}
    for p in projects:
        live = [s for s in p["sessions"] if s.get("live")]
        if not live:
            continue
        live.sort(key=lambda s: state_order.get(s.get("state"), 3))
        # Blank spacer row between groups so the eye separates one
        # project from the next. The first group gets no leading
        # spacer — it's already visually at the top.
        if entries:
            entries.append({"header": True, "display": ""})
        entries.append({"header": True, "display": p["name"]})
        for s in live:
            load_session_summary(s)
            label = _STATE_TEXT_LABELS.get(s.get("state"), "[?]      ")
            # session["display"] is "<date>  <size>  provider/<rest>".
            # Drop the date+size column — the state label already
            # telegraphs recency, and screen real estate is precious.
            display = s["display"]
            provider_marker = f"{s['provider']}/"
            idx = display.find(provider_marker)
            tail = display[idx:] if idx >= 0 else display
            entries.append(
                {
                    # Mirror the session dict shape so resume_session
                    # and the existing select_entry rendering both work
                    # without special-casing.
                    "provider": s["provider"],
                    "session_id": s["session_id"],
                    "cwd": s["cwd"],
                    "filepath": s["filepath"],
                    "mtime": s["mtime"],
                    "live": s["live"],
                    "state": s["state"],
                    # PID is None for unbound (cwd-matched) sessions;
                    # the focus action knows to fall back to peek when
                    # it's missing.
                    "pid": s.get("pid"),
                    # No extra indent here — ``select_entry`` already
                    # prefixes each row with a 2-char arrow column and
                    # a 2-char state-marker column, which naturally
                    # indents sessions 4 spaces below the flush-left
                    # project header.
                    "display": f"{label}  {tail}",
                }
            )
    return entries
