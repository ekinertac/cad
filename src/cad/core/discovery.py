"""
core/discovery.py — per-provider session discovery.

One function per agent: each ``find_<provider>_sessions()`` returns a
flat list of session dicts in the provider-agnostic shape the rest of
cad expects::

    {
        "provider":   "claude" | "codex" | "pi" | "opencode" | "forge",
        "session_id": str,            # what the provider's resume command needs
        "filepath":   Path,
        "cwd":        str,            # absolute path
        "mtime":      float,          # epoch seconds
        "size":       int,            # bytes (0 for DB-backed providers)
        "summary":    str | None,     # lazy — load_session_summary fills
        "name":       str | None,     # user-given title (claude /rename, pi --name)
        "display":    str | None,     # lazy — built in projects.py
    }

Adding a new provider means writing one of these and wiring it into
``find_local_projects``. Per-provider summary extractors live here too
(``get_codex_summary``, ``get_pi_summary``) because they're the only
callers of provider-specific JSONL knowledge outside the core/session_model.py
helpers.

Filters applied during discovery:

- claude: drop sessions without a recoverable ``cwd`` (warmups, broken
  files, agent-* invocations). Drop sessions containing
  ``queue-operation`` events — those are programmatic ``claude -p``
  calls from user hooks; not interactively resumable.
- codex / pi: drop sessions whose first event doesn't expose both
  ``id`` and ``cwd``.
- opencode / forge (SQLite-backed): drop rows without a recoverable
  ``directory`` / ``<current_working_directory>``.

May import from: ``core.session_model``, plus stdlib. May NOT import
from: ``features/``.
"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .session_model import get_session_cwd, get_session_summary


def find_local_sessions(folder, limit=10):
    """Find recent JSONL session files in the given folder.

    Returns a list of (Path, summary) tuples sorted by modification time.
    Excludes agent files and warmup/empty sessions.

    Pass ``limit=None`` to return every matching session uncapped — used by
    the two-step ``local`` picker once a project has been chosen.
    """
    folder = Path(folder)
    if not folder.exists():
        return []

    results = []
    for f in folder.glob("**/*.jsonl"):
        if f.name.startswith("agent-"):
            continue
        summary = get_session_summary(f)
        # Skip boring/empty sessions
        if summary.lower() == "warmup" or summary == "(no summary)":
            continue
        results.append((f, summary))

    # Sort by modification time, most recent first
    results.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    return results[:limit]


def _is_claude_queue_operation_session(filepath, scan_lines=50):
    """Return True if the JSONL contains a ``queue-operation`` event in
    its first ``scan_lines`` lines. Those are programmatic ``claude -p``
    invocations (typically from SessionEnd / UserPromptSubmit hooks
    writing digests, auto-titles, etc.). Claude's own ``--resume`` picker
    hides them, so cad does too — they're never interactively resumable
    and would otherwise show up as confusing "phantom" sessions.

    Bounded scan because some interactive sessions are huge (multi-MB);
    queue-operation events appear near the start of programmatic files.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= scan_lines:
                    return False
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and d.get("type") == "queue-operation":
                    return True
    except OSError:
        pass
    return False


def find_claude_sessions(projects_folder):
    """Return a flat list of claude session dicts under ``projects_folder``.

    Each dict has the provider-agnostic shape used by the project picker::

        {
            "provider":   "claude",
            "session_id": "<uuid>",          # for `claude --resume`
            "filepath":   Path(...),
            "cwd":        "/Users/x/Code/foo",
            "mtime":      1700000000.0,
            "size":       42000,
            "summary":    None,              # lazy — load_session_summary fills
            "display":    None,              # lazy — generated from summary
        }

    Filters out:

    - Sessions without a recoverable ``cwd`` (warmups, broken files,
      agent-* invocations) — can't be grouped into a project.
    - Sessions containing ``queue-operation`` events. Those are
      programmatic ``claude -p`` invocations (typically from user hooks
      doing post-session digests, auto-titles, etc.) and aren't
      interactively resumable — claude's own ``--resume`` hides them,
      so cad matches that behaviour to avoid phantom rows.
    """
    projects_folder = Path(projects_folder)
    if not projects_folder.exists():
        return []

    out = []
    for f in projects_folder.glob("*/*.jsonl"):
        if f.name.startswith("agent-"):
            continue
        cwd = get_session_cwd(f)
        if not cwd:
            continue
        if _is_claude_queue_operation_session(f):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        out.append(
            {
                "provider": "claude",
                "session_id": f.stem,
                "filepath": f,
                "cwd": cwd,
                "mtime": st.st_mtime,
                "size": st.st_size,
                "summary": None,
                "name": None,
                "display": None,
            }
        )
    return out


