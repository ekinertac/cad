"""
features/local/actions.py — action handlers triggered from the
session picker (and reused by features/live for its Enter fallback).

Two handlers live here:

- :func:`peek_session`: render the conversation to a temp markdown
  file and open in ``$PAGER`` (less by default). Opens at the
  bottom (most recent turns) since for a live session that's what
  matters. Quick Look-style: pager exits, temp file cleaned up,
  cad's terminal state restored.
- :func:`summarize_session`: pipe a session excerpt to ``codex exec
  --ephemeral`` and return the 3-7 word title it produces. Uses
  codex specifically because it auths via ChatGPT account (so users
  with depleted Anthropic API credits can still summarize, which
  ``claude -p`` requires).
"""

import os
import subprocess
import tempfile
from pathlib import Path

import click

from ...core.session_model import (
    _read_session_excerpt_for_summary,
    get_session_transcript,
)


def peek_session(session):
    """Render the session's prompts/replies to a temp markdown file and
    open it in ``$PAGER`` (fallback ``less``). Blocks until the user
    quits the pager, then unlinks the temp file. less uses the alternate
    screen so cad's terminal state is restored on exit — Quick Look-style.

    Opens at the *bottom* of the file (most recent turns) since that's
    what's relevant for an in-progress live session. Scroll up to see
    earlier history.
    """
    transcript = get_session_transcript(session)
    if not transcript:
        click.echo(f"Peek not yet supported for {session['provider']} sessions.")
        return

    lines = [
        f"# {session['provider']} session {session['session_id']}",
        f"# {session['cwd']}",
        "",
    ]
    for role, text in transcript:
        lines.append(f"## {role.capitalize()}")
        lines.append("")
        lines.append(text)
        lines.append("")

    fd, path = tempfile.mkstemp(prefix="cad-peek-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        pager = os.environ.get("PAGER") or "less"
        args = [pager]
        # less-specific flags: `-R` passes ANSI colours, `+G` opens at
        # end of file so the user sees the most recent turns first.
        # Skip if $PAGER is something else (we can't assume its flags).
        if Path(pager).name == "less":
            args += ["-R", "+G"]
        args.append(path)
        try:
            subprocess.run(args)
        except FileNotFoundError:
            click.echo(f"Pager not found: {pager}", err=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def summarize_session(session):
    """Generate a short title for the session by piping an excerpt to the
    locally-installed ``codex`` CLI in non-interactive mode.

    Codex over a ChatGPT account is the cheapest path here: ``claude -p``
    insists on the Anthropic API even when the Claude Code subscription
    is active, so users with depleted API credits can't use it. Codex
    exec uses the same auth as their interactive codex sessions. The
    final stdout line is the agent's reply (codex prints framing info on
    stderr that we discard).

    Raises :class:`click.ClickException` on timeout, missing binary,
    non-zero exit, or empty output.
    """
    excerpt = _read_session_excerpt_for_summary(session)
    if not excerpt:
        raise click.ClickException("Could not read session content to summarize.")

    prompt = (
        "Generate a concise 3-7 word title that captures what this session "
        "was about. Respond with the title only — no quotes, no trailing "
        "punctuation, no preamble.\n\n"
        f"<session>\n{excerpt}\n</session>"
    )

    try:
        result = subprocess.run(
            # --ephemeral keeps the throwaway summarization call from
            # leaving a session file under ~/.codex/sessions, which would
            # otherwise pollute cct's own session list.
            ["codex", "exec", "--ephemeral", prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise click.ClickException(
            "`codex` not found on PATH — summarize uses it as the LLM."
        )
    except subprocess.TimeoutExpired:
        raise click.ClickException("Summarize timed out after 60s.")

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise click.ClickException(
            f"codex exited with code {result.returncode}: {stderr[-200:]}"
        )

    # Take the last non-empty line — codex's reply is the final stdout chunk,
    # any preceding lines are typically status output. Strip wrapping quotes
    # the model sometimes adds despite the instruction.
    lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
    title = (lines[-1] if lines else "").strip("\"'").strip()
    if not title:
        raise click.ClickException("codex returned an empty title.")
    return title
