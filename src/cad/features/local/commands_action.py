"""
features/local/commands_action.py — action-as-command CLI surface.

Every interactive shortcut on the session picker has a sibling CLI
command here, so each action is scriptable and can be aliased /
piped without going through the picker. Same operations, same
underlying primitives — the CLI commands are thin wrappers around
:func:`resolve_session_id` plus a direct call to the same function
the picker action calls.

Subcommands defined here:

- ``cad resume    <ref>``     — exec ``claude --resume`` (replaces the process)
- ``cad new       [<cwd>]``   — exec fresh ``claude`` in cwd
- ``cad peek      <ref>``     — open in ``$PAGER``
- ``cad rename    <ref> <text>`` — set title override
- ``cad summarize <ref>``     — codex-pipe + save title
- ``cad move      <ref> <cwd>`` — set cwd override
- ``cad archive   <ref>``     — mv to ``~/.cad/archive/``
- ``cad restore   <ref>``     — inverse of archive

``<ref>`` accepts a full UUID, a unique prefix, ``@last``, or
``@live`` (see :mod:`features.local.resolve`).

``resume`` and ``new`` use ``os.execvp`` — they replace the cad
process. Calling them from a shell script works, but if the user
wants the parent shell's cwd to follow the agent's exit, the
``cad shell-init`` wrapper still needs to be sourced.
"""

from pathlib import Path

import click

from ...core.overrides import save_cwd_override, save_title_override
from ...core.providers import new_session as _new_session
from ...core.providers import resume_session as _resume_session
from ..archive.store import (
    ArchiveError,
    restore_session as _restore_session,
)
from .actions import peek_session as _peek_session
from .actions import summarize_session as _summarize_session
from .resolve import (
    AmbiguousSessionRef,
    NoLiveSession,
    SessionNotFound,
    resolve_session_id,
)


def _resolve_or_die(ref, *, cwd=None):
    """Resolve ``ref`` to a session dict or raise a friendly
    :class:`click.ClickException` so the CLI exits with a clean
    message instead of a traceback."""
    try:
        return resolve_session_id(ref, cwd=cwd)
    except SessionNotFound as e:
        raise click.ClickException(str(e))
    except AmbiguousSessionRef as e:
        raise click.ClickException(str(e))
    except NoLiveSession as e:
        raise click.ClickException(str(e))


# --- shared option that every ref-taking command accepts ---


def _ref_arg():
    return click.argument("ref")


def _cwd_option():
    return click.option(
        "--cwd",
        "scope_cwd",
        help="Only consider sessions in this project (exact cwd match).",
    )


# --- subcommands ---


@click.command("resume")
@_ref_arg()
@_cwd_option()
def resume_cmd(ref, scope_cwd):
    """Resume the session matching REF.

    REF can be a full UUID, a unique prefix, ``@last``, or ``@live``.
    Replaces the cad process with the agent CLI (``claude --resume`` etc.)
    so the shell wrapper from ``cad shell-init`` still controls
    post-exit cwd.
    """
    session = _resolve_or_die(ref, cwd=scope_cwd)
    _resume_session(session)


@click.command("peek")
@_ref_arg()
@_cwd_option()
def peek_cmd(ref, scope_cwd):
    """Open the session matching REF in ``$PAGER`` (read-only)."""
    session = _resolve_or_die(ref, cwd=scope_cwd)
    _peek_session(session)


@click.command("rename")
@_ref_arg()
@click.argument("title")
@_cwd_option()
def rename_cmd(ref, title, scope_cwd):
    """Set TITLE as the cad-side title for the session matching REF.

    Stored in ``~/.cad/titles.json`` — same sidecar the picker's ``r``
    action writes. Pass an empty TITLE (in quotes) to clear the override.
    """
    session = _resolve_or_die(ref, cwd=scope_cwd)
    save_title_override(session["provider"], session["session_id"], title)
    if title:
        click.echo(f"Renamed {session['session_id']} → {title!r}")
    else:
        click.echo(f"Cleared title override for {session['session_id']}")


@click.command("summarize")
@_ref_arg()
@_cwd_option()
def summarize_cmd(ref, scope_cwd):
    """Pipe REF's session through ``codex exec`` and save the
    3-7-word title it returns. Same logic the picker's ``s`` action
    runs.
    """
    session = _resolve_or_die(ref, cwd=scope_cwd)
    click.echo(f"Summarizing {session['session_id']}...")
    title = _summarize_session(session)
    save_title_override(session["provider"], session["session_id"], title)
    click.echo(f"Saved title: {title}")


@click.command("move")
@_ref_arg()
@click.argument("new_cwd")
@_cwd_option()
def move_cmd(ref, new_cwd, scope_cwd):
    """Move REF to NEW_CWD in cad (sidecar override, agent files
    untouched). Pass an empty NEW_CWD to clear the override.
    """
    session = _resolve_or_die(ref, cwd=scope_cwd)
    # Normalise the user's input — `~` expansion, absolute path.
    target = ""
    if new_cwd:
        path = Path(new_cwd).expanduser().resolve()
        if not path.is_dir():
            raise click.ClickException(f"Not a directory: {path}")
        target = str(path)
    save_cwd_override(session["provider"], session["session_id"], target)
    if target:
        click.echo(f"Moved {session['session_id']} → {target}")
    else:
        click.echo(f"Cleared cwd override for {session['session_id']}")


@click.command("new")
@click.argument("cwd", required=False)
def new_cmd(cwd):
    """Start a fresh claude session in CWD (default: current dir).

    Replaces the cad process with ``claude --dangerously-skip-permissions``.
    """
    target = cwd or str(Path.cwd())
    _new_session(target)


@click.command("restore")
@click.argument("ref")
def restore_cmd(ref):
    """Restore an archived session matching REF back to ~/.claude/projects/.

    REF resolves against ``~/.cad/archive/`` (not the live discovery
    set), so a prefix match looks at archived filenames.
    """
    from ..archive.store import find_archived_sessions

    archived = find_archived_sessions()
    matches = [s for s in archived if s["session_id"].startswith(ref)] or [
        s for s in archived if s["session_id"] == ref
    ]
    if not matches:
        raise click.ClickException(f"No archived session matches {ref!r}.")
    if len(matches) > 1:
        ids = ", ".join(s["session_id"] for s in matches[:5])
        raise click.ClickException(
            f"{ref!r} matches {len(matches)} archived sessions: {ids}"
        )
    try:
        dest = _restore_session(matches[0])
    except ArchiveError as e:
        raise click.ClickException(str(e))
    click.echo(f"Restored to {dest}")


def register_actions(cli):
    """Attach every action subcommand to the click group. Called
    from the local feature's main register() so the surface is
    contiguous. `cad archive` lives in the archive feature itself
    (which dual-routes between picker and action) so we don't add it
    here."""
    cli.add_command(resume_cmd, name="resume")
    cli.add_command(peek_cmd, name="peek")
    cli.add_command(rename_cmd, name="rename")
    cli.add_command(summarize_cmd, name="summarize")
    cli.add_command(move_cmd, name="move")
    cli.add_command(new_cmd, name="new")
    cli.add_command(restore_cmd, name="restore")
