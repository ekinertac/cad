"""
core/projects.py — discover sessions across providers and group into
projects for the two-step picker.

The public entry point is :func:`find_local_projects` which:

1. Calls every provider's ``find_X_sessions`` (claude, codex, pi,
   opencode, forge) and flattens the results.
2. Applies any user-set cwd overrides so a moved session lands in its
   new project.
3. **Optionally** runs a live-state annotator over the flat session
   list — passed in as ``annotate_live``. core/projects.py knows
   nothing about pgrep/lsof; it just accepts a callable
   ``(sessions) -> None`` and runs it if provided. features/live
   wires the real annotator in.
4. Groups the flat list by cwd via :func:`_group_sessions_into_projects`
   (which also collapses ``~/`` and ``~/Code`` into a virtual "Global
   Sessions" entry, deduplicates name collisions, and builds the
   ``display`` row string the picker renders).

Plus the auto-pick helper :func:`_find_project_for_cwd` (used by
``cad`` to drop straight into the right project when the user is
inside one) and :func:`load_session_summary` (lazy summary +
``display`` builder per session, invoked after the user picks a
project so the project picker stays cheap).

May import from: core/discovery, core/session_model, core/overrides,
core/providers. May NOT import from: ``features/``.
"""

import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .discovery import (
    find_claude_sessions,
    find_codex_sessions,
    find_forge_sessions,
    find_opencode_sessions,
    find_pi_sessions,
    get_codex_summary,
    get_pi_summary,
)
from .overrides import _apply_cwd_override, get_title_override
from .providers import PROVIDER_BADGES
from .session_model import get_claude_session_metadata, get_session_summary


def _global_session_cwds():
    """``cwd`` values that should merge into the virtual 'Global Sessions'
    project — the user's home dir and ~/Code, the two catch-all places for
    one-off questions. Returned as strings since cwd values from JSONLs are
    strings; comparison is exact-match.
    """
    home = str(Path.home())
    return {home, str(Path(home) / "Code")}


def _find_project_for_cwd(projects, cwd):
    """Return the project that 'owns' the given cwd, or None.

    Match rules, in order:

    1. Skip if cwd is a global cwd (``~/`` or ``~/Code``) — those are
       Global Sessions territory; we don't auto-pick the catch-all bucket.
    2. Exact match against any project's cwd.
    3. Longest-prefix match — a project at ``/Users/x/Code/foo`` claims
       any subdir like ``/Users/x/Code/foo/sub/dir``. The deepest match
       wins when multiple ancestors are projects.
    """
    if cwd in _global_session_cwds():
        return None
    for p in projects:
        if p["cwd"] == cwd:
            return p
    candidates = [
        p for p in projects if p["cwd"] and cwd.startswith(p["cwd"].rstrip("/") + "/")
    ]
    if candidates:
        return max(candidates, key=lambda p: len(p["cwd"]))
    return None


def load_session_summary(session):
    """Populate ``session['summary']`` and ``session['display']`` in place.

    For JSONL-based providers (claude, codex, pi) the summary is loaded
    lazily here — discovery only reads cwd + mtime, leaving the heavier
    first-prompt scan for once the user actually chose a project.
    SQLite-based providers (opencode, forge) already set the summary at
    discovery time because their schema makes it cheap; for those we just
    skip the scan and go straight to display building.

    The cct sidecar override (set by ``r``/``s`` actions in the picker)
    wins over any provider-derived summary.
    """
    if session["display"] is not None:
        return
    override = get_title_override(session)
    if override:
        session["summary"] = override
    elif session["summary"] is None:
        if session["provider"] == "claude":
            # Single pass picks up summary AND any user-set /rename name.
            meta = get_claude_session_metadata(session["filepath"])
            session["summary"] = meta["summary"]
            if not session.get("name"):
                session["name"] = meta["name"]
        elif session["provider"] == "codex":
            session["summary"] = get_codex_summary(session["filepath"])
        elif session["provider"] == "pi":
            session["summary"] = get_pi_summary(session["filepath"])
        else:
            session["summary"] = "(unknown provider)"

    mtime = datetime.fromtimestamp(session["mtime"])
    date_str = mtime.strftime("%Y-%m-%d %H:%M")
    size_kb = session["size"] / 1024
    summary_one_line = re.sub(r"\s+", " ", session["summary"]).strip()
    # User-named sessions (claude /rename, pi --name) get a prominent
    # "provider/Name" prefix separated from the implicit prompt by an
    # em-dash. Unnamed rows keep the trailing slash so layout aligns.
    name = session.get("name")
    if name:
        prefix = f"{session['provider']}/{name} — "
    else:
        prefix = f"{session['provider']}/ "
    session["display"] = f"{date_str}  {size_kb:5.0f} KB  {prefix}{summary_one_line}"


