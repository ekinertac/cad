"""
features/archive/store.py — move sessions into ~/.cad/archive/ and back.

Three operations:

- :func:`archive_session` — move a claude session's JSONL out of
  ``~/.claude/projects/<encoded-cwd>/<sid>.jsonl`` into
  ``~/.cad/archive/<sid>.jsonl``. Refuses to archive a session that
  is currently live (claude has an open handle on the file) or one
  that isn't claude (other providers aren't file-based yet). On
  filename collision, appends a timestamp.
- :func:`restore_session` — the inverse: move a previously-archived
  JSONL back to its original encoded-cwd location. The cwd comes
  from the session dict, not the file path, because the archive
  filename only carries the session_id.
- :func:`find_archived_sessions` — list everything currently in the
  archive in the standard session-dict shape, so the
  ``cad archive`` picker can render them with the same machinery as
  regular sessions.

Storage layout: a flat ``~/.cad/archive/<session-id>.jsonl`` per
file. The directory is created lazily on the first archive. The
filename keeps the session_id so a manual ``mv`` recovery is
trivial; the original cwd lives inside the JSONL itself (every
claude event line carries a ``cwd`` field) so we don't need a
sidecar manifest to know where to restore to.

The session-shaped dicts returned by :func:`find_archived_sessions`
mirror what :mod:`core.discovery` produces, with two distinctions:

- ``filepath`` points into ``~/.cad/archive/``, not the agent's own
  storage.
- ``cwd`` is read out of the JSONL (via :func:`core.session_model.get_session_cwd`)
  so restore knows where to put it back.
"""

import shutil
from datetime import datetime
from pathlib import Path

from ...core.session_model import get_session_cwd
from ...core.providers import PROVIDER_BADGES  # noqa: F401  (kept for re-export use)


class ArchiveError(Exception):
    """Raised when archive / restore can't proceed (live session,
    unsupported provider, IO failure). Callers translate to a click
    error or a status message."""


def _archive_dir():
    """``~/.cad/archive/``. Created lazily on first archive so a fresh
    install doesn't leave an empty directory hanging around."""
    return Path.home() / ".cad" / "archive"


def archive_session(session):
    """Move ``session["filepath"]`` into the archive dir. Returns the
    new :class:`Path`. Raises :class:`ArchiveError` for live or
    non-claude sessions (see module docstring for the why).

    Filename collisions get a ``-<YYYYmmdd-HHMMSS>`` suffix so a
    second archive of the same session_id doesn't clobber the first.
    """
    provider = session.get("provider")
    if provider != "claude":
        raise ArchiveError(
            f"Archive only supports claude sessions for now (got {provider!r})."
        )
    if session.get("live"):
        raise ArchiveError(
            "Refusing to archive: this session is currently active in another "
            "terminal. Close that terminal first — archiving an open file "
            "would race with claude's writes."
        )
    src = Path(session["filepath"])
    if not src.exists():
        raise ArchiveError(f"Source JSONL no longer exists: {src}")

    dest_dir = _archive_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        # Suffix the new archive entry with a timestamp so the existing
        # one stays intact. Trivial to recover via `mv` either way.
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"{src.stem}-{ts}{src.suffix}"

    shutil.move(str(src), str(dest))
    return dest


def restore_session(session):
    """Move an archived session back to its original
    ``~/.claude/projects/<encoded-cwd>/<sid>.jsonl`` location. The
    encoded cwd is recomputed from ``session["cwd"]`` so the restore
    target matches what claude expects.

    Returns the new :class:`Path`. Raises :class:`ArchiveError` if
    the cwd can't be recovered or the destination already exists.
    """
    from ..project_rename.migrate import _claude_encode_path

    provider = session.get("provider")
    if provider != "claude":
        raise ArchiveError(f"Restore only supports claude sessions (got {provider!r}).")
    src = Path(session["filepath"])
    if not src.exists():
        raise ArchiveError(f"Archived file no longer exists: {src}")

    cwd = session.get("cwd") or get_session_cwd(src)
    if not cwd:
        raise ArchiveError(
            "Can't restore: no cwd recorded in the session, can't reconstruct "
            "the destination path."
        )

    encoded = _claude_encode_path(cwd)
    dest_dir = Path.home() / ".claude" / "projects" / encoded
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        raise ArchiveError(
            f"Destination already exists: {dest}. Move it out of the way and retry."
        )

    shutil.move(str(src), str(dest))
    return dest


def find_archived_sessions():
    """Walk the archive dir and return session-shaped dicts. Reads
    the embedded cwd out of each JSONL so the restore action can use
    it without the caller having to track origins separately."""
    archive_dir = _archive_dir()
    if not archive_dir.exists():
        return []

    out = []
    for f in sorted(archive_dir.glob("*.jsonl")):
        try:
            st = f.stat()
        except OSError:
            continue
        cwd = get_session_cwd(f) or ""
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
    # Newest archives first so the picker starts on the most recent.
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out
