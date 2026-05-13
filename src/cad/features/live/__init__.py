"""
features/live/ — the running-process dashboard and live-indicator
machinery.

Three internal modules:

- :mod:`detection`: ``find_live_claude_state`` + the pure
  ``_annotate_sessions_with_live_state``. Pgrep / lsof / ps lives
  here. Plus :func:`default_annotator` which bundles the two so
  ``core.projects.find_local_projects`` can be given live indicators
  without core knowing anything about detection.
- :mod:`focus`: ``focus_live_session`` + ``_resolve_pid_tty`` + the
  iTerm2 AppleScript. Used by ``cad live``'s Enter handler to bring
  the right terminal tab to the front.
- :mod:`entries`: ``_build_live_entries`` — turns a flat live-session
  list into the grouped entries the picker renders.
- :mod:`command`: the ``cad live`` click subcommand.

External callers should only need ``default_annotator`` (for
:func:`core.projects.find_local_projects`) and :func:`register` (for
the CLI).
"""

from .detection import (
    _annotate_sessions_with_live_state,
    default_annotator,
    find_live_claude_state,
)
from .entries import _build_live_entries
from .focus import focus_live_session


def register(cli):
    """Attach this feature's commands to the top-level click group.
    Idempotent — calling twice is fine; click rejects duplicate
    subcommand names with a clear error."""
    from .command import live_cmd

    cli.add_command(live_cmd, name="live")


__all__ = [
    "_annotate_sessions_with_live_state",
    "_build_live_entries",
    "default_annotator",
    "find_live_claude_state",
    "focus_live_session",
    "register",
]