def find_local_projects(folder=None, annotate_live=None):
    """Discover all sessions across providers and group them by ``cwd`` into
    project dicts for the two-step picker.

    Grouping key is the JSONL's / DB's recorded ``cwd`` rather than the
    encoded folder name, so sessions from any agent in the same directory
    end up in a single project entry. Sessions whose cwd matches the
    'Global Sessions' rule (home or ~/Code) collapse into a single virtual
    entry.

    The ``folder`` argument is accepted for backward compatibility but is
    typically unused — providers know their own canonical roots.

    ``annotate_live`` (optional callable): given the flat session list,
    tag each session with ``live``/``state``/``pid`` in place. core/
    knows nothing about pgrep/lsof — the live feature module injects
    its annotator here. When omitted, every session is treated as
    not-live (the default project picker without live indicators).

    Each project dict::

        {
            "name":          "foo",                      # cwd basename, or "Global Sessions"
            "cwd":           "/Users/x/Code/foo",        # None for the virtual entry
            "sessions":      [<session dict>, ...],      # sorted newest-first
            "session_count": 3,
            "latest_mtime":  1700000000.0,
            "provider_counts": {"claude": 2, "codex": 1},
            "display":       "foo            2026-05-10 14:02   3 sessions  (2c+1x)",
        }
    """
    if folder is None:
        folder = Path.home() / ".claude" / "projects"
    sessions = (
        find_claude_sessions(folder)
        + find_codex_sessions()
        + find_pi_sessions()
        + find_opencode_sessions()
        + find_forge_sessions()
    )
    # Apply user-set cwd overrides before grouping so a moved session
    # appears under its new project — agent files are never modified.
    for s in sessions:
        _apply_cwd_override(s)
    # Live annotation is a feature-level concern; inject the annotator
    # if the caller wants it (typically yes for `cad` / `cad live`, no
    # for the HTML batch renderer which only needs grouping).
    if annotate_live is not None:
        annotate_live(sessions)
    return _group_sessions_into_projects(sessions)


def _group_sessions_into_projects(sessions):
    """Pure grouping function — given a flat session list (any providers),
    group by cwd, apply the Global Sessions rule, and build display rows.
    Split out from ``find_local_projects`` so it's trivially testable.
    """
    global_cwds = _global_session_cwds()
    by_cwd = defaultdict(list)
    global_sessions = []

    for s in sessions:
        if s["cwd"] in global_cwds:
            global_sessions.append(s)
        else:
            by_cwd[s["cwd"]].append(s)

    def _build(name, cwd, sess):
        sess.sort(key=lambda x: x["mtime"], reverse=True)
        counts = defaultdict(int)
        for s in sess:
            counts[s["provider"]] += 1
        # Project-level live count comes from session-level annotations
        # already applied by _annotate_sessions_with_live_state.
        live_count = sum(1 for s in sess if s.get("live"))
        # Project state reflects the most active session: working beats
        # input beats idle. Picker uses this to colour the row marker.
        if any(s.get("live") and s.get("state") == "working" for s in sess):
            project_state = "working"
        elif any(s.get("live") and s.get("state") == "input" for s in sess):
            project_state = "input"
        else:
            project_state = "idle"
        return {
            "name": name,
            "cwd": cwd,
            "sessions": sess,
            "session_count": len(sess),
            "latest_mtime": sess[0]["mtime"],
            "provider_counts": dict(counts),
            "live_count": live_count,
            "state": project_state,
        }

    projects = [
        _build(Path(cwd).name or cwd, cwd, sess) for cwd, sess in by_cwd.items()
    ]
    if global_sessions:
        projects.append(_build("Global Sessions", None, global_sessions))

    projects.sort(key=lambda p: p["latest_mtime"], reverse=True)

    # Disambiguate name collisions (two real projects whose basename matches)
    # by appending the full cwd to the colliding rows only.
    name_counts = defaultdict(int)
    for p in projects:
        name_counts[p["name"]] += 1

    for p in projects:
        date_str = datetime.fromtimestamp(p["latest_mtime"]).strftime("%Y-%m-%d %H:%M")
        plural = "session" if p["session_count"] == 1 else "sessions"
        # Badges in PROVIDER_BADGES order so output is stable regardless of
        # which providers happen to have sessions in this project.
        badges = []
        for provider, code in PROVIDER_BADGES.items():
            n = p["provider_counts"].get(provider, 0)
            if n:
                badges.append(f"{n}{code}")
        badge_str = "+".join(badges) if badges else ""
        line = (
            f"{p['name']:<28} {date_str}   {p['session_count']} {plural}"
            f"  ({badge_str})"
        )
        if p.get("live_count"):
            line = f"{line}  [{p['live_count']} live]"
        if name_counts[p["name"]] > 1 and p["cwd"] is not None:
            line = f"{line}   {p['cwd']}"
        p["display"] = line

    return projects


