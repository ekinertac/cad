"""
features/archive/command.py — the ``cad archive`` click subcommand.

Picker over every session currently in ``~/.cad/archive/``. Three
actions:

- ``Enter`` (``restore``) — move the JSONL back to its original
  ``~/.claude/projects/<encoded-cwd>/`` location. ``claude --resume``
  and ``cad local`` see it again immediately.
- ``p`` (``peek``) — same read-only pager view used by the regular
  session picker.
- ``D`` (``delete``) — permanent ``rm``. Capital-D on purpose: the
  lowercase ``d`` on the regular picker is the archive action, so
  capitalising here means "this one really is the destructive
  option". Confirm prompt before the unlink.

All cross-module helpers (peek, picker, prompts) are looked up via
``cad.__dict__`` at call time so test monkeypatches on the cad
top-level keep working.
"""

import click

from ...core.util import _loading_message


def _lookup(name):
    """Resolve ``name`` on the top-level ``cad`` module at call time.
    Same pattern as features/live/command.py — see its docstring."""
    from ... import __dict__ as cad_ns

    return cad_ns[name]


@click.command("archive")
def archive_cmd():
    """Browse and restore archived sessions.

    Sessions move into the archive when you press ``d`` on the regular
    session picker. They live at ``~/.cad/archive/<session-id>.jsonl``
    until you either restore them (Enter), permanently delete them
    (``D``), or ``mv`` them somewhere else by hand.

    The archive is intentionally a flat directory so a plain
    ``ls ~/.cad/archive/`` shows you everything cad has on hand.
    """
    find_archived_sessions = _lookup("find_archived_sessions")
    restore_session = _lookup("restore_session")
    select_entry = _lookup("select_entry")
    load_session_summary = _lookup("load_session_summary")
    peek_session = _lookup("peek_session")
    prompt_confirm = _lookup("prompt_confirm")
    ArchiveError = _lookup("ArchiveError")

    with _loading_message("Loading archive..."):
        sessions = find_archived_sessions()

    if not sessions:
        click.echo("Archive is empty. Press `d` on a session in `cad` to archive it.")
        return

    # Hydrate display strings only once we have the list — same lazy
    # pattern the local picker uses.
    for s in sessions:
        load_session_summary(s)

    selected_idx = 0
    while True:
        if not sessions:
            click.echo("Archive is empty.")
            return
        picked = select_entry(
            sessions,
            actions={"enter": "restore", "p": "peek", "D": "delete"},
            initial_selected=selected_idx,
        )
        if picked is None:
            return
        session, action = picked

        try:
            selected_idx = sessions.index(session)
        except ValueError:
            selected_idx = 0

        if action == "peek":
            peek_session(session)
            continue

        if action == "restore":
            try:
                dest = restore_session(session)
            except ArchiveError as e:
                click.echo(f"Restore failed: {e}", err=True)
                continue
            click.echo(f"Restored to {dest}")
            try:
                sessions.remove(session)
            except ValueError:
                pass
            selected_idx = min(selected_idx, max(0, len(sessions) - 1))
            continue

        if action == "delete":
            if not prompt_confirm(
                f"Permanently delete {session['session_id']}? "
                "(NOT reversible — file goes away for good)"
            ):
                continue
            try:
                session["filepath"].unlink()
            except OSError as e:
                click.echo(f"Delete failed: {e}", err=True)
                continue
            click.echo(f"Deleted {session['filepath']}")
            try:
                sessions.remove(session)
            except ValueError:
                pass
            selected_idx = min(selected_idx, max(0, len(sessions) - 1))
            continue
