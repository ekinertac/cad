"""
features/local/ — the everyday ``cad`` / ``cad local`` flow.

Two-step picker: project, then session, with per-session actions for
peek, resume, summarize, move, rename, render-to-HTML, and start a
new session. Plus a project-level ``r`` (rename) shortcut backed by
features/project_rename and an ``n`` (new) shortcut backed by
core.providers.

Modules:

- :mod:`actions`: shared action handlers — :func:`peek_session`
  (also consumed by features/live's Enter fallback) and
  :func:`summarize_session`.
- :mod:`command`: the click subcommand and the picker state machine.

Public surface: :func:`register` + the action handlers (so other
features can call them without crossing the feature boundary into
:mod:`command`).
"""

from .actions import peek_session, summarize_session


def register(cli):
    """Attach `cad local` to the click group."""
    from .command import local_cmd

    cli.add_command(local_cmd, name="local")


__all__ = ["peek_session", "register", "summarize_session"]