def get_project_display_name(folder_name):
    """Convert encoded folder name to readable project name.

    Claude Code stores projects in folders like:
    - -home-user-projects-myproject -> myproject
    - -mnt-c-Users-name-Projects-app -> app

    For nested paths under common roots (home, projects, code, Users, etc.),
    extracts the meaningful project portion.
    """
    # Common path prefixes to strip
    prefixes_to_strip = [
        "-home-",
        "-mnt-c-Users-",
        "-mnt-c-users-",
        "-Users-",
    ]

    name = folder_name
    for prefix in prefixes_to_strip:
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix) :]
            break

    # Split on dashes and find meaningful parts
    parts = name.split("-")

    # Common intermediate directories to skip
    skip_dirs = {"projects", "code", "repos", "src", "dev", "work", "documents"}

    # Find the first meaningful part (after skipping username and common dirs)
    meaningful_parts = []
    found_project = False

    for i, part in enumerate(parts):
        if not part:
            continue
        # Skip the first part if it looks like a username (before common dirs)
        if i == 0 and not found_project:
            # Check if next parts contain common dirs
            remaining = [p.lower() for p in parts[i + 1 :]]
            if any(d in remaining for d in skip_dirs):
                continue
        if part.lower() in skip_dirs:
            found_project = True
            continue
        meaningful_parts.append(part)
        found_project = True

    if meaningful_parts:
        return "-".join(meaningful_parts)

    # Fallback: return last non-empty part or original
    for part in reversed(parts):
        if part:
            return part
    return folder_name


def find_all_sessions(folder, include_agents=False):
    """Find all sessions in a Claude projects folder, grouped by project.

    Returns a list of project dicts, each containing:
    - name: display name for the project
    - path: Path to the project folder
    - sessions: list of session dicts with path, summary, mtime, size

    Sessions are sorted by modification time (most recent first) within each project.
    Projects are sorted by their most recent session.
    """
    folder = Path(folder)
    if not folder.exists():
        return []

    projects = {}

    for session_file in folder.glob("**/*.jsonl"):
        # Skip agent files unless requested
        if not include_agents and session_file.name.startswith("agent-"):
            continue

        # Get summary and skip boring sessions
        summary = get_session_summary(session_file)
        if summary.lower() == "warmup" or summary == "(no summary)":
            continue

        # Get project folder
        project_folder = session_file.parent
        project_key = project_folder.name

        if project_key not in projects:
            projects[project_key] = {
                "name": get_project_display_name(project_key),
                "path": project_folder,
                "sessions": [],
            }

        stat = session_file.stat()
        projects[project_key]["sessions"].append(
            {
                "path": session_file,
                "summary": summary,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )

    # Sort sessions within each project by mtime (most recent first)
    for project in projects.values():
        project["sessions"].sort(key=lambda s: s["mtime"], reverse=True)

    # Convert to list and sort projects by most recent session
    result = list(projects.values())
    result.sort(
        key=lambda p: p["sessions"][0]["mtime"] if p["sessions"] else 0, reverse=True
    )

    return result
