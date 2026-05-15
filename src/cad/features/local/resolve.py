"""
features/local/resolve.py — turn a user-supplied session reference
into a real session dict.

The action-as-command CLI surface (``cad resume <ref>``,
``cad peek <ref>``, ``cad archive <ref>``, ...) takes a single
``<ref>`` argument that may be:

- A full session UUID (claude format: ``abc1-2345-...``).
- A prefix — matches if exactly one session's id starts with it.
- ``@last`` — the most-recently-modified session globally, or scoped
  to ``--cwd`` when provided.
- ``@live`` — the currently-running session (only useful when
  exactly one is live; ambiguous otherwise).

Plus an optional ``cwd=`` argument that scopes prefix and ``@last``
lookups to one project (used by the ``--cwd`` flag on the action
commands).

Three failure modes get distinct exception types so the CLI layer
can render a precise error message:

- :class:`SessionNotFound` — nothing matches.
- :class:`AmbiguousSessionRef` — prefix or ``@live`` resolves to more
  than one candidate.
- :class:`NoLiveSession` — ``@live`` resolves to zero.

May import from: ``core/``, ``cad.__dict__`` (for the live-state
hook, kept indirect so monkeypatch.setattr(cad, "find_live_claude_state", …)
still works in tests and so this resolver doesn't take a hard
dependency on features/live at import time).
"""


class SessionNotFound(Exception):
    """Raised when no session matches the given reference."""


class AmbiguousSessionRef(Exception):
    """Raised when a prefix or @live could mean more than one session."""


class NoLiveSession(Exception):
    """Raised when @live is requested but no claude process is running."""


def _all_sessions(cwd=None):
    """Walk every provider's session list. Optionally filter by cwd."""
    from ...core.discovery import (
        find_claude_sessions,
        find_codex_sessions,
        find_forge_sessions,
        find_opencode_sessions,
        find_pi_sessions,
    )
    from pathlib import Path

    sessions = (
        find_claude_sessions(Path.home() / ".claude" / "projects")
        + find_codex_sessions()
        + find_pi_sessions()
        + find_opencode_sessions()
        + find_forge_sessions()
    )
    if cwd is not None:
        sessions = [s for s in sessions if s.get("cwd") == cwd]
    return sessions


def _resolve_at_live():
    """Look up the running claude state via the cad top-level
    namespace so tests can monkeypatch find_live_claude_state. Returns
    the unique bound session_id, or raises NoLiveSession /
    AmbiguousSessionRef."""
    from ... import __dict__ as cad_ns

    state = cad_ns["find_live_claude_state"]()
    bound = list(state.get("bound_uuids", {}).keys())
    if not bound:
        raise NoLiveSession(
            "No live claude session right now. Resolve with a UUID or " "@last instead."
        )
    if len(bound) > 1:
        raise AmbiguousSessionRef(
            f"@live matches {len(bound)} running sessions; pass a "
            f"UUID prefix to disambiguate: {', '.join(bound)}"
        )
    return bound[0]


def resolve_session_id(ref, *, cwd=None):
    """Look up the session dict that ``ref`` refers to. See module
    docstring for the supported ``ref`` shapes and exception types.
    """
    if not ref:
        raise SessionNotFound("Empty session reference.")

    sessions = _all_sessions(cwd=cwd)
    by_id = {s["session_id"]: s for s in sessions}

    if ref == "@live":
        sid = _resolve_at_live()
        # The live session must also be in our local discovery — if
        # not, the cwd scope removed it. Tell the user.
        if sid not in by_id:
            raise SessionNotFound(
                f"@live session {sid} isn't in the discovered set " f"(cwd filter?)."
            )
        return by_id[sid]

    if ref == "@last":
        if not sessions:
            raise SessionNotFound("No sessions found.")
        # Newest by mtime.
        return max(sessions, key=lambda s: s.get("mtime", 0))

    # Full-id exact match wins.
    if ref in by_id:
        return by_id[ref]

    # Prefix match. Unique → return; multiple → error with the list
    # so the user can see what they need to type more of.
    candidates = [s for s in sessions if s["session_id"].startswith(ref)]
    if not candidates:
        raise SessionNotFound(f"No session matches {ref!r}.")
    if len(candidates) > 1:
        matched = ", ".join(s["session_id"] for s in candidates[:5])
        more = "" if len(candidates) <= 5 else f" (and {len(candidates) - 5} more)"
        raise AmbiguousSessionRef(
            f"{ref!r} matches {len(candidates)} sessions: {matched}{more}"
        )
    return candidates[0]
