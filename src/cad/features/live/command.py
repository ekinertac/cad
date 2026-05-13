"""
features/live/command.py — the ``cad live`` click subcommand.

Full-screen interactive dashboard of every running agent session,
auto-refreshing every 2 seconds. Enter brings the terminal tab
running the highlighted session to the foreground (iTerm2 today);
falls back to peek when focus can't run. Resume is intentionally not
bound — see ``core.providers.resume_session`` for the guardrail and
``focus.py`` for why Enter does what it does.
"""

import click

from ...core.util import _loading_message
from .entries import _build_live_entries


def _lookup(name):
    """Resolve ``name`` on the top-level ``cad`` module at call time.
    Lets tests ``monkeypatch.setattr(cad, "select_entry", …)`` etc.
    even after this command file binds its own imports at startup."""
    from ... import __dict__ as cad_ns

    return cad_ns[name]


def _peek(session):
    """Indirect through cad.peek_session so tests monkeypatching it
    take effect."""
    _lookup("peek_session")(session)


@click.command("live")
def live_cmd():
    """Interactive dashboard of running agent sessions across all
    projects. Refreshes every 2 seconds so working/input/idle
    transitions surface without re-running the command.

    Rows are grouped by project (project name shown inline on each
    row) and sorted within each group by state priority — anything
    needing attention floats up.

    - ``[working]`` (green dot) — last JSONL write within 10s
      (claude is producing output or running a tool right now).
    - ``[input]`` (yellow dot) — alive, no recent writes within 5 min
      (claude printed its turn; waiting on you).
    - ``[idle]`` (dim dot) — alive but stale for 5+ min (probably
      forgotten about).

    Enter peeks the highlighted session: opens its conversation so far
    in $PAGER (read-only, won't disturb the running agent). Resume is
    intentionally NOT bound here — every row by definition has an
    agent process writing to its JSONL, and spawning a second one
    would corrupt the conversation. Switch to the original terminal
    or close it before resuming via `cad local`.

    Esc / q quits.
    """
    # Indirect through cad.* so test monkeypatches on the top-level
    # module hit the right targets even though the canonical
    # implementations live elsewhere.
    select_entry = _lookup("select_entry")
    focus_live_session = _lookup("focus_live_session")
    build_entries = _lookup("_build_live_entries")

    with _loading_message("Loading live sessions..."):
        entries = build_entries()
    if not entries:
        click.echo("No live agent sessions.")
        return

    picked = select_entry(
        entries,
        # Enter = "go to this session". First try to bring the
        # terminal tab running it to the foreground (iTerm2 today,
        # agamon/others can plug in later via ``focus_live_session``).
        # If that's not possible, fall back to peek so Enter is never
        # a silent no-op. Resume is NOT bound here — spawning a second
        # agent on a live JSONL would corrupt the conversation.
        actions={"enter": "go"},
        refresh_callback=build_entries,
        refresh_interval=2.0,
        # No pagination on the live dashboard — the user wants to see
        # every running session at a glance, not a 18-row window of
        # them. The window grows to fit content.
        page_size=None,
        # Take over the terminal (alternate screen buffer) — the
        # dashboard is a dedicated TUI view, not a one-shot prompt
        # that should print into scrollback.
        full_screen=True,
    )
    if picked is None:
        return
    session, _ = picked
    if focus_live_session(session):
        return
    # No terminal integration matched (or no PID to match against);
    # show the user what's in the session instead of doing nothing.
    _peek(session)
