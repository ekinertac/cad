"""
features/search/ — cross-session text search.

The title-search built into the picker (``/``) only matches against
each row's summary line. When the user remembers a phrase from inside
a session but not the title, that's not enough. This feature scans
the actual conversation text across every local session and returns
the matching rows with a snippet around the hit.

Two surfaces, one engine:

- :func:`search_sessions` in :mod:`find` — the testable core. Takes
  a query string plus optional scoping (``cwd``, ``provider``,
  ``limit``) and returns hit dicts that are drop-in compatible with
  the regular picker (each carries the same provider / session_id /
  cwd / filepath / mtime keys, plus a ``snippet`` and ``match_count``).
- ``cad search <query>`` in :mod:`command` — thin click wrapper that
  runs the engine, hydrates display strings, and drops the hits into
  the picker with the standard resume/peek/html/etc. actions.

The keyboard sugar ``/?phrase`` inside the regular picker (see
core/picker.py's ``search_callback`` param) calls into the same
:func:`search_sessions` so the engine is exercised by both surfaces.
"""

from .find import search_sessions


def register(cli):
    """Attach `cad search` to the click group."""
    from .command import search_cmd

    cli.add_command(search_cmd, name="search")


__all__ = ["register", "search_sessions"]
