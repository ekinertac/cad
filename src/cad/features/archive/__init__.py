"""
features/archive/ — soft-delete sessions to ``~/.cad/archive/``.

The session picker's ``d`` action archives a session: moves its JSONL
out of ``~/.claude/projects/`` into ``~/.cad/archive/`` so it stops
showing up in cad and in ``claude --resume``, while staying recoverable
with ``cad archive``'s restore action (or a plain ``mv`` back if the
user prefers).

Same neighbourhood as the rename backups (``~/.cad/agent-backups/``)
and the same philosophy: cad never permanently deletes anything
without an extra confirmation step.

Modules:

- :mod:`store`: :func:`archive_session`, :func:`restore_session`,
  :func:`find_archived_sessions`, :class:`ArchiveError`. Pure
  filesystem operations with the safety guards.
- :mod:`command`: the ``cad archive`` click subcommand — picker over
  archived sessions with restore / peek / hard-delete actions.

May import from: ``core/``. May NOT import from: sibling features
beyond the shared utility surfaces (peek lives in features/local).
"""

from .store import (
    ArchiveError,
    archive_session,
    find_archived_sessions,
    restore_session,
)


def register(cli):
    """Attach `cad archive` to the click group."""
    from .command import archive_cmd

    cli.add_command(archive_cmd, name="archive")


__all__ = [
    "ArchiveError",
    "archive_session",
    "find_archived_sessions",
    "register",
    "restore_session",
]