def find_codex_sessions(root=None):
    """Return a flat list of codex session dicts under ``root`` (default
    ``~/.codex/sessions``). Codex stores sessions in a date-organized tree
    (``YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``) and the first JSONL line is
    a ``session_meta`` event whose ``payload`` carries both ``id`` (for
    ``codex resume``) and ``cwd`` — so a single readline is enough.

    Same dict shape as :func:`find_claude_sessions`, with provider="codex".
    """
    if root is None:
        root = Path.home() / ".codex" / "sessions"
    root = Path(root)
    if not root.exists():
        return []

    out = []
    for f in root.glob("*/*/*/rollout-*.jsonl"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                meta = json.loads(fh.readline())
        except (OSError, json.JSONDecodeError):
            continue
        payload = (meta or {}).get("payload") or {}
        sid = payload.get("id")
        cwd = payload.get("cwd")
        if not sid or not cwd:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        out.append(
            {
                "provider": "codex",
                "session_id": sid,
                "filepath": f,
                "cwd": cwd,
                "mtime": st.st_mtime,
                "size": st.st_size,
                "summary": None,
                "name": None,
                "display": None,
            }
        )
    return out


def get_codex_summary(filepath, max_length=200):
    """First user prompt from a codex JSONL — the first ``event_msg`` whose
    payload type is ``user_message``. Returns ``"(no prompt)"`` if none
    found. Codex injects synthetic user messages for skill-loader warnings
    at session start, so the first real user prompt is sometimes the second
    or third user_message event; the function scans linearly which is good
    enough for the picker.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                if d.get("type") != "event_msg":
                    continue
                p = d.get("payload") or {}
                if p.get("type") != "user_message":
                    continue
                msg = (p.get("message") or "").strip()
                if not msg:
                    continue
                first_line = msg.split("\n", 1)[0]
                if len(first_line) > max_length:
                    first_line = first_line[: max_length - 3] + "..."
                return first_line
    except OSError:
        pass
    return "(no prompt)"


def find_pi_sessions(root=None):
    """pi sessions live at ``~/.pi/agent/sessions/<encoded-cwd>/<ts>_<uuid>.jsonl``.
    Line 1 is a ``session`` event with ``id`` and ``cwd`` directly — same
    cheap-readline pattern as codex.
    """
    if root is None:
        root = Path.home() / ".pi" / "agent" / "sessions"
    root = Path(root)
    if not root.exists():
        return []

    out = []
    for f in root.glob("*/*.jsonl"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                meta = json.loads(fh.readline())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        sid = meta.get("id")
        cwd = meta.get("cwd")
        if not sid or not cwd:
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        out.append(
            {
                "provider": "pi",
                "session_id": sid,
                "filepath": f,
                "cwd": cwd,
                "mtime": st.st_mtime,
                "size": st.st_size,
                "summary": None,
                # Pi persists a user-given --name in the session_meta line.
                "name": meta.get("name") or None,
                "display": None,
            }
        )
    return out


def get_pi_summary(filepath, max_length=200):
    """First user prompt in a pi JSONL. Pi wraps messages as
    ``{"type":"message","message":{"role":"user","content":[{...}]}}`` with
    content as a list of typed blocks (``{"type":"text","text":"..."}``).
    """
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                if d.get("type") != "message":
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    text = " ".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                elif isinstance(content, str):
                    text = content
                else:
                    text = ""
                text = text.strip()
                if not text:
                    continue
                first_line = text.split("\n", 1)[0]
                if len(first_line) > max_length:
                    first_line = first_line[: max_length - 3] + "..."
                return first_line
    except OSError:
        pass
    return "(no prompt)"


def find_opencode_sessions(db_path=None):
    """opencode stores sessions in a SQLite DB at
    ``~/.local/share/opencode/opencode.db``. The ``session`` table already
    has ``directory`` (cwd), ``title`` (a usable summary), and
    ``time_updated`` (epoch seconds), so no JSONL parsing needed.
    """
    if db_path is None:
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    out = []
    try:
        # Read-only mode so we don't fight with a running opencode process.
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            for row in conn.execute(
                "SELECT id, title, directory, time_updated FROM session"
            ):
                sid, title, directory, time_updated = row
                if not sid or not directory:
                    continue
                out.append(
                    {
                        "provider": "opencode",
                        "session_id": sid,
                        # The DB row IS the source of truth — no on-disk
                        # filepath to point at. Use the DB path as a
                        # placeholder so size/dependent code doesn't crash.
                        "filepath": db_path,
                        "cwd": directory,
                        # opencode stores time as epoch milliseconds.
                        "mtime": float(time_updated or 0) / 1000.0,
                        "size": 0,
                        # opencode already records a title; carry it through
                        # so load_session_summary doesn't need to re-read.
                        "summary": title or "(no title)",
                        "name": None,
                        "display": None,
                    }
                )
    except Exception:
        # Any DB error → just return what we have, don't crash discovery.
        pass
    return out


# Forge embeds the cwd inside its conversation context blob (system prompt
# tagged section). This regex is the cheapest reliable way to extract it
# without parsing the entire JSON tree.
_FORGE_CWD_RE = re.compile(
    r"<current_working_directory>([^<]+)</current_working_directory>"
)


def find_forge_sessions(db_path=None):
    """forge stores conversations in a SQLite DB at ``~/forge/.forge.db``.
    The ``conversations`` table has ``conversation_id``, ``title``,
    ``updated_at``, and a ``context`` JSON blob. cwd lives inside the blob
    as ``<current_working_directory>`` tags — a regex extraction is cheaper
    than parsing the full tree (which can be hundreds of KB).
    """
    if db_path is None:
        db_path = Path.home() / "forge" / ".forge.db"
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    out = []
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            for row in conn.execute(
                "SELECT conversation_id, title, context, updated_at "
                "FROM conversations"
            ):
                cid, title, context, updated_at = row
                if not cid or not context:
                    continue
                m = _FORGE_CWD_RE.search(context)
                if not m:
                    # Conversation without a recoverable cwd — skip; we
                    # can't group or chdir for it.
                    continue
                cwd = m.group(1).strip()
                # updated_at is a TIMESTAMP string like "2026-05-10 21:00:00".
                # Best-effort parse, fallback to 0 (will sort to bottom).
                try:
                    mtime = datetime.fromisoformat(
                        (updated_at or "").replace("Z", "+00:00")
                    ).timestamp()
                except (ValueError, AttributeError, TypeError):
                    mtime = 0.0
                out.append(
                    {
                        "provider": "forge",
                        "session_id": cid,
                        "filepath": db_path,
                        "cwd": cwd,
                        "mtime": mtime,
                        "size": 0,
                        "summary": title or "(no title)",
                        "name": None,
                        "display": None,
                    }
                )
    except Exception:
        pass
    return out
