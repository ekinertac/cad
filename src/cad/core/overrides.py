"""
core/overrides.py — cad's own title and cwd sidecar storage.

cad reads from each agent's storage but never writes back to it. The
two pieces of user-controlled state we DO own (the ``r`` rename
action and the ``m`` move action) land here, in two flat JSON files
under ``~/.cad/``:

- ``titles.json``: ``{"<provider>:<session_id>": "<title>"}``
- ``cwd-overrides.json``: ``{"<provider>:<session_id>": "<absolute path>"}``

Plus a one-time migration helper that renames the legacy ``~/.cct/``
directory to ``~/.cad/`` so existing users carry their overrides
across the tool rename.

Atomic writes via :func:`core.util._atomic_write_json` so a kill mid-write
leaves the previous file plus a ``.bak`` intact.

May import from: ``core.util``. May NOT import from: ``features/``.
"""

import json
import shutil
from pathlib import Path

from .util import _atomic_write_json


def _migrate_legacy_sidecar_dir():
    """One-time best-effort: rename ~/.cct → ~/.cad so users carry their
    title and cwd overrides across the tool rename. Idempotent — does
    nothing if .cad already exists. Silent on permission errors; the user
    can ``mv`` manually in that case.
    """
    old = Path.home() / ".cct"
    new = Path.home() / ".cad"
    if old.is_dir() and not new.exists():
        try:
            shutil.move(str(old), str(new))
        except OSError:
            pass


def _titles_file():
    """Sidecar storage for cad's title overrides — set via the picker's `r`
    (rename) or `s` (summarize) actions. Keyed by ``<provider>:<session_id>``
    so it's uniform across all agents and never touches the agent's own
    storage. JSON for trivial hand-inspection.
    """
    _migrate_legacy_sidecar_dir()
    return Path.home() / ".cad" / "titles.json"


def _load_titles():
    f = _titles_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_title_override(provider, session_id, title):
    """Write a user-provided title for one session to the sidecar. Empty
    or falsy ``title`` removes the override."""
    titles = _load_titles()
    key = f"{provider}:{session_id}"
    if title:
        titles[key] = title
    else:
        titles.pop(key, None)
    _atomic_write_json(_titles_file(), titles)


def get_title_override(session):
    return _load_titles().get(f"{session['provider']}:{session['session_id']}")


def _cwd_overrides_file():
    """Sidecar storage for cad's per-session cwd overrides — set via the
    picker's ``m`` (move) action. Lets the user reassign which project a
    session belongs to without modifying the agent's own session files.
    Same key shape as the titles sidecar.
    """
    _migrate_legacy_sidecar_dir()
    return Path.home() / ".cad" / "cwd-overrides.json"


def _load_cwd_overrides():
    f = _cwd_overrides_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cwd_override(provider, session_id, cwd):
    """Move a session into a different cad project. Empty/falsy ``cwd``
    removes the override (i.e. the session goes back to wherever its
    agent file recorded it). Path is normalised before storing so we can
    rely on string equality for the Global Sessions match later.
    """
    overrides = _load_cwd_overrides()
    key = f"{provider}:{session_id}"
    if cwd:
        overrides[key] = str(Path(cwd).expanduser().resolve())
    else:
        overrides.pop(key, None)
    _atomic_write_json(_cwd_overrides_file(), overrides)


def get_cwd_override(provider, session_id):
    return _load_cwd_overrides().get(f"{provider}:{session_id}")


def _apply_cwd_override(session):
    """If a cwd override exists for this session, swap it into the dict.
    Called from each discovery function so downstream grouping/resume
    logic stays oblivious to where the override came from.
    """
    override = get_cwd_override(session["provider"], session["session_id"])
    if override:
        session["cwd"] = override
