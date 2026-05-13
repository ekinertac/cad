"""
core/providers.py — the provider abstraction.

This is the layer that knows which agents cad supports and how to
launch (resume / start) one. Three concerns live here:

- :data:`PROVIDER_BADGES`: single-letter codes shown on project rows
  (``(6c+5o+1f)``). Adding a provider here is the only place the
  badge layer cares about names.
- :data:`PROVIDER_RESUME_COMMANDS` / :data:`PROVIDER_NEW_COMMANDS`:
  argv prefixes the corresponding subprocess gets exec'd with.
- :func:`resume_session` / :func:`new_session`: the bottom-of-stack
  exec calls that replace the cad process with the agent process.

May import from: ``core.util``. May NOT import from: anything in
``features/``. The feature-local action handlers (``features/local/``)
call into here, never the other way around.
"""

import os
import sys
from pathlib import Path

import click


# Single-letter badge codes for the project-row provider counts. Adding a
# new provider here is the only place the badge layer cares about names.
PROVIDER_BADGES = {
    "claude": "c",
    "codex": "x",
    "pi": "p",
    "opencode": "o",
    "forge": "f",
}


PROVIDER_RESUME_COMMANDS = {
    # The agent CLI to exec for each provider, plus the static flags. The
    # session id is appended at call time. Skip-permissions on claude is
    # intentional — long sessions devolve into rubber-stamping prompts.
    "claude": ["claude", "--dangerously-skip-permissions", "--resume"],
    "codex": ["codex", "resume"],
    "pi": ["pi", "--session"],
    "opencode": ["opencode", "--session"],
    "forge": ["forge", "--conversation-id"],
}


# Commands for starting a fresh session (no resume id). Currently
# claude-only — other agents would slot in here when there's demand.
PROVIDER_NEW_COMMANDS = {
    "claude": ["claude", "--dangerously-skip-permissions"],
}


def resume_session(session):
    """Replace this process with the appropriate provider's resume command
    after chdir'ing to the cwd recorded in the session. Never returns on
    success.

    A child process can't change its parent shell's working directory; the
    optional shell wrapper installed via ``cad shell-init`` reads
    ``$CAD_CWD_FILE`` after the agent exits and cd's the parent shell.

    Guardrail: refuses to resume a session that's already live in another
    terminal. Spawning a second agent process on the same JSONL would
    cause interleaved writes and scramble the conversation order. The
    user must close the other terminal (or use peek) instead.
    """
    if session.get("live"):
        click.echo(
            "Refusing to resume: this session is currently active in "
            "another terminal. Spawning a second agent on the same "
            "session file would corrupt it. Close that terminal first, "
            "or use peek (`p` in the picker) to view it read-only.",
            err=True,
        )
        return

    provider = session["provider"]
    cwd = session["cwd"]
    session_id = session["session_id"]

    if provider not in PROVIDER_RESUME_COMMANDS:
        click.echo(f"Unknown provider: {provider}", err=True)
        sys.exit(1)
    if not Path(cwd).is_dir():
        click.echo(f"Original project directory no longer exists: {cwd}", err=True)
        sys.exit(1)

    click.echo(f"Resuming {provider} session {session_id} in {cwd}...")

    cwd_file = os.environ.get("CAD_CWD_FILE")
    if cwd_file:
        try:
            Path(cwd_file).write_text(cwd)
        except OSError:
            pass

    os.chdir(cwd)
    cmd = PROVIDER_RESUME_COMMANDS[provider] + [session_id]
    os.execvp(cmd[0], cmd)


def new_session(cwd, provider="claude"):
    """Replace this process with the agent CLI in ``cwd``, starting a
    fresh session (no ``--resume``). Same chdir / CAD_CWD_FILE / execvp
    plumbing as :func:`resume_session` so the shell wrapper also picks
    up the new cwd post-exit.
    """
    if provider not in PROVIDER_NEW_COMMANDS:
        click.echo(
            f"Starting a new session isn't wired for {provider} yet — "
            f"cd into the project and run the agent manually.",
            err=True,
        )
        return
    if not Path(cwd).is_dir():
        click.echo(f"Project directory does not exist: {cwd}", err=True)
        return

    click.echo(f"Starting new {provider} session in {cwd}...")

    cwd_file = os.environ.get("CAD_CWD_FILE")
    if cwd_file:
        try:
            Path(cwd_file).write_text(cwd)
        except OSError:
            pass

    os.chdir(cwd)
    cmd = PROVIDER_NEW_COMMANDS[provider]
    os.execvp(cmd[0], cmd)
