"""cad — Coding Agent Driver.

A single CLI for managing and resuming sessions across multiple local
coding agents (claude, codex, pi, opencode, forge). Originally grew out
of Simon Willison's claude-code-transcripts HTML renderer, which still
lives inside as the `json` / `all` / `web` subcommands.
"""

import json
import html
import os
import platform
import re
import contextlib
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
import webbrowser
from datetime import datetime
from pathlib import Path

import click
from click_default_group import DefaultGroup
import httpx
from jinja2 import Environment, PackageLoader
import markdown
import questionary

# Set up Jinja2 environment
_jinja_env = Environment(
    loader=PackageLoader("cad", "templates"),
    autoescape=True,
)

# Load macros template and expose macros
_macros_template = _jinja_env.get_template("macros.html")
_macros = _macros_template.module


def get_template(name):
    """Get a Jinja2 template by name."""
    return _jinja_env.get_template(name)


# Regex to match git commit output: [branch hash] message
COMMIT_PATTERN = re.compile(r"\[[\w\-/]+ ([a-f0-9]{7,})\] (.+?)(?:\n|$)")

# Regex to detect GitHub repo from git push output (e.g., github.com/owner/repo/pull/new/branch)
GITHUB_REPO_PATTERN = re.compile(
    r"github\.com/([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)/pull/new/"
)

PROMPTS_PER_PAGE = 5
LONG_TEXT_THRESHOLD = (
    300  # Characters - text blocks longer than this are shown in index
)


# Session parsing / transcript extraction lives in core/session_model.py.
# Re-exported for test compatibility and any callers reaching in.
from .core.session_model import (  # noqa: E402,F401
    _extract_role_text,
    _extract_summarizable_text,
    _flatten_content_blocks,
    _get_jsonl_summary,
    _parse_jsonl_file,
    _read_session_excerpt_for_summary,
    extract_text_from_content,
    get_claude_session_metadata,
    get_session_cwd,
    get_session_summary,
    get_session_transcript,
    parse_session_file,
)

# Module-level variable for GitHub repo (set by generate_html)
_github_repo = None

# API constants
API_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


def select_entry(
    entries,
    actions=None,
    back_action=None,
    initial_selected=0,
    refresh_callback=None,
    refresh_interval=2.0,
    page_size=18,
    full_screen=False,
):
    """Interactive list picker with per-key actions and modal search.

    ``actions`` is a dict mapping key names to action labels, e.g.
    ``{"enter": "resume", "h": "transcript"}``. ``enter`` is treated as
    the primary confirm key and gets auto-added if missing. Search mode is
    opened with ``/`` so plain typing can't conflict with letter hotkeys
    (the same pattern fzf/htop use). Inside search mode, typing builds the
    filter; Enter still confirms with the primary action.

    ``back_action`` (optional): when set, Esc and Backspace (outside
    search mode) return ``(None, back_action)`` instead of cancelling.
    ``q`` and Ctrl-C still hard-cancel either way — handy escape hatch.

    ``initial_selected``: 0-based index the cursor starts on. Callers
    that re-enter the picker after a peek/rename pass the previous
    selection here so the user comes back to the same row.

    ``refresh_callback`` (optional): a function ``() -> list[dict]`` that
    recomputes entries. If passed, a daemon thread invokes it every
    ``refresh_interval`` seconds, mutates ``entries`` in place, and
    triggers a redraw. Used by ``cad live`` so process-state changes
    surface without re-running the command.

    Each entry must be a dict with at least ``display`` populated. The full
    entry dict is passed back to the caller so it can dispatch on whatever
    metadata it included. Returns ``(entry, action_name)`` or ``None`` if
    the user cancelled.

    Built directly on prompt_toolkit because questionary's select doesn't
    expose per-key action binding — the alternative would be inconsistent
    search behaviour between pickers, which is exactly what this avoids.
    """
    from prompt_toolkit import Application
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.styles import Style

    if not actions:
        actions = {"enter": "select"}
    else:
        actions = dict(actions)
        actions.setdefault("enter", "select")

    # Clamp initial_selected so a caller passing a stale index against a
    # shorter list doesn't crash the picker.
    start = max(0, min(initial_selected, len(entries) - 1)) if entries else 0
    state = {
        "selected": start,
        "filter": "",
        "search_mode": False,
        "result": None,
    }
    # ``page_size=None`` opts out of pagination — the viewport grows
    # to include every entry. Used by ``cad live`` where the user
    # wants the whole live dashboard on screen at once, no scrolling.
    # Other pickers cap at a fixed window so they don't take over the
    # whole terminal.
    NO_PAGINATION = page_size is None

    def viewport_size():
        return max(1, len(filtered_indices())) if NO_PAGINATION else page_size

    def is_selectable(idx_into_indices, indices):
        """Header rows (``header: True``) are visual separators only —
        skip them when the cursor is moving. ``indices`` is the result
        of ``filtered_indices()``; ``idx_into_indices`` is a position
        within it."""
        if idx_into_indices < 0 or idx_into_indices >= len(indices):
            return False
        return not entries[indices[idx_into_indices]].get("header")

    def next_selectable(indices, start_pos, direction):
        """Walk in ``direction`` (+1 / -1) from ``start_pos`` until a
        selectable row is found or we run off either end."""
        i = start_pos
        n = len(indices)
        while 0 <= i < n:
            if is_selectable(i, indices):
                return i
            i += direction
        return None

    def filtered_indices():
        # When search is active, hide headers — the result is a flat
        # list of matches and a project label without its children would
        # just be noise.
        if state["filter"]:
            needle = state["filter"].lower()
            return [
                i
                for i, e in enumerate(entries)
                if not e.get("header") and needle in e["display"].lower()
            ]
        return list(range(len(entries)))

    def viewport(indices):
        if not indices:
            return [], 0, 0
        if state["selected"] >= len(indices):
            state["selected"] = len(indices) - 1
        if state["selected"] < 0:
            state["selected"] = 0
        # If the cursor landed on a header (e.g. initial start, or
        # filter just cleared), nudge to the nearest selectable row so
        # the picker is never sitting on an unselectable line.
        if not is_selectable(state["selected"], indices):
            target = next_selectable(indices, state["selected"], 1)
            if target is None:
                target = next_selectable(indices, state["selected"], -1)
            if target is not None:
                state["selected"] = target
        vp = viewport_size()
        top = max(0, state["selected"] - vp // 2)
        top = min(top, max(0, len(indices) - vp))
        visible_indices = indices[top : top + vp]
        return visible_indices, top, max(0, len(indices) - top - len(visible_indices))

    def get_list_text():
        indices = filtered_indices()
        if not indices:
            return [("class:hint", "  (no matches)\n")]
        visible, above, below = viewport(indices)
        out = []
        if above > 0:
            out.append(("class:hint", f"  ▲ {above} more above\n"))
        for src_i in visible:
            pos = indices.index(src_i)
            sel = pos == state["selected"]
            entry = entries[src_i]
            # Header rows are visual section dividers — render flush
            # left in a bold style with no arrow/marker columns. The
            # cursor never lands here (see ``is_selectable``), so we
            # don't need to consider ``sel``.
            if entry.get("header"):
                out.append(("class:header", f"{entry['display']}\n"))
                continue
            arrow = "» " if sel else "  "
            # Two-char status marker slot keeps columns aligned. Live
            # sessions render a coloured dot — green=working,
            # yellow=needs input, dim=idle.
            entry_state = entry.get("state")
            if entry.get("live") and entry_state == "working":
                marker, marker_style = "● ", "class:state-working"
            elif entry.get("live") and entry_state == "input":
                marker, marker_style = "● ", "class:state-input"
            elif entry.get("live") and entry_state == "idle":
                marker, marker_style = "● ", "class:state-idle"
            else:
                marker, marker_style = "  ", ""
            # Green-highlight rows touched (rename / summarize) in this cad
            # run so the user can see at a glance what just changed. The
            # cursor row uses the reverse style instead.
            if sel:
                row_style = "class:selected"
            elif entry.get("_recently_updated"):
                row_style = "class:updated"
            else:
                row_style = ""
            # Emit as three segments so the marker keeps its own colour
            # independent of the row style (and reverse-style cursor rows
            # still highlight the whole line).
            out.append((row_style, arrow))
            out.append((marker_style or row_style, marker))
            out.append((row_style, f"{entry['display']}\n"))
        if below > 0:
            out.append(("class:hint", f"  ▼ {below} more below\n"))
        return out

    def get_status_text():
        if state["search_mode"]:
            return [
                (
                    "class:status",
                    f" /{state['filter']}▁  Enter=confirm · Esc=exit search\n",
                )
            ]
        # Render the hint line from the actions dict so it always reflects
        # the configured keys for this picker.
        parts = [f"{'Enter' if k == 'enter' else k}={v}" for k, v in actions.items()]
        parts.append("/=search")
        if back_action:
            parts += ["Esc/Bksp=back", "q=quit"]
        else:
            parts.append("q/Esc=quit")
        return [("class:status", " " + " · ".join(parts) + "\n")]

    kb = KeyBindings()
    not_searching = Condition(lambda: not state["search_mode"])
    is_searching = Condition(lambda: state["search_mode"])

    @kb.add("up")
    def _(event):
        indices = filtered_indices()
        target = next_selectable(indices, state["selected"] - 1, -1)
        if target is not None:
            state["selected"] = target

    @kb.add("down")
    def _(event):
        indices = filtered_indices()
        target = next_selectable(indices, state["selected"] + 1, 1)
        if target is not None:
            state["selected"] = target

    @kb.add("pageup")
    def _(event):
        indices = filtered_indices()
        vp = viewport_size()
        target = next_selectable(indices, max(0, state["selected"] - vp), -1)
        if target is None:
            # No selectable row at-or-before the page-up target; fall
            # forward instead so we don't stick on a header.
            target = next_selectable(indices, 0, 1)
        if target is not None:
            state["selected"] = target

    @kb.add("pagedown")
    def _(event):
        indices = filtered_indices()
        vp = viewport_size()
        target = next_selectable(
            indices, min(len(indices) - 1, state["selected"] + vp), 1
        )
        if target is None:
            target = next_selectable(indices, len(indices) - 1, -1)
        if target is not None:
            state["selected"] = target

    # Dynamic action bindings — one handler per configured key. Enter is
    # always live (even in search mode, where it both confirms the filter
    # and selects). Letter hotkeys only bind outside search mode so typing
    # them while filtering doesn't accidentally pick.
    def _make_action_handler(action_name):
        def _handler(event):
            indices = filtered_indices()
            if not indices:
                return
            # Defensive: if the cursor somehow ended up on a header
            # (shouldn't happen — viewport snaps it off), don't fire.
            if not is_selectable(state["selected"], indices):
                return
            state["search_mode"] = False
            state["result"] = (entries[indices[state["selected"]]], action_name)
            event.app.exit()

        return _handler

    for key, action_name in actions.items():
        handler = _make_action_handler(action_name)
        if key == "enter":
            kb.add(key)(handler)
        else:
            kb.add(key, filter=not_searching)(handler)

    @kb.add("/", filter=not_searching)
    def _(event):
        state["search_mode"] = True
        state["filter"] = ""
        state["selected"] = 0

    @kb.add("escape", eager=True)
    def _(event):
        if state["search_mode"]:
            state["search_mode"] = False
            state["filter"] = ""
            state["selected"] = 0
        elif back_action:
            # Caller wired a "go back" action — route Esc through it
            # instead of hard-cancelling. q / Ctrl-C remain the escape
            # hatch when the user really wants out.
            state["result"] = (None, back_action)
            event.app.exit()
        else:
            event.app.exit()

    # `q` is the unconditional cancel — only outside search mode so the
    # user can still type `q` as part of a filter query.
    @kb.add("q", filter=not_searching)
    def _(event):
        event.app.exit()

    @kb.add("c-c", eager=True)
    def _(event):
        event.app.exit()

    @kb.add("backspace", filter=is_searching)
    def _(event):
        if state["filter"]:
            state["filter"] = state["filter"][:-1]
            state["selected"] = 0

    if back_action:
        # Backspace outside search mode = same as Esc when back is enabled.
        # This matches the user's mental model from file managers / wizards
        # where Backspace navigates up one level.
        @kb.add("backspace", filter=not_searching)
        def _(event):
            state["result"] = (None, back_action)
            event.app.exit()

    @kb.add("<any>", filter=is_searching)
    def _(event):
        char = event.key_sequence[0].data
        if len(char) == 1 and char.isprintable():
            state["filter"] += char
            state["selected"] = 0

    def get_window_height():
        # Recomputed on every render — when ``page_size=None`` the
        # window grows to match the current entry count (plus a couple
        # of slack rows for the ▲/▼ hint lines, even though they
        # won't render in the no-pagination case). Recompute is what
        # lets ``cad live`` resize as projects come and go between
        # refreshes.
        vp = viewport_size() + 2
        return Dimension(min=1, preferred=vp, max=vp)

    list_window = Window(
        content=FormattedTextControl(text=get_list_text),
        height=get_window_height,
        # Long lines clip at the right edge instead of wrapping
        # into multi-row rows that break the layout.
        wrap_lines=False,
    )
    status_window = Window(content=FormattedTextControl(text=get_status_text), height=1)
    # In full-screen mode the picker owns the whole terminal; insert
    # a flexible spacer between the list and the status bar so the
    # status anchors to the bottom edge instead of floating directly
    # under the last entry. Inline pickers keep the tight layout —
    # there's nothing to anchor against in that case.
    if full_screen:
        children = [list_window, Window(), status_window]
    else:
        children = [list_window, status_window]
    layout = Layout(HSplit(children))
    style = Style.from_dict(
        {
            "selected": "reverse",
            "hint": "fg:ansibrightblack",
            "status": "fg:ansicyan",
            # Bright-green for rows just renamed/summarized — non-persistent,
            # only highlights what changed since cad launched.
            "updated": "fg:ansibrightgreen bold",
            # Live-session markers: green = actively producing output,
            # yellow = process alive waiting for the user, grey = alive
            # but stale (probably abandoned).
            "state-working": "fg:ansibrightgreen bold",
            "state-input": "fg:ansiyellow",
            "state-idle": "fg:ansibrightblack",
            # Non-selectable group header rows (used by `cad live` to
            # label each project's session block).
            "header": "fg:ansicyan bold",
        }
    )
    # erase_when_done removes the picker's frame from the terminal on exit
    # so re-entering after a rename/summarize action doesn't leave a stack
    # of duplicate frames in the scrollback.
    #
    # ``full_screen`` switches prompt_toolkit to the terminal's alternate
    # screen buffer — the picker takes over the whole window and the
    # original shell contents are restored on exit. ``cad live`` uses this
    # so the dashboard feels like a dedicated TUI; the inline pickers
    # leave it off so they stay anchored in scrollback like a regular
    # prompt.
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=style,
        full_screen=full_screen,
        erase_when_done=True,
    )

    # Background refresh thread, only when a refresh_callback is wired.
    # Runs as a daemon so it doesn't block process exit; an Event lets us
    # tell it to stop politely once the app's done.
    stop_refresh = None
    if refresh_callback:
        import threading

        stop_refresh = threading.Event()

        def _refresh_loop():
            while not stop_refresh.wait(refresh_interval):
                try:
                    new_entries = refresh_callback()
                except Exception:
                    continue
                # Preserve cursor on the same session-id when possible —
                # otherwise a refresh that adds/removes a row would
                # bounce the user to a different session mid-scroll.
                current_key = None
                idx = state["selected"]
                if 0 <= idx < len(entries):
                    current_key = entries[idx].get("session_id") or entries[idx].get(
                        "display"
                    )
                entries.clear()
                entries.extend(new_entries)
                if current_key is not None:
                    for i, e in enumerate(entries):
                        if (
                            e.get("session_id") == current_key
                            or e.get("display") == current_key
                        ):
                            state["selected"] = i
                            break
                try:
                    app.invalidate()
                except Exception:
                    pass

        threading.Thread(target=_refresh_loop, daemon=True).start()

    try:
        app.run()
    finally:
        if stop_refresh is not None:
            stop_refresh.set()
    return state["result"]


def select_session_action(sessions):
    """Backwards-compatible wrapper: the session picker with the original
    ``resume`` (Enter) / ``html`` (h) actions. Existing tests and call
    sites stay green; new code should use :func:`select_entry` directly.
    """
    return select_entry(sessions, actions={"enter": "resume", "h": "html"})


# Provider abstraction lives in core/providers.py — re-exported so legacy
# imports (`from cad import resume_session` etc.) still resolve.
from .core.providers import (  # noqa: E402,F401
    PROVIDER_BADGES,
    PROVIDER_NEW_COMMANDS,
    PROVIDER_RESUME_COMMANDS,
    new_session,
    resume_session,
)


# `command cad` in the wrapper skips this function so the binary runs once.
# zsh and bash get separate snippets only because the conditional syntax
# differs slightly; the mechanism is identical.
def prompt_for_title(default=""):
    """Prompt the user for a new session title. Uses prompt_toolkit so the
    experience matches the picker. Returns the entered string (possibly
    empty — caller decides what to do with that). Returns ``None`` if
    cancelled (Ctrl-C / EOF).
    """
    from prompt_toolkit import prompt as _ptk_prompt
    from prompt_toolkit.formatted_text import FormattedText

    try:
        text = _ptk_prompt(
            FormattedText([("class:prompt", "Title: ")]),
            default=default,
        )
    except (KeyboardInterrupt, EOFError):
        return None
    return text.strip()


def prompt_confirm(message, default=False):
    """Yes/No prompt used as a guardrail before destructive bulk
    operations. Returns ``True`` only when the user types y/yes.
    Default is ``False`` (the safe answer) — pressing Enter on an
    unread prompt won't accidentally fire the action.
    """
    from prompt_toolkit import prompt as _ptk_prompt
    from prompt_toolkit.formatted_text import FormattedText

    suffix = " [y/N] " if not default else " [Y/n] "
    try:
        text = _ptk_prompt(
            FormattedText([("class:prompt", message + suffix)]),
        )
    except (KeyboardInterrupt, EOFError):
        return False
    text = text.strip().lower()
    if not text:
        return default
    return text in ("y", "yes")


def prompt_for_cwd(default="", must_exist=True, label="New cwd"):
    """Prompt for a directory path. Resolves ``~``. Loops until valid or
    cancelled so any error stays adjacent to the next prompt instead of
    being buried above a freshly-rendered picker.

    ``must_exist=True`` (default): path has to be an existing directory.
    Used by per-session ``m`` where we just want to point at a known dir.

    ``must_exist=False``: path must NOT exist (we'll create it via the
    caller's mv) and its parent must exist. Used by project-level ``r``
    where cad does the full rename including the user-side mv.

    Returns the absolute path string, ``""`` to clear an existing
    override, or ``None`` if cancelled.
    """
    from prompt_toolkit import prompt as _ptk_prompt
    from prompt_toolkit.formatted_text import FormattedText

    current = default
    while True:
        try:
            text = _ptk_prompt(
                FormattedText([("class:prompt", f"{label} (empty to clear): ")]),
                default=current,
            )
        except (KeyboardInterrupt, EOFError):
            return None
        text = text.strip()
        if not text:
            return ""
        path = Path(text).expanduser().resolve()
        if must_exist:
            if path.is_dir():
                return str(path)
            click.echo(f"Not a directory: {path}", err=True)
        else:
            if path.exists():
                click.echo(f"Already exists — would clobber: {path}", err=True)
            elif not path.parent.exists():
                click.echo(f"Parent directory doesn't exist: {path.parent}", err=True)
            else:
                return str(path)
        current = text


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


SHELL_WRAPPERS = {
    "zsh": """\
cad() {
  local cwd_file
  cwd_file=$(mktemp -t cad-cwd.XXXXXX)
  CAD_CWD_FILE="$cwd_file" command cad "$@"
  local rc=$?
  if [[ -s "$cwd_file" ]]; then
    cd "$(< "$cwd_file")" || true
  fi
  rm -f "$cwd_file"
  return $rc
}
""",
    "bash": """\
cad() {
  local cwd_file
  cwd_file=$(mktemp -t cad-cwd.XXXXXX)
  CAD_CWD_FILE="$cwd_file" command cad "$@"
  local rc=$?
  if [ -s "$cwd_file" ]; then
    cd "$(cat "$cwd_file")" || true
  fi
  rm -f "$cwd_file"
  return $rc
}
""",
}


# Temp output constants + helpers live in core/util.py now. Re-export
# the module-level names for any legacy callers (and tests) that import
# them from the top-level cad namespace.
from .core.util import (  # noqa: E402,F401
    TEMP_OUTPUT_KEEP,
    TEMP_OUTPUT_PARENT,
    _atomic_write_json,
    _loading_message,
    _prune_temp_outputs,
    _temp_output_dir,
)


def _global_session_cwds():
    """``cwd`` values that should merge into the virtual 'Global Sessions'
    project — the user's home dir and ~/Code, the two catch-all places for
    one-off questions. Returned as strings since cwd values from JSONLs are
    strings; comparison is exact-match.
    """
    home = str(Path.home())
    return {home, str(Path(home) / "Code")}


def _find_project_for_cwd(projects, cwd):
    """Return the project that 'owns' the given cwd, or None.

    Match rules, in order:

    1. Skip if cwd is a global cwd (``~/`` or ``~/Code``) — those are
       Global Sessions territory; we don't auto-pick the catch-all bucket.
    2. Exact match against any project's cwd.
    3. Longest-prefix match — a project at ``/Users/x/Code/foo`` claims
       any subdir like ``/Users/x/Code/foo/sub/dir``. The deepest match
       wins when multiple ancestors are projects.
    """
    if cwd in _global_session_cwds():
        return None
    for p in projects:
        if p["cwd"] == cwd:
            return p
    candidates = [
        p for p in projects if p["cwd"] and cwd.startswith(p["cwd"].rstrip("/") + "/")
    ]
    if candidates:
        return max(candidates, key=lambda p: len(p["cwd"]))
    return None


# Per-provider discovery lives in core/discovery.py — re-exported for
# legacy import paths and tests.
from .core.discovery import (  # noqa: E402,F401
    _FORGE_CWD_RE,
    _is_claude_queue_operation_session,
    find_claude_sessions,
    find_codex_sessions,
    find_forge_sessions,
    find_local_sessions,
    find_opencode_sessions,
    find_pi_sessions,
    get_codex_summary,
    get_pi_summary,
)


def _migrate_legacy_sidecar_dir():
    """One-time best-effort: rename ~/.cct → ~/.cad so users carry their
    title and cwd overrides across the tool rename. Idempotent — does
    nothing if .cad already exists. Silent on permission errors; the user
    can ``mv`` manually in that case.
    """
    old = Path.home() / ".cct"
    new = Path.home() / ".cad"
    if old.is_dir() and not new.exists():
        try:
            shutil.move(str(old), str(new))
        except OSError:
            pass


def _titles_file():
    """Sidecar storage for cad's title overrides — set via the picker's `r`
    (rename) or `s` (summarize) actions. Keyed by ``<provider>:<session_id>``
    so it's uniform across all agents and never touches the agent's own
    storage. JSON for trivial hand-inspection.
    """
    _migrate_legacy_sidecar_dir()
    return Path.home() / ".cad" / "titles.json"


def _load_titles():
    f = _titles_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_title_override(provider, session_id, title):
    """Write a user-provided title for one session to the sidecar. Empty
    or falsy ``title`` removes the override."""
    titles = _load_titles()
    key = f"{provider}:{session_id}"
    if title:
        titles[key] = title
    else:
        titles.pop(key, None)
    _atomic_write_json(_titles_file(), titles)


def get_title_override(session):
    return _load_titles().get(f"{session['provider']}:{session['session_id']}")


def _cwd_overrides_file():
    """Sidecar storage for cad's per-session cwd overrides — set via the
    picker's ``m`` (move) action. Lets the user reassign which project a
    session belongs to without modifying the agent's own session files.
    Same key shape as the titles sidecar.
    """
    _migrate_legacy_sidecar_dir()
    return Path.home() / ".cad" / "cwd-overrides.json"


def _load_cwd_overrides():
    f = _cwd_overrides_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cwd_override(provider, session_id, cwd):
    """Move a session into a different cad project. Empty/falsy ``cwd``
    removes the override (i.e. the session goes back to wherever its
    agent file recorded it). Path is normalised before storing so we can
    rely on string equality for the Global Sessions match later.
    """
    overrides = _load_cwd_overrides()
    key = f"{provider}:{session_id}"
    if cwd:
        overrides[key] = str(Path(cwd).expanduser().resolve())
    else:
        overrides.pop(key, None)
    _atomic_write_json(_cwd_overrides_file(), overrides)


def get_cwd_override(provider, session_id):
    return _load_cwd_overrides().get(f"{provider}:{session_id}")


def _apply_cwd_override(session):
    """If a cwd override exists for this session, swap it into the dict.
    Called from each discovery function so downstream grouping/resume
    logic stays oblivious to where the override came from.
    """
    override = get_cwd_override(session["provider"], session["session_id"])
    if override:
        session["cwd"] = override


# Most claude builds set argv[0] to the version string ("2.1.138") and the
# real binary is "claude". `pgrep -x claude` matches the basename, which is
# the most portable signal we have. The regex extracts the resume UUID
# from argv so we can map a process directly to a session id.
_CLAUDE_RESUME_ARG_RE = re.compile(r"--resume\s+([0-9a-f-]{36})")


# Total wall-clock budget for live detection. lsof can hang on a single
# weird PID (NFS, locked fd, kernel state); a hard ceiling guarantees
# the picker is never blocked for more than this even if one lsof call
# stalls. Set CAD_NO_LIVE=1 in env to skip detection entirely.
_LIVE_DETECTION_BUDGET_SEC = 2.0
_LIVE_DETECTION_PER_CALL_SEC = 1.0


def find_live_claude_state():
    """Inspect running claude processes to discover which sessions are
    live. Returns a dict::

        {
            "bound_uuids": {uuid: {"pid": int, "cwd": str}, ...},
            "unbound_cwds": {cwd: pid_count, ...},
        }

    "Bound" means the process was started with ``--resume <uuid>`` so we
    can map it precisely. "Unbound" means a fresh ``claude`` (no resume
    flag); we know which project is live but not which specific JSONL —
    the caller resolves that heuristically by binding to the most recent
    JSONL(s) under the project's folder.

    Best-effort with a hard total time budget: any subprocess error
    (pgrep/lsof/ps missing, slow, or denied), or breaching the budget,
    silently returns the empty state. The picker still works; it just
    won't show live indicators. Set ``CAD_NO_LIVE=1`` to skip entirely.
    """
    empty = {"bound_uuids": {}, "unbound_cwds": {}}
    if os.environ.get("CAD_NO_LIVE"):
        return empty

    deadline = time.monotonic() + _LIVE_DETECTION_BUDGET_SEC

    try:
        result = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True,
            text=True,
            timeout=_LIVE_DETECTION_PER_CALL_SEC,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return empty
    if result.returncode not in (0, 1):
        return empty

    bound = {}
    unbound = {}
    for pid_str in result.stdout.split():
        if time.monotonic() > deadline:
            # Out of budget. Return whatever we've gathered so far rather
            # than block the picker any longer.
            break
        try:
            pid = int(pid_str)
        except ValueError:
            continue

        cwd = None
        try:
            # `-P -n` skips port-number and IP-to-hostname resolution.
            # Without them lsof does blocking reverse-DNS for every open
            # network socket — measured at 8s vs 0.03s for one claude on
            # the developer's machine. We only care about the `cwd` row
            # so DNS is pure overhead.
            lsof_out = subprocess.run(
                ["lsof", "-Pn", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=_LIVE_DETECTION_PER_CALL_SEC,
            ).stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        for line in lsof_out.splitlines():
            parts = line.split(None, 8)
            # lsof column layout: COMMAND PID USER FD TYPE DEVICE SIZE NODE NAME
            if len(parts) >= 9 and parts[3] == "cwd":
                cwd = parts[-1]
                break
        if not cwd:
            continue

        args = ""
        try:
            args = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=_LIVE_DETECTION_PER_CALL_SEC,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

        match = _CLAUDE_RESUME_ARG_RE.search(args)
        if match:
            bound[match.group(1)] = {"pid": pid, "cwd": cwd}
        else:
            unbound[cwd] = unbound.get(cwd, 0) + 1

    return {"bound_uuids": bound, "unbound_cwds": unbound}


def _annotate_sessions_with_live_state(sessions, live_state, now=None):
    """Tag each session in place with ``live`` (bool) and ``state``
    (``working`` / ``input`` / ``idle``). Pure function (no I/O beyond
    reading session["mtime"]) so it's trivially testable.

    State classification for live (process-alive) sessions by JSONL mtime:
    - ``working`` (<10s): still streaming tokens or running a tool
    - ``input`` (10s-5min): claude printed its turn and is at the prompt
      waiting for the user
    - ``idle`` (>5min): alive but stale — probably forgotten about

    Non-live sessions are always ``idle``.
    """
    if now is None:
        now = time.time()
    bound = live_state.get("bound_uuids", {})
    unbound = live_state.get("unbound_cwds", {})
    WORKING_WINDOW = 10  # seconds — still streaming
    INPUT_WINDOW = 300  # 5 minutes — within reach of user; older = idle

    def _state_from_mtime(s):
        age = now - s["mtime"]
        if age < WORKING_WINDOW:
            return "working"
        if age < INPUT_WINDOW:
            return "input"
        return "idle"

    # Default everything to idle first.
    for s in sessions:
        s["live"] = False
        s["state"] = "idle"
        s["pid"] = None

    # Bound: each --resume uuid maps to exactly one session.
    for s in sessions:
        if s["provider"] == "claude" and s["session_id"] in bound:
            s["live"] = True
            s["state"] = _state_from_mtime(s)
            # Carry the PID forward — downstream features (terminal
            # focus, future kill / attach actions) need to reach the
            # actual process and can't realistically re-shell pgrep.
            s["pid"] = bound[s["session_id"]].get("pid")

    # Unbound: for each cwd with N fresh claudes, bind to the N most
    # recently-modified claude JSONLs in that cwd that aren't already
    # bound by a --resume match.
    by_cwd = defaultdict(list)
    for s in sessions:
        if s["provider"] == "claude" and not s["live"]:
            by_cwd[s["cwd"]].append(s)
    for cwd, n_unbound in unbound.items():
        candidates = sorted(
            by_cwd.get(cwd, []), key=lambda x: x["mtime"], reverse=True
        )[:n_unbound]
        for s in candidates:
            s["live"] = True
            s["state"] = _state_from_mtime(s)


def _claude_encode_path(path):
    """Replicate Claude Code's directory-encoding scheme. Both ``/`` and
    ``.`` are replaced with ``-`` — e.g. ``/Users/x/Code/humbl.ai`` becomes
    ``-Users-x-Code-humbl-ai``. Verified against folders on disk."""
    return re.sub(r"[/.]", "-", str(path))


# State that claude maintains in parallel directories alongside projects/.
# Each is keyed by the same encoded path. We move all that exist for a
# given project — leaving any behind would let claude see stale references.
_CLAUDE_STATE_DIRS = ("projects", "file-history", "todos", "shell-snapshots")


def migrate_claude_project(old_cwd, new_cwd, backup_root=None, dry_run=False):
    """Move a claude project's on-disk state from one cwd to another.

    1. Backup the four ``~/.claude/<dir>/<old_enc>/`` trees (where they
       exist) into ``backup_root`` so the user has a one-command undo.
    2. Move each ``~/.claude/<dir>/<old_enc>/`` to ``<new_enc>/``. If the
       destination already exists, merge file-by-file (existing files at
       the destination win — we never overwrite).
    3. Rewrite the ``cwd`` field in every JSONL line under the new
       ``projects/<new_enc>/`` directory.

    Mechanism is from https://www.vincentschmalbach.com/migrate-claude-code-sessions-to-a-new-computer/
    cross-checked against claude's actual filter behaviour (claude --resume
    filters its picker by cwd inside the JSONL, not by folder name).

    Returns a dict::

        {
            "moved_dirs":      [(old_path, new_path), ...],
            "rewritten_files": [Path, ...],
            "backup_dir":      Path | None,
            "skipped":         ["projects exists at target", ...],
        }
    """
    old_enc = _claude_encode_path(old_cwd)
    new_enc = _claude_encode_path(new_cwd)
    if old_enc == new_enc:
        raise click.ClickException(
            "Old and new cwd encode to the same path — nothing to migrate."
        )

    claude_root = Path.home() / ".claude"
    result = {
        "moved_dirs": [],
        "rewritten_files": [],
        "backup_dir": None,
        "skipped": [],
    }

    # Phase 1 — backup. Only copy what actually exists; don't create empty
    # backup trees for state dirs that don't apply to this project.
    if backup_root and not dry_run:
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        backup_dir = backup_root / f"claude-migrate-{ts}"
        for base in _CLAUDE_STATE_DIRS:
            src = claude_root / base / old_enc
            if src.exists():
                dst = backup_dir / base / old_enc
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dst)
        result["backup_dir"] = backup_dir if backup_dir.exists() else None

    # Phase 2 — move each state dir if it exists.
    for base in _CLAUDE_STATE_DIRS:
        src = claude_root / base / old_enc
        dst = claude_root / base / new_enc
        if not src.exists():
            continue
        if dst.exists():
            # Merge: move individual entries, skipping any name collisions
            # so we never clobber an existing file in the destination.
            for entry in src.iterdir():
                target = dst / entry.name
                if target.exists():
                    result["skipped"].append(f"{base}/{new_enc}/{entry.name} exists")
                    continue
                if not dry_run:
                    shutil.move(str(entry), str(target))
            # Best-effort cleanup of the (now hopefully empty) source dir.
            try:
                if not dry_run:
                    src.rmdir()
            except OSError:
                pass
            result["moved_dirs"].append((src, dst))
        else:
            if not dry_run:
                shutil.move(str(src), str(dst))
            result["moved_dirs"].append((src, dst))

    # Phase 3 — rewrite cwd inside every JSONL under the new projects dir.
    new_project_dir = claude_root / "projects" / new_enc
    if new_project_dir.exists():
        old_cwd_str = str(old_cwd)
        new_cwd_str = str(new_cwd)
        for jsonl in new_project_dir.glob("*.jsonl"):
            if dry_run:
                result["rewritten_files"].append(jsonl)
                continue
            # Preserve mtime — the session content didn't semantically
            # change, only its location label. Lets cad's sort order
            # remain stable across a migration.
            stat = jsonl.stat()
            text = jsonl.read_text(encoding="utf-8")
            new_text = text.replace(f'"cwd":"{old_cwd_str}"', f'"cwd":"{new_cwd_str}"')
            if new_text != text:
                jsonl.write_text(new_text, encoding="utf-8")
                os.utime(jsonl, (stat.st_atime, stat.st_mtime))
                result["rewritten_files"].append(jsonl)

    return result


def load_session_summary(session):
    """Populate ``session['summary']`` and ``session['display']`` in place.

    For JSONL-based providers (claude, codex, pi) the summary is loaded
    lazily here — discovery only reads cwd + mtime, leaving the heavier
    first-prompt scan for once the user actually chose a project.
    SQLite-based providers (opencode, forge) already set the summary at
    discovery time because their schema makes it cheap; for those we just
    skip the scan and go straight to display building.

    The cct sidecar override (set by ``r``/``s`` actions in the picker)
    wins over any provider-derived summary.
    """
    if session["display"] is not None:
        return
    override = get_title_override(session)
    if override:
        session["summary"] = override
    elif session["summary"] is None:
        if session["provider"] == "claude":
            # Single pass picks up summary AND any user-set /rename name.
            meta = get_claude_session_metadata(session["filepath"])
            session["summary"] = meta["summary"]
            if not session.get("name"):
                session["name"] = meta["name"]
        elif session["provider"] == "codex":
            session["summary"] = get_codex_summary(session["filepath"])
        elif session["provider"] == "pi":
            session["summary"] = get_pi_summary(session["filepath"])
        else:
            session["summary"] = "(unknown provider)"

    mtime = datetime.fromtimestamp(session["mtime"])
    date_str = mtime.strftime("%Y-%m-%d %H:%M")
    size_kb = session["size"] / 1024
    summary_one_line = re.sub(r"\s+", " ", session["summary"]).strip()
    # User-named sessions (claude /rename, pi --name) get a prominent
    # "provider/Name" prefix separated from the implicit prompt by an
    # em-dash. Unnamed rows keep the trailing slash so layout aligns.
    name = session.get("name")
    if name:
        prefix = f"{session['provider']}/{name} — "
    else:
        prefix = f"{session['provider']}/ "
    session["display"] = f"{date_str}  {size_kb:5.0f} KB  {prefix}{summary_one_line}"


def find_local_projects(folder=None):
    """Discover all sessions across providers and group them by ``cwd`` into
    project dicts for the two-step picker.

    Grouping key is the JSONL's / DB's recorded ``cwd`` rather than the
    encoded folder name, so sessions from any agent in the same directory
    end up in a single project entry. Sessions whose cwd matches the
    'Global Sessions' rule (home or ~/Code) collapse into a single virtual
    entry.

    The ``folder`` argument is accepted for backward compatibility but is
    typically unused — providers know their own canonical roots.

    Each project dict::

        {
            "name":          "foo",                      # cwd basename, or "Global Sessions"
            "cwd":           "/Users/x/Code/foo",        # None for the virtual entry
            "sessions":      [<session dict>, ...],      # sorted newest-first
            "session_count": 3,
            "latest_mtime":  1700000000.0,
            "provider_counts": {"claude": 2, "codex": 1},
            "display":       "foo            2026-05-10 14:02   3 sessions  (2c+1x)",
        }
    """
    if folder is None:
        folder = Path.home() / ".claude" / "projects"
    sessions = (
        find_claude_sessions(folder)
        + find_codex_sessions()
        + find_pi_sessions()
        + find_opencode_sessions()
        + find_forge_sessions()
    )
    # Apply user-set cwd overrides before grouping so a moved session
    # appears under its new project — agent files are never modified.
    for s in sessions:
        _apply_cwd_override(s)
    # Tag claude sessions that are currently running so the picker can
    # show a live indicator. Best-effort; degrades to all-idle if the
    # platform doesn't have pgrep/lsof.
    _annotate_sessions_with_live_state(sessions, find_live_claude_state())
    return _group_sessions_into_projects(sessions)


def _group_sessions_into_projects(sessions):
    """Pure grouping function — given a flat session list (any providers),
    group by cwd, apply the Global Sessions rule, and build display rows.
    Split out from ``find_local_projects`` so it's trivially testable.
    """
    global_cwds = _global_session_cwds()
    by_cwd = defaultdict(list)
    global_sessions = []

    for s in sessions:
        if s["cwd"] in global_cwds:
            global_sessions.append(s)
        else:
            by_cwd[s["cwd"]].append(s)

    def _build(name, cwd, sess):
        sess.sort(key=lambda x: x["mtime"], reverse=True)
        counts = defaultdict(int)
        for s in sess:
            counts[s["provider"]] += 1
        # Project-level live count comes from session-level annotations
        # already applied by _annotate_sessions_with_live_state.
        live_count = sum(1 for s in sess if s.get("live"))
        # Project state reflects the most active session: working beats
        # input beats idle. Picker uses this to colour the row marker.
        if any(s.get("live") and s.get("state") == "working" for s in sess):
            project_state = "working"
        elif any(s.get("live") and s.get("state") == "input" for s in sess):
            project_state = "input"
        else:
            project_state = "idle"
        return {
            "name": name,
            "cwd": cwd,
            "sessions": sess,
            "session_count": len(sess),
            "latest_mtime": sess[0]["mtime"],
            "provider_counts": dict(counts),
            "live_count": live_count,
            "state": project_state,
        }

    projects = [
        _build(Path(cwd).name or cwd, cwd, sess) for cwd, sess in by_cwd.items()
    ]
    if global_sessions:
        projects.append(_build("Global Sessions", None, global_sessions))

    projects.sort(key=lambda p: p["latest_mtime"], reverse=True)

    # Disambiguate name collisions (two real projects whose basename matches)
    # by appending the full cwd to the colliding rows only.
    name_counts = defaultdict(int)
    for p in projects:
        name_counts[p["name"]] += 1

    for p in projects:
        date_str = datetime.fromtimestamp(p["latest_mtime"]).strftime("%Y-%m-%d %H:%M")
        plural = "session" if p["session_count"] == 1 else "sessions"
        # Badges in PROVIDER_BADGES order so output is stable regardless of
        # which providers happen to have sessions in this project.
        badges = []
        for provider, code in PROVIDER_BADGES.items():
            n = p["provider_counts"].get(provider, 0)
            if n:
                badges.append(f"{n}{code}")
        badge_str = "+".join(badges) if badges else ""
        line = (
            f"{p['name']:<28} {date_str}   {p['session_count']} {plural}"
            f"  ({badge_str})"
        )
        if p.get("live_count"):
            line = f"{line}  [{p['live_count']} live]"
        if name_counts[p["name"]] > 1 and p["cwd"] is not None:
            line = f"{line}   {p['cwd']}"
        p["display"] = line

    return projects


def get_project_display_name(folder_name):
    """Convert encoded folder name to readable project name.

    Claude Code stores projects in folders like:
    - -home-user-projects-myproject -> myproject
    - -mnt-c-Users-name-Projects-app -> app

    For nested paths under common roots (home, projects, code, Users, etc.),
    extracts the meaningful project portion.
    """
    # Common path prefixes to strip
    prefixes_to_strip = [
        "-home-",
        "-mnt-c-Users-",
        "-mnt-c-users-",
        "-Users-",
    ]

    name = folder_name
    for prefix in prefixes_to_strip:
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix) :]
            break

    # Split on dashes and find meaningful parts
    parts = name.split("-")

    # Common intermediate directories to skip
    skip_dirs = {"projects", "code", "repos", "src", "dev", "work", "documents"}

    # Find the first meaningful part (after skipping username and common dirs)
    meaningful_parts = []
    found_project = False

    for i, part in enumerate(parts):
        if not part:
            continue
        # Skip the first part if it looks like a username (before common dirs)
        if i == 0 and not found_project:
            # Check if next parts contain common dirs
            remaining = [p.lower() for p in parts[i + 1 :]]
            if any(d in remaining for d in skip_dirs):
                continue
        if part.lower() in skip_dirs:
            found_project = True
            continue
        meaningful_parts.append(part)
        found_project = True

    if meaningful_parts:
        return "-".join(meaningful_parts)

    # Fallback: return last non-empty part or original
    for part in reversed(parts):
        if part:
            return part
    return folder_name


def find_all_sessions(folder, include_agents=False):
    """Find all sessions in a Claude projects folder, grouped by project.

    Returns a list of project dicts, each containing:
    - name: display name for the project
    - path: Path to the project folder
    - sessions: list of session dicts with path, summary, mtime, size

    Sessions are sorted by modification time (most recent first) within each project.
    Projects are sorted by their most recent session.
    """
    folder = Path(folder)
    if not folder.exists():
        return []

    projects = {}

    for session_file in folder.glob("**/*.jsonl"):
        # Skip agent files unless requested
        if not include_agents and session_file.name.startswith("agent-"):
            continue

        # Get summary and skip boring sessions
        summary = get_session_summary(session_file)
        if summary.lower() == "warmup" or summary == "(no summary)":
            continue

        # Get project folder
        project_folder = session_file.parent
        project_key = project_folder.name

        if project_key not in projects:
            projects[project_key] = {
                "name": get_project_display_name(project_key),
                "path": project_folder,
                "sessions": [],
            }

        stat = session_file.stat()
        projects[project_key]["sessions"].append(
            {
                "path": session_file,
                "summary": summary,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )

    # Sort sessions within each project by mtime (most recent first)
    for project in projects.values():
        project["sessions"].sort(key=lambda s: s["mtime"], reverse=True)

    # Convert to list and sort projects by most recent session
    result = list(projects.values())
    result.sort(
        key=lambda p: p["sessions"][0]["mtime"] if p["sessions"] else 0, reverse=True
    )

    return result


def generate_batch_html(
    source_folder, output_dir, include_agents=False, progress_callback=None
):
    """Generate HTML archive for all sessions in a Claude projects folder.

    Creates:
    - Master index.html listing all projects
    - Per-project directories with index.html listing sessions
    - Per-session directories with transcript pages

    Args:
        source_folder: Path to the Claude projects folder
        output_dir: Path for output archive
        include_agents: Whether to include agent-* session files
        progress_callback: Optional callback(project_name, session_name, current, total)
            called after each session is processed

    Returns statistics dict with total_projects, total_sessions, failed_sessions, output_dir.
    """
    source_folder = Path(source_folder)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find all sessions
    projects = find_all_sessions(source_folder, include_agents=include_agents)

    # Calculate total for progress tracking
    total_session_count = sum(len(p["sessions"]) for p in projects)
    processed_count = 0
    successful_sessions = 0
    failed_sessions = []

    # Process each project
    for project in projects:
        project_dir = output_dir / project["name"]
        project_dir.mkdir(exist_ok=True)

        # Process each session
        for session in project["sessions"]:
            session_name = session["path"].stem
            session_dir = project_dir / session_name

            # Generate transcript HTML with error handling
            try:
                generate_html(session["path"], session_dir)
                successful_sessions += 1
            except Exception as e:
                failed_sessions.append(
                    {
                        "project": project["name"],
                        "session": session_name,
                        "error": str(e),
                    }
                )

            processed_count += 1

            # Call progress callback if provided
            if progress_callback:
                progress_callback(
                    project["name"], session_name, processed_count, total_session_count
                )

        # Generate project index
        _generate_project_index(project, project_dir)

    # Generate master index
    _generate_master_index(projects, output_dir)

    return {
        "total_projects": len(projects),
        "total_sessions": successful_sessions,
        "failed_sessions": failed_sessions,
        "output_dir": output_dir,
    }


def _generate_project_index(project, output_dir):
    """Generate index.html for a single project."""
    template = get_template("project_index.html")

    # Format sessions for template
    sessions_data = []
    for session in project["sessions"]:
        mod_time = datetime.fromtimestamp(session["mtime"])
        sessions_data.append(
            {
                "name": session["path"].stem,
                "summary": session["summary"],
                "date": mod_time.strftime("%Y-%m-%d %H:%M"),
                "size_kb": session["size"] / 1024,
            }
        )

    html_content = template.render(
        project_name=project["name"],
        sessions=sessions_data,
        session_count=len(sessions_data),
        css=CSS,
        js=JS,
    )

    output_path = output_dir / "index.html"
    output_path.write_text(html_content, encoding="utf-8")


def _generate_master_index(projects, output_dir):
    """Generate master index.html listing all projects."""
    template = get_template("master_index.html")

    # Format projects for template
    projects_data = []
    total_sessions = 0

    for project in projects:
        session_count = len(project["sessions"])
        total_sessions += session_count

        # Get most recent session date
        if project["sessions"]:
            most_recent = datetime.fromtimestamp(project["sessions"][0]["mtime"])
            recent_date = most_recent.strftime("%Y-%m-%d")
        else:
            recent_date = "N/A"

        projects_data.append(
            {
                "name": project["name"],
                "session_count": session_count,
                "recent_date": recent_date,
            }
        )

    html_content = template.render(
        projects=projects_data,
        total_projects=len(projects),
        total_sessions=total_sessions,
        css=CSS,
        js=JS,
    )

    output_path = output_dir / "index.html"
    output_path.write_text(html_content, encoding="utf-8")


class CredentialsError(Exception):
    """Raised when credentials cannot be obtained."""

    pass


def get_access_token_from_keychain():
    """Get access token from macOS keychain.

    Returns the access token or None if not found.
    Raises CredentialsError with helpful message on failure.
    """
    if platform.system() != "Darwin":
        return None

    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                os.environ.get("USER", ""),
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

        # Parse the JSON to get the access token
        creds = json.loads(result.stdout.strip())
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, subprocess.SubprocessError):
        return None


def get_org_uuid_from_config():
    """Get organization UUID from ~/.claude.json.

    Returns the organization UUID or None if not found.
    """
    config_path = Path.home() / ".claude.json"
    if not config_path.exists():
        return None

    try:
        with open(config_path) as f:
            config = json.load(f)
        return config.get("oauthAccount", {}).get("organizationUuid")
    except (json.JSONDecodeError, IOError):
        return None


def get_api_headers(token, org_uuid):
    """Build API request headers."""
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
        "x-organization-uuid": org_uuid,
    }


def fetch_sessions(token, org_uuid):
    """Fetch list of sessions from the API.

    Returns the sessions data as a dict.
    Raises httpx.HTTPError on network/API errors.
    """
    headers = get_api_headers(token, org_uuid)
    response = httpx.get(f"{API_BASE_URL}/sessions", headers=headers, timeout=30.0)
    response.raise_for_status()
    return response.json()


def fetch_session(token, org_uuid, session_id):
    """Fetch a specific session from the API.

    Returns the session data as a dict.
    Raises httpx.HTTPError on network/API errors.
    """
    headers = get_api_headers(token, org_uuid)
    response = httpx.get(
        f"{API_BASE_URL}/session_ingress/session/{session_id}",
        headers=headers,
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


def detect_github_repo(loglines):
    """
    Detect GitHub repo from git push output in tool results.

    Looks for patterns like:
    - github.com/owner/repo/pull/new/branch (from git push messages)

    Returns the first detected repo (owner/name) or None.
    """
    for entry in loglines:
        message = entry.get("message", {})
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    match = GITHUB_REPO_PATTERN.search(result_content)
                    if match:
                        return match.group(1)
    return None


def extract_repo_from_session(session):
    """Extract GitHub repo from session metadata.

    Looks in session_context.outcomes for git_info.repo,
    or parses from session_context.sources URL.

    Returns repo as "owner/name" or None.
    """
    context = session.get("session_context", {})

    # Try outcomes first (has clean repo format)
    outcomes = context.get("outcomes", [])
    for outcome in outcomes:
        if outcome.get("type") == "git_repository":
            git_info = outcome.get("git_info", {})
            repo = git_info.get("repo")
            if repo:
                return repo

    # Fall back to sources URL
    sources = context.get("sources", [])
    for source in sources:
        if source.get("type") == "git_repository":
            url = source.get("url", "")
            # Parse github.com/owner/repo from URL
            if "github.com/" in url:
                # Extract owner/repo from https://github.com/owner/repo
                match = re.search(r"github\.com/([^/]+/[^/]+?)(?:\.git)?$", url)
                if match:
                    return match.group(1)

    return None


def enrich_sessions_with_repos(sessions, token=None, org_uuid=None, fetch_fn=None):
    """Enrich sessions with repo information from session metadata.

    Args:
        sessions: List of session dicts from the API
        token: Unused (kept for backward compatibility)
        org_uuid: Unused (kept for backward compatibility)
        fetch_fn: Unused (kept for backward compatibility)

    Returns:
        List of session dicts with 'repo' key added
    """
    enriched = []
    for session in sessions:
        session_copy = dict(session)
        session_copy["repo"] = extract_repo_from_session(session)
        enriched.append(session_copy)
    return enriched


def filter_sessions_by_repo(sessions, repo):
    """Filter sessions by repo.

    Args:
        sessions: List of session dicts with 'repo' key
        repo: Repo to filter by (owner/name), or None to return all

    Returns:
        Filtered list of sessions
    """
    if repo is None:
        return sessions
    return [s for s in sessions if s.get("repo") == repo]


def format_json(obj):
    try:
        if isinstance(obj, str):
            obj = json.loads(obj)
        formatted = json.dumps(obj, indent=2, ensure_ascii=False)
        return f'<pre class="json">{html.escape(formatted)}</pre>'
    except (json.JSONDecodeError, TypeError):
        return f"<pre>{html.escape(str(obj))}</pre>"


def render_markdown_text(text):
    if not text:
        return ""
    return markdown.markdown(text, extensions=["fenced_code", "tables"])


def is_json_like(text):
    if not text or not isinstance(text, str):
        return False
    text = text.strip()
    return (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    )


def render_todo_write(tool_input, tool_id):
    todos = tool_input.get("todos", [])
    if not todos:
        return ""
    return _macros.todo_list(todos, tool_id)


def render_write_tool(tool_input, tool_id):
    """Render Write tool calls with file path header and content preview."""
    file_path = tool_input.get("file_path", "Unknown file")
    content = tool_input.get("content", "")
    return _macros.write_tool(file_path, content, tool_id)


def render_edit_tool(tool_input, tool_id):
    """Render Edit tool calls with diff-like old/new display."""
    file_path = tool_input.get("file_path", "Unknown file")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")
    replace_all = tool_input.get("replace_all", False)
    return _macros.edit_tool(file_path, old_string, new_string, replace_all, tool_id)


def render_bash_tool(tool_input, tool_id):
    """Render Bash tool calls with command as plain text."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    return _macros.bash_tool(command, description, tool_id)


def render_content_block(block):
    if not isinstance(block, dict):
        return f"<p>{html.escape(str(block))}</p>"
    block_type = block.get("type", "")
    if block_type == "image":
        source = block.get("source", {})
        media_type = source.get("media_type", "image/png")
        data = source.get("data", "")
        return _macros.image_block(media_type, data)
    elif block_type == "thinking":
        content_html = render_markdown_text(block.get("thinking", ""))
        return _macros.thinking(content_html)
    elif block_type == "text":
        content_html = render_markdown_text(block.get("text", ""))
        return _macros.assistant_text(content_html)
    elif block_type == "tool_use":
        tool_name = block.get("name", "Unknown tool")
        tool_input = block.get("input", {})
        tool_id = block.get("id", "")
        if tool_name == "TodoWrite":
            return render_todo_write(tool_input, tool_id)
        if tool_name == "Write":
            return render_write_tool(tool_input, tool_id)
        if tool_name == "Edit":
            return render_edit_tool(tool_input, tool_id)
        if tool_name == "Bash":
            return render_bash_tool(tool_input, tool_id)
        description = tool_input.get("description", "")
        display_input = {k: v for k, v in tool_input.items() if k != "description"}
        input_json = json.dumps(display_input, indent=2, ensure_ascii=False)
        return _macros.tool_use(tool_name, description, input_json, tool_id)
    elif block_type == "tool_result":
        content = block.get("content", "")
        is_error = block.get("is_error", False)
        has_images = False

        # Check for git commits and render with styled cards
        if isinstance(content, str):
            commits_found = list(COMMIT_PATTERN.finditer(content))
            if commits_found:
                # Build commit cards + remaining content
                parts = []
                last_end = 0
                for match in commits_found:
                    # Add any content before this commit
                    before = content[last_end : match.start()].strip()
                    if before:
                        parts.append(f"<pre>{html.escape(before)}</pre>")

                    commit_hash = match.group(1)
                    commit_msg = match.group(2)
                    parts.append(
                        _macros.commit_card(commit_hash, commit_msg, _github_repo)
                    )
                    last_end = match.end()

                # Add any remaining content after last commit
                after = content[last_end:].strip()
                if after:
                    parts.append(f"<pre>{html.escape(after)}</pre>")

                content_html = "".join(parts)
            else:
                content_html = f"<pre>{html.escape(content)}</pre>"
        elif isinstance(content, list):
            # Handle tool result content that contains multiple blocks (text, images, etc.)
            parts = []
            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type", "")
                    if item_type == "text":
                        text = item.get("text", "")
                        if text:
                            parts.append(f"<pre>{html.escape(text)}</pre>")
                    elif item_type == "image":
                        source = item.get("source", {})
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        if data:
                            parts.append(_macros.image_block(media_type, data))
                            has_images = True
                    else:
                        # Unknown type, render as JSON
                        parts.append(format_json(item))
                else:
                    # Non-dict item, escape as text
                    parts.append(f"<pre>{html.escape(str(item))}</pre>")
            content_html = "".join(parts) if parts else format_json(content)
        elif is_json_like(content):
            content_html = format_json(content)
        else:
            content_html = format_json(content)
        return _macros.tool_result(content_html, is_error, has_images)
    else:
        return format_json(block)


def render_user_message_content(message_data):
    content = message_data.get("content", "")
    if isinstance(content, str):
        if is_json_like(content):
            return _macros.user_content(format_json(content))
        return _macros.user_content(render_markdown_text(content))
    elif isinstance(content, list):
        return "".join(render_content_block(block) for block in content)
    return f"<p>{html.escape(str(content))}</p>"


def render_assistant_message(message_data):
    content = message_data.get("content", [])
    if not isinstance(content, list):
        return f"<p>{html.escape(str(content))}</p>"
    return "".join(render_content_block(block) for block in content)


def make_msg_id(timestamp):
    return f"msg-{timestamp.replace(':', '-').replace('.', '-')}"


def analyze_conversation(messages):
    """Analyze messages in a conversation to extract stats and long texts."""
    tool_counts = {}  # tool_name -> count
    long_texts = []
    commits = []  # list of (hash, message, timestamp)

    for log_type, message_json, timestamp in messages:
        if not message_json:
            continue
        try:
            message_data = json.loads(message_json)
        except json.JSONDecodeError:
            continue

        content = message_data.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")

            if block_type == "tool_use":
                tool_name = block.get("name", "Unknown")
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            elif block_type == "tool_result":
                # Check for git commit output
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    for match in COMMIT_PATTERN.finditer(result_content):
                        commits.append((match.group(1), match.group(2), timestamp))
            elif block_type == "text":
                text = block.get("text", "")
                if len(text) >= LONG_TEXT_THRESHOLD:
                    long_texts.append(text)

    return {
        "tool_counts": tool_counts,
        "long_texts": long_texts,
        "commits": commits,
    }


def format_tool_stats(tool_counts):
    """Format tool counts into a concise summary string."""
    if not tool_counts:
        return ""

    # Abbreviate common tool names
    abbrev = {
        "Bash": "bash",
        "Read": "read",
        "Write": "write",
        "Edit": "edit",
        "Glob": "glob",
        "Grep": "grep",
        "Task": "task",
        "TodoWrite": "todo",
        "WebFetch": "fetch",
        "WebSearch": "search",
    }

    parts = []
    for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        short_name = abbrev.get(name, name.lower())
        parts.append(f"{count} {short_name}")

    return " · ".join(parts)


def is_tool_result_message(message_data):
    """Check if a message contains only tool_result blocks."""
    content = message_data.get("content", [])
    if not isinstance(content, list):
        return False
    if not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def render_message(log_type, message_json, timestamp):
    if not message_json:
        return ""
    try:
        message_data = json.loads(message_json)
    except json.JSONDecodeError:
        return ""
    if log_type == "user":
        content_html = render_user_message_content(message_data)
        # Check if this is a tool result message
        if is_tool_result_message(message_data):
            role_class, role_label = "tool-reply", "Tool reply"
        else:
            role_class, role_label = "user", "User"
    elif log_type == "assistant":
        content_html = render_assistant_message(message_data)
        role_class, role_label = "assistant", "Assistant"
    else:
        return ""
    if not content_html.strip():
        return ""
    msg_id = make_msg_id(timestamp)
    return _macros.message(role_class, role_label, msg_id, timestamp, content_html)


CSS = """
/* iMessage-style dark theme. The markup is unchanged from the prior light
   theme — all the layout work is here. Each .message is a flex column whose
   children (header strip + content bubble) are reordered so the bubble sits
   on top and the timestamp/role caption sits underneath. User messages
   align to the right with a filled blue bubble; assistant and tool-reply
   align to the left in a dark grey bubble. Inner blocks (tool use, tool
   result, edits, code) use translucent accent colours that sit cleanly on
   the dark grey assistant bubble. */
:root {
    --bg-color: #000;
    --surface-1: #1c1c1e;
    --surface-2: #2c2c2e;
    --surface-3: #3a3a3c;
    --user-bg: #0b84ff;
    --user-text: #fff;
    --assistant-bg: #2c2c2e;
    --assistant-text: #f2f2f7;
    --tool-accent: #bf5af2;
    --tool-result-accent: #30d158;
    --tool-error-accent: #ff453a;
    --thinking-accent: #ff9f0a;
    --link-color: #64d2ff;
    --code-bg: #0a0a0c;
    --code-text: #c4f0a3;
    --text-color: #f2f2f7;
    --text-muted: #98989d;
    --border-subtle: rgba(255,255,255,0.08);
    --shadow: 0 1px 3px rgba(0,0,0,0.5);
    --user-border: var(--user-bg);
    --assistant-border: var(--surface-3);
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Roboto, sans-serif; background: var(--bg-color); color: var(--text-color); margin: 0; padding: 16px; line-height: 1.55; }
.container { max-width: 900px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin-bottom: 24px; padding-bottom: 8px; border-bottom: 1px solid var(--border-subtle); color: var(--text-color); }
.header-row { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 8px; margin-bottom: 24px; }
.header-row h1 { border-bottom: none; padding-bottom: 0; margin-bottom: 0; flex: 1; min-width: 200px; }
a { color: var(--link-color); }

/* Message row: flex column so header (caption) and content (bubble) can
   be re-ordered. align-items steers the whole row left vs right. */
.message { display: flex; flex-direction: column; margin-bottom: 4px; }
.message + .message { margin-top: 14px; }
.message.user { align-items: flex-end; }
.message.assistant, .message.tool-reply { align-items: flex-start; }

/* Caption strip below the bubble (time + role label). Kept tiny so it
   reads as metadata, not content. */
.message-header { order: 2; display: flex; gap: 10px; align-items: center; padding: 4px 8px 0; background: transparent; font-size: 0.72rem; color: var(--text-muted); }
.role-label { font-weight: 500; text-transform: lowercase; letter-spacing: 0; color: var(--text-muted); }
time { color: var(--text-muted); font-size: 0.72rem; }
.timestamp-link { color: inherit; text-decoration: none; }
.timestamp-link:hover { text-decoration: underline; }
.message:target .message-content { animation: highlight 1.5s ease-out; }
@keyframes highlight { 0% { box-shadow: 0 0 0 4px rgba(11,132,255,0.45); } 100% { box-shadow: 0 0 0 0 rgba(11,132,255,0); } }

/* The bubble itself. Asymmetric border-radius gives the chat-tail look:
   tail-side corner is small, the other three are big. */
.message-content { order: 1; padding: 10px 14px; border-radius: 18px; max-width: 90%; word-wrap: break-word; overflow-wrap: anywhere; }
.message.user .message-content { background: var(--user-bg); color: var(--user-text); border-bottom-right-radius: 6px; max-width: 75%; }
.message.assistant .message-content { background: var(--assistant-bg); color: var(--assistant-text); border-bottom-left-radius: 6px; }
.message.tool-reply .message-content { background: var(--surface-1); color: var(--text-color); border: 1px solid rgba(255,159,10,0.20); border-bottom-left-radius: 6px; }
.tool-reply .role-label { color: var(--thinking-accent); }
.tool-reply .tool-result { background: transparent; padding: 0; margin: 0; border: 0; }
.message-content p { margin: 0 0 8px 0; }
.message-content p:last-child { margin-bottom: 0; }
.message.user .message-content a { color: #fff; text-decoration: underline; }

.thinking { background: rgba(255,159,10,0.10); border: 1px solid rgba(255,159,10,0.25); border-radius: 12px; padding: 10px 12px; margin: 10px 0; font-size: 0.9rem; color: var(--text-muted); }
.thinking-label { font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--thinking-accent); margin-bottom: 6px; }
.thinking p { margin: 6px 0; }
.assistant-text { margin: 6px 0; }

.tool-use { background: rgba(191,90,242,0.10); border: 1px solid rgba(191,90,242,0.25); border-radius: 12px; padding: 10px 12px; margin: 10px 0; }
.tool-header { font-weight: 600; color: var(--tool-accent); margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
.tool-icon { font-size: 1.05rem; }
.tool-description { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 6px; font-style: italic; }
.tool-result { background: rgba(48,209,88,0.08); border: 1px solid rgba(48,209,88,0.20); border-radius: 12px; padding: 10px 12px; margin: 10px 0; }
.tool-result.tool-error { background: rgba(255,69,58,0.10); border-color: rgba(255,69,58,0.25); }

.file-tool { border-radius: 12px; padding: 10px 12px; margin: 10px 0; }
.write-tool { background: rgba(48,209,88,0.10); border: 1px solid rgba(48,209,88,0.25); }
.edit-tool { background: rgba(255,159,10,0.08); border: 1px solid rgba(255,159,10,0.25); }
.file-tool-header { font-weight: 600; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; font-size: 0.95rem; }
.write-header { color: var(--tool-result-accent); }
.edit-header { color: var(--thinking-accent); }
.file-tool-icon { font-size: 1rem; }
.file-tool-path { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: rgba(255,255,255,0.10); padding: 2px 8px; border-radius: 4px; }
.file-tool-fullpath { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.78rem; color: var(--text-muted); margin-bottom: 8px; word-break: break-all; }
.file-content { margin: 0; }
.edit-section { display: flex; margin: 4px 0; border-radius: 6px; overflow: hidden; }
.edit-label { padding: 8px 12px; font-weight: bold; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; display: flex; align-items: flex-start; }
.edit-old { background: rgba(255,69,58,0.10); }
.edit-old .edit-label { color: #ff8a80; background: rgba(255,69,58,0.22); }
.edit-old .edit-content { color: #ff9c8f; }
.edit-new { background: rgba(48,209,88,0.10); }
.edit-new .edit-label { color: #7be0a3; background: rgba(48,209,88,0.22); }
.edit-new .edit-content { color: #b2f0c8; }
.edit-content { margin: 0; flex: 1; background: transparent; font-size: 0.85rem; }
.edit-replace-all { font-size: 0.75rem; font-weight: normal; color: var(--text-muted); }

.todo-list { background: rgba(48,209,88,0.08); border: 1px solid rgba(48,209,88,0.25); border-radius: 12px; padding: 10px 12px; margin: 10px 0; }
.todo-header { font-weight: 600; color: var(--tool-result-accent); margin-bottom: 8px; display: flex; align-items: center; gap: 8px; font-size: 0.95rem; }
.todo-items { list-style: none; margin: 0; padding: 0; }
.todo-item { display: flex; align-items: flex-start; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--border-subtle); font-size: 0.9rem; }
.todo-item:last-child { border-bottom: none; }
.todo-icon { flex-shrink: 0; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; font-weight: bold; border-radius: 50%; }
.todo-completed .todo-icon { color: var(--tool-result-accent); background: rgba(48,209,88,0.18); }
.todo-completed .todo-content { color: var(--text-muted); text-decoration: line-through; }
.todo-in-progress .todo-icon { color: var(--thinking-accent); background: rgba(255,159,10,0.18); }
.todo-in-progress .todo-content { color: var(--thinking-accent); font-weight: 500; }
.todo-pending .todo-icon { color: var(--text-muted); background: rgba(255,255,255,0.06); }
.todo-pending .todo-content { color: var(--text-color); }

pre { background: var(--code-bg); color: var(--code-text); padding: 10px 12px; border-radius: 8px; overflow-x: auto; font-size: 0.82rem; line-height: 1.5; margin: 8px 0; white-space: pre-wrap; word-wrap: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre.json { color: #e0e0e0; }
code { background: rgba(255,255,255,0.10); padding: 1px 6px; border-radius: 4px; font-size: 0.88em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre code { background: none; padding: 0; }
/* Code inside the user (blue) bubble: bump contrast so it stays legible. */
.message.user code { background: rgba(255,255,255,0.20); color: #fff; }
.message.user pre { background: rgba(0,0,0,0.35); color: #e7f4ff; }

.user-content { margin: 0; }
.truncatable { position: relative; }
.truncatable.truncated .truncatable-content { max-height: 200px; overflow: hidden; }
.truncatable.truncated::after { content: ''; position: absolute; bottom: 32px; left: 0; right: 0; height: 60px; background: linear-gradient(to bottom, transparent, var(--assistant-bg)); pointer-events: none; }
.message.user .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--user-bg)); }
.message.tool-reply .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--surface-1)); }
.tool-use .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, rgba(28,28,30,0.95)); }
.tool-result .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, rgba(28,28,30,0.95)); }
.expand-btn { display: none; width: 100%; padding: 8px 12px; margin-top: 4px; background: rgba(255,255,255,0.06); border: 1px solid var(--border-subtle); border-radius: 8px; cursor: pointer; font-size: 0.8rem; color: var(--text-muted); }
.expand-btn:hover { background: rgba(255,255,255,0.12); color: var(--text-color); }
.message.user .expand-btn { background: rgba(255,255,255,0.18); border-color: rgba(255,255,255,0.25); color: rgba(255,255,255,0.92); }
.message.user .expand-btn:hover { background: rgba(255,255,255,0.28); color: #fff; }
.truncatable.truncated .expand-btn, .truncatable.expanded .expand-btn { display: block; }

.pagination { display: flex; justify-content: center; gap: 8px; margin: 24px 0; flex-wrap: wrap; }
.pagination a, .pagination span { padding: 5px 10px; border-radius: 8px; text-decoration: none; font-size: 0.85rem; }
.pagination a { background: var(--surface-1); color: var(--user-bg); border: 1px solid rgba(11,132,255,0.30); }
.pagination a:hover { background: rgba(11,132,255,0.12); }
.pagination .current { background: var(--user-bg); color: white; }
.pagination .disabled { color: var(--text-muted); border: 1px solid var(--border-subtle); }
.pagination .index-link { background: var(--user-bg); color: white; }

details.continuation { margin-bottom: 16px; }
details.continuation summary { cursor: pointer; padding: 10px 14px; background: var(--surface-2); border-radius: 14px; font-weight: 500; color: var(--text-muted); list-style: none; }
details.continuation summary:hover { background: var(--surface-3); }
details.continuation[open] summary { border-radius: 14px 14px 0 0; margin-bottom: 0; }

/* The index page keeps card layout — bubbles only make sense for the
   transcript pages where there's a back-and-forth. The cards just adopt
   the dark palette. */
.index-item { margin-bottom: 14px; border-radius: 14px; overflow: hidden; box-shadow: var(--shadow); background: var(--surface-1); border: 1px solid var(--border-subtle); }
.index-item a { display: block; text-decoration: none; color: inherit; }
.index-item a:hover { background: rgba(11,132,255,0.08); }
.index-item-header { display: flex; justify-content: space-between; align-items: center; padding: 8px 14px; background: rgba(255,255,255,0.03); font-size: 0.85rem; border-bottom: 1px solid var(--border-subtle); }
.index-item-number { font-weight: 600; color: var(--user-bg); }
.index-item-content { padding: 14px; }
.index-item-stats { padding: 8px 14px 12px; font-size: 0.85rem; color: var(--text-muted); border-top: 1px solid var(--border-subtle); }
.index-item-commit { margin-top: 6px; padding: 4px 8px; background: rgba(255,159,10,0.10); border-radius: 4px; font-size: 0.85rem; color: var(--thinking-accent); }
.index-item-commit code { background: rgba(0,0,0,0.30); padding: 1px 4px; border-radius: 3px; font-size: 0.8rem; margin-right: 6px; }
.commit-card { margin: 8px 0; padding: 10px 14px; background: rgba(255,159,10,0.08); border-left: 3px solid var(--thinking-accent); border-radius: 6px; }
.commit-card a { text-decoration: none; color: var(--text-color); display: block; }
.commit-card a:hover { color: var(--thinking-accent); }
.commit-card-hash { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--thinking-accent); font-weight: 600; margin-right: 8px; }
.index-commit { margin-bottom: 12px; padding: 10px 14px; background: rgba(255,159,10,0.08); border-left: 3px solid var(--thinking-accent); border-radius: 8px; box-shadow: var(--shadow); }
.index-commit a { display: block; text-decoration: none; color: inherit; }
.index-commit a:hover { background: rgba(255,159,10,0.12); margin: -10px -14px; padding: 10px 14px; border-radius: 8px; }
.index-commit-header { display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; margin-bottom: 4px; }
.index-commit-hash { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--thinking-accent); font-weight: 600; }
.index-commit-msg { color: var(--text-color); }
.index-item-long-text { margin-top: 8px; padding: 10px 12px; background: var(--surface-2); border-radius: 8px; border-left: 2px solid var(--border-subtle); }
.index-item-long-text .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--surface-2)); }
.index-item-long-text-content { color: var(--text-color); }

#search-box { display: none; align-items: center; gap: 8px; }
#search-box input { padding: 6px 10px; border: 1px solid var(--border-subtle); border-radius: 8px; font-size: 16px; width: 180px; background: var(--surface-1); color: var(--text-color); }
#search-box button, #modal-search-btn, #modal-close-btn { background: var(--user-bg); color: white; border: none; border-radius: 8px; padding: 6px 10px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
#search-box button:hover, #modal-search-btn:hover { background: #2a95ff; }
#modal-close-btn { background: var(--surface-3); margin-left: 8px; }
#modal-close-btn:hover { background: var(--text-muted); color: #000; }
#search-modal[open] { border: 1px solid var(--border-subtle); border-radius: 14px; box-shadow: 0 8px 32px rgba(0,0,0,0.6); padding: 0; width: 90vw; max-width: 900px; height: 80vh; max-height: 80vh; display: flex; flex-direction: column; background: var(--surface-1); color: var(--text-color); }
#search-modal::backdrop { background: rgba(0,0,0,0.7); }
.search-modal-header { display: flex; align-items: center; gap: 8px; padding: 14px; border-bottom: 1px solid var(--border-subtle); background: var(--surface-1); border-radius: 14px 14px 0 0; }
.search-modal-header input { flex: 1; padding: 8px 12px; border: 1px solid var(--border-subtle); border-radius: 8px; font-size: 16px; background: var(--bg-color); color: var(--text-color); }
#search-status { padding: 8px 14px; font-size: 0.85rem; color: var(--text-muted); border-bottom: 1px solid var(--border-subtle); }
#search-results { flex: 1; overflow-y: auto; padding: 14px; }
.search-result { margin-bottom: 12px; border-radius: 10px; overflow: hidden; box-shadow: var(--shadow); background: var(--surface-2); }
.search-result a { display: block; text-decoration: none; color: inherit; }
.search-result a:hover { background: rgba(11,132,255,0.10); }
.search-result-page { padding: 6px 12px; background: rgba(255,255,255,0.04); font-size: 0.8rem; color: var(--text-muted); border-bottom: 1px solid var(--border-subtle); }
.search-result-content { padding: 12px; }
.search-result mark { background: rgba(255,214,10,0.30); color: #ffd60a; padding: 1px 2px; border-radius: 2px; }

@media (max-width: 600px) {
    body { padding: 8px; }
    .message.user .message-content { max-width: 85%; }
    .message.assistant .message-content { max-width: 95%; }
    .index-item { border-radius: 10px; }
    .message-content, .index-item-content { padding: 10px 12px; }
    pre { font-size: 0.78rem; padding: 8px; }
    #search-box input { width: 120px; }
    #search-modal[open] { width: 95vw; height: 90vh; }
}
"""

JS = """
document.querySelectorAll('time[data-timestamp]').forEach(function(el) {
    const timestamp = el.getAttribute('data-timestamp');
    const date = new Date(timestamp);
    const now = new Date();
    const isToday = date.toDateString() === now.toDateString();
    const timeStr = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    if (isToday) { el.textContent = timeStr; }
    else { el.textContent = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + timeStr; }
});
document.querySelectorAll('pre.json').forEach(function(el) {
    let text = el.textContent;
    text = text.replace(/"([^"]+)":/g, '<span style="color: #ce93d8">"$1"</span>:');
    text = text.replace(/: "([^"]*)"/g, ': <span style="color: #81d4fa">"$1"</span>');
    text = text.replace(/: (\\d+)/g, ': <span style="color: #ffcc80">$1</span>');
    text = text.replace(/: (true|false|null)/g, ': <span style="color: #f48fb1">$1</span>');
    el.innerHTML = text;
});
document.querySelectorAll('.truncatable').forEach(function(wrapper) {
    const content = wrapper.querySelector('.truncatable-content');
    const btn = wrapper.querySelector('.expand-btn');
    if (content.scrollHeight > 250) {
        wrapper.classList.add('truncated');
        btn.addEventListener('click', function() {
            if (wrapper.classList.contains('truncated')) { wrapper.classList.remove('truncated'); wrapper.classList.add('expanded'); btn.textContent = 'Show less'; }
            else { wrapper.classList.remove('expanded'); wrapper.classList.add('truncated'); btn.textContent = 'Show more'; }
        });
    }
});
"""

# JavaScript to fix relative URLs when served via gisthost.github.io or gistpreview.github.io
# Fixes issue #26: Pagination links broken on gisthost.github.io
GIST_PREVIEW_JS = r"""
(function() {
    var hostname = window.location.hostname;
    if (hostname !== 'gisthost.github.io' && hostname !== 'gistpreview.github.io') return;
    // URL format: https://gisthost.github.io/?GIST_ID/filename.html
    var match = window.location.search.match(/^\?([^/]+)/);
    if (!match) return;
    var gistId = match[1];

    function rewriteLinks(root) {
        (root || document).querySelectorAll('a[href]').forEach(function(link) {
            var href = link.getAttribute('href');
            // Skip already-rewritten links (issue #26 fix)
            if (href.startsWith('?')) return;
            // Skip external links and anchors
            if (href.startsWith('http') || href.startsWith('#') || href.startsWith('//')) return;
            // Handle anchor in relative URL (e.g., page-001.html#msg-123)
            var parts = href.split('#');
            var filename = parts[0];
            var anchor = parts.length > 1 ? '#' + parts[1] : '';
            link.setAttribute('href', '?' + gistId + '/' + filename + anchor);
        });
    }

    // Run immediately
    rewriteLinks();

    // Also run on DOMContentLoaded in case DOM isn't ready yet
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() { rewriteLinks(); });
    }

    // Use MutationObserver to catch dynamically added content
    // gistpreview.github.io may add content after initial load
    var observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            mutation.addedNodes.forEach(function(node) {
                if (node.nodeType === 1) { // Element node
                    rewriteLinks(node);
                    // Also check if the node itself is a link
                    if (node.tagName === 'A' && node.getAttribute('href')) {
                        var href = node.getAttribute('href');
                        if (!href.startsWith('?') && !href.startsWith('http') &&
                            !href.startsWith('#') && !href.startsWith('//')) {
                            var parts = href.split('#');
                            var filename = parts[0];
                            var anchor = parts.length > 1 ? '#' + parts[1] : '';
                            node.setAttribute('href', '?' + gistId + '/' + filename + anchor);
                        }
                    }
                }
            });
        });
    });

    // Start observing once body exists
    function startObserving() {
        if (document.body) {
            observer.observe(document.body, { childList: true, subtree: true });
        } else {
            setTimeout(startObserving, 10);
        }
    }
    startObserving();

    // Handle fragment navigation after dynamic content loads
    // gisthost.github.io/gistpreview.github.io loads content dynamically, so the browser's
    // native fragment navigation fails because the element doesn't exist yet
    function scrollToFragment() {
        var hash = window.location.hash;
        if (!hash) return false;
        var targetId = hash.substring(1);
        var target = document.getElementById(targetId);
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return true;
        }
        return false;
    }

    // Try immediately in case content is already loaded
    if (!scrollToFragment()) {
        // Retry with increasing delays to handle dynamic content loading
        var delays = [100, 300, 500, 1000, 2000];
        delays.forEach(function(delay) {
            setTimeout(scrollToFragment, delay);
        });
    }
})();
"""


def inject_gist_preview_js(output_dir):
    """Inject gist preview JavaScript into all HTML files in the output directory."""
    output_dir = Path(output_dir)
    for html_file in output_dir.glob("*.html"):
        content = html_file.read_text(encoding="utf-8")
        # Insert the gist preview JS before the closing </body> tag
        if "</body>" in content:
            content = content.replace(
                "</body>", f"<script>{GIST_PREVIEW_JS}</script>\n</body>"
            )
            html_file.write_text(content, encoding="utf-8")


def create_gist(output_dir, public=False):
    """Create a GitHub gist from the HTML files in output_dir.

    Returns the gist ID on success, or raises click.ClickException on failure.
    """
    output_dir = Path(output_dir)
    html_files = list(output_dir.glob("*.html"))
    if not html_files:
        raise click.ClickException("No HTML files found to upload to gist.")

    # Build the gh gist create command
    # gh gist create file1 file2 ... --public/--private
    cmd = ["gh", "gist", "create"]
    cmd.extend(str(f) for f in sorted(html_files))
    if public:
        cmd.append("--public")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        # Output is the gist URL, e.g., https://gist.github.com/username/GIST_ID
        gist_url = result.stdout.strip()
        # Extract gist ID from URL
        gist_id = gist_url.rstrip("/").split("/")[-1]
        return gist_id, gist_url
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else str(e)
        raise click.ClickException(f"Failed to create gist: {error_msg}")
    except FileNotFoundError:
        raise click.ClickException(
            "gh CLI not found. Install it from https://cli.github.com/ and run 'gh auth login'."
        )


def generate_pagination_html(current_page, total_pages):
    return _macros.pagination(current_page, total_pages)


def generate_index_pagination_html(total_pages):
    """Generate pagination for index page where Index is current (first page)."""
    return _macros.index_pagination(total_pages)


def generate_html(json_path, output_dir, github_repo=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    # Load session file (supports both JSON and JSONL)
    data = parse_session_file(json_path)

    loglines = data.get("loglines", [])

    # Auto-detect GitHub repo if not provided
    if github_repo is None:
        github_repo = detect_github_repo(loglines)
        if github_repo:
            print(f"Auto-detected GitHub repo: {github_repo}")
        else:
            print(
                "Warning: Could not auto-detect GitHub repo. Commit links will be disabled."
            )

    # Set module-level variable for render functions
    global _github_repo
    _github_repo = github_repo

    conversations = []
    current_conv = None
    for entry in loglines:
        log_type = entry.get("type")
        timestamp = entry.get("timestamp", "")
        is_compact_summary = entry.get("isCompactSummary", False)
        message_data = entry.get("message", {})
        if not message_data:
            continue
        # Convert message dict to JSON string for compatibility with existing render functions
        message_json = json.dumps(message_data)
        is_user_prompt = False
        user_text = None
        if log_type == "user":
            content = message_data.get("content", "")
            text = extract_text_from_content(content)
            if text:
                is_user_prompt = True
                user_text = text
        if is_user_prompt:
            if current_conv:
                conversations.append(current_conv)
            current_conv = {
                "user_text": user_text,
                "timestamp": timestamp,
                "messages": [(log_type, message_json, timestamp)],
                "is_continuation": bool(is_compact_summary),
            }
        elif current_conv:
            current_conv["messages"].append((log_type, message_json, timestamp))
    if current_conv:
        conversations.append(current_conv)

    total_convs = len(conversations)
    total_pages = (total_convs + PROMPTS_PER_PAGE - 1) // PROMPTS_PER_PAGE

    for page_num in range(1, total_pages + 1):
        start_idx = (page_num - 1) * PROMPTS_PER_PAGE
        end_idx = min(start_idx + PROMPTS_PER_PAGE, total_convs)
        page_convs = conversations[start_idx:end_idx]
        messages_html = []
        for conv in page_convs:
            is_first = True
            for log_type, message_json, timestamp in conv["messages"]:
                msg_html = render_message(log_type, message_json, timestamp)
                if msg_html:
                    # Wrap continuation summaries in collapsed details
                    if is_first and conv.get("is_continuation"):
                        msg_html = f'<details class="continuation"><summary>Session continuation summary</summary>{msg_html}</details>'
                    messages_html.append(msg_html)
                is_first = False
        pagination_html = generate_pagination_html(page_num, total_pages)
        page_template = get_template("page.html")
        page_content = page_template.render(
            css=CSS,
            js=JS,
            page_num=page_num,
            total_pages=total_pages,
            pagination_html=pagination_html,
            messages_html="".join(messages_html),
        )
        (output_dir / f"page-{page_num:03d}.html").write_text(
            page_content, encoding="utf-8"
        )
        print(f"Generated page-{page_num:03d}.html")

    # Calculate overall stats and collect all commits for timeline
    total_tool_counts = {}
    total_messages = 0
    all_commits = []  # (timestamp, hash, message, page_num, conv_index)
    for i, conv in enumerate(conversations):
        total_messages += len(conv["messages"])
        stats = analyze_conversation(conv["messages"])
        for tool, count in stats["tool_counts"].items():
            total_tool_counts[tool] = total_tool_counts.get(tool, 0) + count
        page_num = (i // PROMPTS_PER_PAGE) + 1
        for commit_hash, commit_msg, commit_ts in stats["commits"]:
            all_commits.append((commit_ts, commit_hash, commit_msg, page_num, i))
    total_tool_calls = sum(total_tool_counts.values())
    total_commits = len(all_commits)

    # Build timeline items: prompts and commits merged by timestamp
    timeline_items = []

    # Add prompts
    prompt_num = 0
    for i, conv in enumerate(conversations):
        if conv.get("is_continuation"):
            continue
        if conv["user_text"].startswith("Stop hook feedback:"):
            continue
        prompt_num += 1
        page_num = (i // PROMPTS_PER_PAGE) + 1
        msg_id = make_msg_id(conv["timestamp"])
        link = f"page-{page_num:03d}.html#{msg_id}"
        rendered_content = render_markdown_text(conv["user_text"])

        # Collect all messages including from subsequent continuation conversations
        # This ensures long_texts from continuations appear with the original prompt
        all_messages = list(conv["messages"])
        for j in range(i + 1, len(conversations)):
            if not conversations[j].get("is_continuation"):
                break
            all_messages.extend(conversations[j]["messages"])

        # Analyze conversation for stats (excluding commits from inline display now)
        stats = analyze_conversation(all_messages)
        tool_stats_str = format_tool_stats(stats["tool_counts"])

        long_texts_html = ""
        for lt in stats["long_texts"]:
            rendered_lt = render_markdown_text(lt)
            long_texts_html += _macros.index_long_text(rendered_lt)

        stats_html = _macros.index_stats(tool_stats_str, long_texts_html)

        item_html = _macros.index_item(
            prompt_num, link, conv["timestamp"], rendered_content, stats_html
        )
        timeline_items.append((conv["timestamp"], "prompt", item_html))

    # Add commits as separate timeline items
    for commit_ts, commit_hash, commit_msg, page_num, conv_idx in all_commits:
        item_html = _macros.index_commit(
            commit_hash, commit_msg, commit_ts, _github_repo
        )
        timeline_items.append((commit_ts, "commit", item_html))

    # Sort by timestamp
    timeline_items.sort(key=lambda x: x[0])
    index_items = [item[2] for item in timeline_items]

    index_pagination = generate_index_pagination_html(total_pages)
    index_template = get_template("index.html")
    index_content = index_template.render(
        css=CSS,
        js=JS,
        pagination_html=index_pagination,
        prompt_num=prompt_num,
        total_messages=total_messages,
        total_tool_calls=total_tool_calls,
        total_commits=total_commits,
        total_pages=total_pages,
        index_items_html="".join(index_items),
    )
    index_path = output_dir / "index.html"
    index_path.write_text(index_content, encoding="utf-8")
    print(
        f"Generated {index_path.resolve()} ({total_convs} prompts, {total_pages} pages)"
    )


@click.group(cls=DefaultGroup, default="local", default_if_no_args=True)
@click.version_option(None, "-v", "--version", package_name="cad")
def cli():
    """cad — Coding Agent Driver. Manage sessions across claude, codex,
    pi, opencode, and forge from one picker, or render Claude Code
    sessions to HTML."""
    pass


@cli.command("shell-init")
@click.argument("shell", type=click.Choice(sorted(SHELL_WRAPPERS.keys())))
def shell_init_cmd(shell):
    """Print a shell wrapper function for `cad`.

    Install once by adding this line to your rc file::

        eval "$(cad shell-init zsh)"   # or bash

    The wrapper makes Enter (resume) leave your shell inside the project
    directory after the agent exits. Without it, you stay in whichever
    directory you ran `cad` from — a Unix child process can't cd its
    parent shell.
    """
    click.echo(SHELL_WRAPPERS[shell], nl=False)


# Text labels shown next to the coloured dot on each live-picker row.
# Padded to a fixed width so columns stay aligned regardless of state.
_STATE_TEXT_LABELS = {
    "working": "[working]",
    "input": "[input]  ",
    "idle": "[idle]   ",
}


def _resolve_pid_tty(pid):
    """Return the /dev tty of ``pid`` (e.g. ``/dev/ttys004``) or None
    if ps can't see it. Used by ``focus_live_session`` to map a live
    claude process onto a terminal tab.

    A child process shares its parent shell's pty, so claude's tty is
    the same one the terminal emulator reports for that tab —
    matching by tty is what lets us go pid → iTerm2 session.
    """
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "tty="],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    raw = (r.stdout or "").strip()
    if not raw or raw == "?":
        return None
    # ps prints the basename (ttys004), iTerm2's AppleScript reports
    # /dev/ttys004. Normalise to the absolute form.
    return raw if raw.startswith("/") else f"/dev/{raw}"


# AppleScript that walks every iTerm2 window/tab/session looking for a
# session whose tty matches ours, then selects window→tab→session so
# the tab comes to the front. Returns "ok" on a match and "no-match"
# otherwise so we can surface a useful boolean to the caller.
_ITERM2_FOCUS_BY_TTY_OSASCRIPT = """
on run argv
    set targetTTY to item 1 of argv
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if tty of s is targetTTY then
                        tell w to select
                        tell t to select
                        tell s to select
                        activate
                        return "ok"
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return "no-match"
end run
"""


def focus_live_session(session):
    """Bring the terminal tab running this live session to the front.
    Returns True on success, False if we can't (unsupported terminal,
    no PID, ps failure, no matching tab). Callers fall back to peek
    when we return False so Enter is never a no-op.

    Only iTerm2 is wired up so far. Agamon could plug in here once it
    exposes a focus-by-tty IPC. Terminal.app, Alacritty, and plain
    tmux-without-host-integration aren't supportable from a child
    process without proprietary escape codes.
    """
    pid = session.get("pid")
    if not pid:
        return False
    term_program = os.environ.get("TERM_PROGRAM", "")
    if term_program != "iTerm.app":
        return False
    tty = _resolve_pid_tty(pid)
    if not tty:
        return False
    try:
        r = subprocess.run(
            ["osascript", "-e", _ITERM2_FOCUS_BY_TTY_OSASCRIPT, tty],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if r.returncode != 0:
        return False
    return (r.stdout or "").strip() == "ok"


def _build_live_entries():
    """Snapshot of every live agent session across all projects, ready
    to feed into ``select_entry``. Each project group is led by a
    non-selectable header row (``header: True``) carrying the project
    name; its sessions follow, indented, in state-priority order
    (working → input → idle) so the row most likely needing attention
    sits at the top of the group. Outer order matches the project
    picker (most-recent-activity first).

    The header row is what gives ``cad live`` its visual hierarchy —
    without it, sessions from many projects blur into one undifferentiated
    list. ``select_entry`` knows to skip rows tagged ``header: True``
    during cursor navigation.
    """
    projects = find_local_projects()
    entries = []
    state_order = {"working": 0, "input": 1, "idle": 2}
    for p in projects:
        live = [s for s in p["sessions"] if s.get("live")]
        if not live:
            continue
        live.sort(key=lambda s: state_order.get(s.get("state"), 3))
        # Blank spacer row between groups so the eye separates one
        # project from the next. The first group gets no leading
        # spacer — it's already visually at the top.
        if entries:
            entries.append({"header": True, "display": ""})
        entries.append({"header": True, "display": p["name"]})
        for s in live:
            load_session_summary(s)
            label = _STATE_TEXT_LABELS.get(s.get("state"), "[?]      ")
            # session["display"] is "<date>  <size>  provider/<rest>".
            # Drop the date+size column — the state label already
            # telegraphs recency, and screen real estate is precious.
            display = s["display"]
            provider_marker = f"{s['provider']}/"
            idx = display.find(provider_marker)
            tail = display[idx:] if idx >= 0 else display
            entries.append(
                {
                    # Mirror the session dict shape so resume_session
                    # and the existing select_entry rendering both work
                    # without special-casing.
                    "provider": s["provider"],
                    "session_id": s["session_id"],
                    "cwd": s["cwd"],
                    "filepath": s["filepath"],
                    "mtime": s["mtime"],
                    "live": s["live"],
                    "state": s["state"],
                    # PID is None for unbound (cwd-matched) sessions;
                    # the focus action knows to fall back to peek when
                    # it's missing.
                    "pid": s.get("pid"),
                    # No extra indent here — ``select_entry`` already
                    # prefixes each row with a 2-char arrow column and
                    # a 2-char state-marker column, which naturally
                    # indents sessions 4 spaces below the flush-left
                    # project header.
                    "display": f"{label}  {tail}",
                }
            )
    return entries


@cli.command("live")
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
    with _loading_message("Loading live sessions..."):
        entries = _build_live_entries()
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
        refresh_callback=_build_live_entries,
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
    peek_session(session)


@cli.command("local")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Output directory. If not specified, writes to temp dir and opens in browser.",
)
@click.option(
    "-a",
    "--output-auto",
    is_flag=True,
    help="Auto-name output subdirectory based on session filename (uses -o as parent, or current dir).",
)
@click.option(
    "--repo",
    help="GitHub repo (owner/name) for commit links. Auto-detected from git push output if not specified.",
)
@click.option(
    "--gist",
    is_flag=True,
    help="Upload to GitHub Gist and output a gisthost.github.io URL.",
)
@click.option(
    "--json",
    "include_json",
    is_flag=True,
    help="Include the original JSONL session file in the output directory.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated index.html in your default browser (default if no -o specified).",
)
@click.option(
    "--all",
    "show_all_projects",
    is_flag=True,
    help="Skip the auto-pick: show the project picker even when launched inside a known project.",
)
def local_cmd(
    output, output_auto, repo, gist, include_json, open_browser, show_all_projects
):
    """Select a local agent session and either resume it or render to HTML.

    Sessions from claude (``~/.claude/projects/``) and codex
    (``~/.codex/sessions/``) are merged by their recorded ``cwd`` so a
    project entry shows the combined session count regardless of which
    agent CLI you used. Provider is shown via a small badge per row
    (``[c]`` / ``[x]``).

    Two-step picker. First choose a project (questionary, with
    type-to-filter). Then choose a session (custom picker):

    - Enter resumes — chdir to the recorded cwd and exec the right agent
      CLI (``claude --dangerously-skip-permissions --resume`` for claude,
      ``codex resume`` for codex).
    - h renders the session to HTML. Currently only claude sessions can
      be rendered; pressing h on a codex session prints a 'not yet
      supported' message and returns.

    Session summaries are loaded only after a project is picked, so the
    first picker stays cheap even with hundreds of sessions on disk.
    """
    with _loading_message("Loading projects..."):
        projects = find_local_projects()

    if not projects:
        click.echo("No local sessions found.")
        return

    # If launched from inside a known project, skip the project picker
    # and drop straight into that project's session list — the common
    # case. The user can press Esc/Bksp to back out to the full list, or
    # pass --all to bypass the auto-pick entirely.
    auto_pick = None
    if not show_all_projects:
        auto_pick = _find_project_for_cwd(projects, str(Path.cwd()))

    # Outer loop covers project → session → (back) → project navigation.
    # Esc/Bksp on the session picker returns the user here instead of
    # quitting; q on either picker still hard-quits.
    project_idx = 0
    while True:
        # Project picker uses the same custom picker as the session step
        # so the search UX is consistent (`/` opens search in both). No
        # back_action: Esc here means quit. `r` bulk-moves every session
        # in a project to a new cwd — for when you've renamed the folder
        # on disk (`mv ~/Code/foo ~/Code/bar`) and want every session to
        # point at the new location in one go.
        if auto_pick is not None:
            # First iteration only: cad was launched inside a known
            # project. Skip the picker and drop straight into its
            # sessions. Clear the auto-pick so back-navigation goes to
            # the full project picker as expected.
            selected_project = auto_pick
            project_action = "open"
            try:
                project_idx = projects.index(auto_pick)
            except ValueError:
                project_idx = 0
            auto_pick = None
            click.echo(
                f"Auto-opening project at {selected_project['cwd']} "
                f"(Esc to see all, --all to skip auto-pick)"
            )
        else:
            picked = select_entry(
                projects,
                actions={"enter": "open", "n": "new", "r": "rename"},
                initial_selected=project_idx,
            )
            if picked is None:
                click.echo("No project selected.")
                return
            selected_project, project_action = picked
            try:
                project_idx = projects.index(selected_project)
            except ValueError:
                project_idx = 0

        if project_action == "new":
            # Start a fresh claude session in this project's cwd. Doesn't
            # pick up the virtual Global Sessions entry — there's no
            # canonical cwd for it.
            cwd = selected_project["cwd"]
            if not cwd:
                click.echo(
                    "Can't start a new session in the virtual Global "
                    "Sessions entry — no canonical cwd.",
                    err=True,
                )
                continue
            # Replaces the current process — does not return on success.
            new_session(cwd)
            return

        if project_action == "rename":
            # Full project rename. cad handles every step so the user
            # never has to do a manual `mv` and then track it across
            # claude state dirs. Sequence: prompt → confirm → backup →
            # mv user folder → migrate claude state dirs → rewrite cwd
            # in JSONLs → clear sidecar overrides (no longer needed).
            project_sessions = selected_project["sessions"]
            n = len(project_sessions)
            old_cwd = selected_project["cwd"]

            if not old_cwd:
                click.echo(
                    "Rename not supported for the virtual Global Sessions entry.",
                    err=True,
                )
                continue

            new_cwd = prompt_for_cwd(
                default=old_cwd, must_exist=False, label="Rename to"
            )
            if new_cwd is None:
                continue  # Ctrl-C / EOF
            if not new_cwd:
                click.echo("Empty path — cancelled (no override changes made).")
                continue
            if new_cwd == old_cwd:
                click.echo("Same path — nothing to do.")
                continue

            # Spell out exactly what's about to happen so the user can
            # bail on the last yes/no rather than discovering surprises.
            providers_in_project = sorted({s["provider"] for s in project_sessions})
            non_claude = [p for p in providers_in_project if p != "claude"]
            click.echo()
            click.echo("About to rename project:")
            click.echo(f"  fs mv:    {old_cwd}  →  {new_cwd}")
            click.echo(f"  claude:   migrate state dirs, rewrite cwd in {n} JSONL(s)")
            if non_claude:
                click.echo(
                    f"  others:   {', '.join(non_claude)} sessions stay where they are"
                )
                click.echo(
                    "            (cad's sidecar override will point them at the new cwd)"
                )
            click.echo(f"  backup:   ~/.cad/agent-backups/claude-migrate-<ts>/")
            if not prompt_confirm("Proceed?"):
                click.echo("Cancelled.")
                continue

            try:
                # Phase 1: backup + migrate claude on-disk state. Do this
                # before the user-side mv so backups live in ~/.cad/ even
                # if the user-side mv fails.
                migration = migrate_claude_project(
                    old_cwd,
                    new_cwd,
                    backup_root=Path.home() / ".cad" / "agent-backups",
                )

                # Phase 2: mv the user's actual project directory.
                old_path = Path(old_cwd)
                new_path = Path(new_cwd)
                if old_path.exists():
                    shutil.move(str(old_path), str(new_path))
                elif not new_path.exists():
                    # User-side directory was already gone (e.g., they
                    # nuked it during testing). Warn but don't fail —
                    # the claude state migration may still be useful.
                    click.echo(
                        f"Note: {old_path} didn't exist; skipped fs mv.",
                        err=True,
                    )

                # Phase 3: for non-claude providers, fall back to the
                # sidecar override (their storage isn't path-encoded so
                # there's nothing to move on disk; cad just needs to
                # know the new cwd).
                for s in project_sessions:
                    if s["provider"] == "claude":
                        # Claude no longer needs the sidecar — its JSONLs
                        # now record the new cwd. Clear any stale
                        # override so source-of-truth is the JSONL.
                        save_cwd_override(s["provider"], s["session_id"], "")
                    else:
                        save_cwd_override(s["provider"], s["session_id"], new_cwd)

                click.echo()
                click.echo(f"Renamed {selected_project['name']} → {Path(new_cwd).name}")
                click.echo(f"  claude state dirs moved: {len(migration['moved_dirs'])}")
                click.echo(
                    f"  JSONL cwds rewritten:    {len(migration['rewritten_files'])}"
                )
                if migration["backup_dir"]:
                    click.echo(f"  backup:                  {migration['backup_dir']}")
                if migration["skipped"]:
                    click.echo("  skipped (collisions):")
                    for s in migration["skipped"]:
                        click.echo(f"    {s}")
            except (OSError, click.ClickException) as e:
                click.echo(f"Migration failed: {e}", err=True)
                click.echo(
                    "Backup (if any) is at ~/.cad/agent-backups/. "
                    "Inspect ~/.claude/projects/ and the new path before retrying.",
                    err=True,
                )
                continue

            # Re-discover so the picker reflects the new state, then jump
            # cursor to the new project.
            projects = find_local_projects()
            project_idx = next(
                (i for i, p in enumerate(projects) if p["cwd"] == new_cwd),
                0,
            )
            continue

        sessions = selected_project["sessions"]
        if not sessions:
            click.echo(f"No sessions in {selected_project['name']}.")
            # Bounce back to project picker — empty project is recoverable.
            continue

        # Hydrate summaries only now (after the project pick), so opening
        # the project picker stays cheap regardless of total session count.
        for s in sessions:
            load_session_summary(s)

        # Inner loop: r/s/m/p actions update a title/cwd / open the pager
        # and stay on the session picker; back returns to the outer loop.
        # selected_idx is preserved across iterations so re-rendered
        # pickers come back to the same row (Quick Look style).
        went_back = False
        selected_idx = 0
        while True:
            picked = select_entry(
                sessions,
                actions={
                    "enter": "resume",
                    "n": "new",
                    "h": "html",
                    "r": "rename",
                    "s": "summarize",
                    "m": "move",
                    "p": "peek",
                },
                back_action="back",
                initial_selected=selected_idx,
            )
            if picked is None:
                # q or Ctrl-C — hard quit.
                click.echo("No session selected.")
                return

            session, action = picked

            if action == "back":
                went_back = True
                break

            # Remember which row was active so the next re-entry of the
            # picker starts on the same session.
            try:
                selected_idx = sessions.index(session)
            except ValueError:
                selected_idx = 0

            if action == "peek":
                peek_session(session)
                continue

            if action == "new":
                # Start a fresh claude session in this project's cwd.
                # Replaces the process — does not return on success.
                new_session(selected_project["cwd"])
                return

            if action == "rename":
                new_title = prompt_for_title(default=session.get("summary") or "")
                if new_title is None:  # Ctrl-C / EOF
                    continue
                save_title_override(
                    session["provider"], session["session_id"], new_title
                )
                session["summary"] = new_title or None
                session["display"] = None
                session["_recently_updated"] = True
                load_session_summary(session)
                continue

            if action == "summarize":
                click.echo(f"Summarizing {session['session_id']}...")
                try:
                    title = summarize_session(session)
                except click.ClickException as e:
                    click.echo(f"Summarize failed: {e.message}", err=True)
                    continue
                save_title_override(session["provider"], session["session_id"], title)
                session["summary"] = title
                session["display"] = None
                session["_recently_updated"] = True
                load_session_summary(session)
                click.echo(f"Saved title: {title}")
                continue

            if action == "move":
                new_cwd = prompt_for_cwd(default=session.get("cwd") or "")
                if new_cwd is None:  # cancel
                    continue
                save_cwd_override(session["provider"], session["session_id"], new_cwd)
                session["cwd"] = new_cwd or session["cwd"]
                session["display"] = None
                session["_recently_updated"] = True
                load_session_summary(session)
                verb = "Moved" if new_cwd else "Cleared override for"
                click.echo(f"{verb} session to {new_cwd or session['cwd']}")
                continue

            if action == "resume":
                # Replaces the current process — does not return.
                resume_session(session)
                return

            # action == "html" — break out, fall through to render.
            break

        if went_back:
            # Re-enter outer loop = project picker.
            continue
        # Fell through with action == "html". Exit outer loop too.
        break

    # action == "html"
    if session["provider"] != "claude":
        click.echo(f"HTML render not supported for {session['provider']} sessions yet.")
        return

    session_file = session["filepath"]
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        parent_dir = Path(output) if output else Path(".")
        output = parent_dir / session_file.stem
    elif output is None:
        output = _temp_output_dir(f"claude-session-{session_file.stem}")

    output = Path(output)
    generate_html(session_file, output, github_repo=repo)

    # Show output directory
    click.echo(f"Output: {output.resolve()}")

    # Copy JSONL file to output directory if requested
    if include_json:
        output.mkdir(exist_ok=True)
        json_dest = output / session_file.name
        shutil.copy(session_file, json_dest)
        json_size_kb = json_dest.stat().st_size / 1024
        click.echo(f"JSONL: {json_dest} ({json_size_kb:.1f} KB)")

    if gist:
        # Inject gist preview JS and create gist
        inject_gist_preview_js(output)
        click.echo("Creating GitHub gist...")
        gist_id, gist_url = create_gist(output)
        preview_url = f"https://gisthost.github.io/?{gist_id}/index.html"
        click.echo(f"Gist: {gist_url}")
        click.echo(f"Preview: {preview_url}")

    if open_browser or auto_open:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)


def is_url(path):
    """Check if a path is a URL (starts with http:// or https://)."""
    return path.startswith("http://") or path.startswith("https://")


def fetch_url_to_tempfile(url):
    """Fetch a URL and save to a temporary file.

    Returns the Path to the temporary file.
    Raises click.ClickException on network errors.
    """
    try:
        response = httpx.get(url, timeout=60.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.RequestError as e:
        raise click.ClickException(f"Failed to fetch URL: {e}")
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"Failed to fetch URL: {e.response.status_code} {e.response.reason_phrase}"
        )

    # Determine file extension from URL
    url_path = url.split("?")[0]  # Remove query params
    if url_path.endswith(".jsonl"):
        suffix = ".jsonl"
    elif url_path.endswith(".json"):
        suffix = ".json"
    else:
        suffix = ".jsonl"  # Default to JSONL

    # Extract a name from the URL for the temp file
    url_name = Path(url_path).stem or "session"

    temp_dir = Path(tempfile.gettempdir())
    temp_file = temp_dir / f"claude-url-{url_name}{suffix}"
    temp_file.write_text(response.text, encoding="utf-8")
    return temp_file


@cli.command("json")
@click.argument("json_file", type=click.Path())
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Output directory. If not specified, writes to temp dir and opens in browser.",
)
@click.option(
    "-a",
    "--output-auto",
    is_flag=True,
    help="Auto-name output subdirectory based on filename (uses -o as parent, or current dir).",
)
@click.option(
    "--repo",
    help="GitHub repo (owner/name) for commit links. Auto-detected from git push output if not specified.",
)
@click.option(
    "--gist",
    is_flag=True,
    help="Upload to GitHub Gist and output a gisthost.github.io URL.",
)
@click.option(
    "--json",
    "include_json",
    is_flag=True,
    help="Include the original JSON session file in the output directory.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated index.html in your default browser (default if no -o specified).",
)
def json_cmd(json_file, output, output_auto, repo, gist, include_json, open_browser):
    """Convert a Claude Code session JSON/JSONL file or URL to HTML."""
    # Handle URL input
    if is_url(json_file):
        click.echo(f"Fetching {json_file}...")
        temp_file = fetch_url_to_tempfile(json_file)
        json_file_path = temp_file
        # Use URL path for naming
        url_name = Path(json_file.split("?")[0]).stem or "session"
    else:
        # Validate that local file exists
        json_file_path = Path(json_file)
        if not json_file_path.exists():
            raise click.ClickException(f"File not found: {json_file}")
        url_name = None

    # Determine output directory and whether to open browser
    # If no -o specified, use temp dir and open browser by default
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        # Use -o as parent dir (or current dir), with auto-named subdirectory
        parent_dir = Path(output) if output else Path(".")
        output = parent_dir / (url_name or json_file_path.stem)
    elif output is None:
        output = _temp_output_dir(f"claude-session-{url_name or json_file_path.stem}")

    output = Path(output)
    generate_html(json_file_path, output, github_repo=repo)

    # Show output directory
    click.echo(f"Output: {output.resolve()}")

    # Copy JSON file to output directory if requested
    if include_json:
        output.mkdir(exist_ok=True)
        json_dest = output / json_file_path.name
        shutil.copy(json_file_path, json_dest)
        json_size_kb = json_dest.stat().st_size / 1024
        click.echo(f"JSON: {json_dest} ({json_size_kb:.1f} KB)")

    if gist:
        # Inject gist preview JS and create gist
        inject_gist_preview_js(output)
        click.echo("Creating GitHub gist...")
        gist_id, gist_url = create_gist(output)
        preview_url = f"https://gisthost.github.io/?{gist_id}/index.html"
        click.echo(f"Gist: {gist_url}")
        click.echo(f"Preview: {preview_url}")

    if open_browser or auto_open:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)


def resolve_credentials(token, org_uuid):
    """Resolve token and org_uuid from arguments or auto-detect.

    Returns (token, org_uuid) tuple.
    Raises click.ClickException if credentials cannot be resolved.
    """
    # Get token
    if token is None:
        token = get_access_token_from_keychain()
        if token is None:
            if platform.system() == "Darwin":
                raise click.ClickException(
                    "Could not retrieve access token from macOS keychain. "
                    "Make sure you are logged into Claude Code, or provide --token."
                )
            else:
                raise click.ClickException(
                    "On non-macOS platforms, you must provide --token with your access token."
                )

    # Get org UUID
    if org_uuid is None:
        org_uuid = get_org_uuid_from_config()
        if org_uuid is None:
            raise click.ClickException(
                "Could not find organization UUID in ~/.claude.json. "
                "Provide --org-uuid with your organization UUID."
            )

    return token, org_uuid


def format_session_for_display(session_data):
    """Format a session for display in the list or picker.

    Shows repo first (if available), then date, then title.
    Returns a formatted string.
    """
    title = session_data.get("title", "Untitled")
    created_at = session_data.get("created_at", "")
    repo = session_data.get("repo")
    # Truncate title if too long
    if len(title) > 50:
        title = title[:47] + "..."
    # Format: repo (or placeholder)  date  title
    repo_display = repo if repo else "(no repo)"
    date_display = created_at[:19] if created_at else "N/A"
    return f"{repo_display:30}  {date_display:19}  {title}"


def generate_html_from_session_data(session_data, output_dir, github_repo=None):
    """Generate HTML from session data dict (instead of file path)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True, parents=True)

    loglines = session_data.get("loglines", [])

    # Auto-detect GitHub repo if not provided
    if github_repo is None:
        github_repo = detect_github_repo(loglines)
        if github_repo:
            click.echo(f"Auto-detected GitHub repo: {github_repo}")

    # Set module-level variable for render functions
    global _github_repo
    _github_repo = github_repo

    conversations = []
    current_conv = None
    for entry in loglines:
        log_type = entry.get("type")
        timestamp = entry.get("timestamp", "")
        is_compact_summary = entry.get("isCompactSummary", False)
        message_data = entry.get("message", {})
        if not message_data:
            continue
        # Convert message dict to JSON string for compatibility with existing render functions
        message_json = json.dumps(message_data)
        is_user_prompt = False
        user_text = None
        if log_type == "user":
            content = message_data.get("content", "")
            text = extract_text_from_content(content)
            if text:
                is_user_prompt = True
                user_text = text
        if is_user_prompt:
            if current_conv:
                conversations.append(current_conv)
            current_conv = {
                "user_text": user_text,
                "timestamp": timestamp,
                "messages": [(log_type, message_json, timestamp)],
                "is_continuation": bool(is_compact_summary),
            }
        elif current_conv:
            current_conv["messages"].append((log_type, message_json, timestamp))
    if current_conv:
        conversations.append(current_conv)

    total_convs = len(conversations)
    total_pages = (total_convs + PROMPTS_PER_PAGE - 1) // PROMPTS_PER_PAGE

    for page_num in range(1, total_pages + 1):
        start_idx = (page_num - 1) * PROMPTS_PER_PAGE
        end_idx = min(start_idx + PROMPTS_PER_PAGE, total_convs)
        page_convs = conversations[start_idx:end_idx]
        messages_html = []
        for conv in page_convs:
            is_first = True
            for log_type, message_json, timestamp in conv["messages"]:
                msg_html = render_message(log_type, message_json, timestamp)
                if msg_html:
                    # Wrap continuation summaries in collapsed details
                    if is_first and conv.get("is_continuation"):
                        msg_html = f'<details class="continuation"><summary>Session continuation summary</summary>{msg_html}</details>'
                    messages_html.append(msg_html)
                is_first = False
        pagination_html = generate_pagination_html(page_num, total_pages)
        page_template = get_template("page.html")
        page_content = page_template.render(
            css=CSS,
            js=JS,
            page_num=page_num,
            total_pages=total_pages,
            pagination_html=pagination_html,
            messages_html="".join(messages_html),
        )
        (output_dir / f"page-{page_num:03d}.html").write_text(
            page_content, encoding="utf-8"
        )
        click.echo(f"Generated page-{page_num:03d}.html")

    # Calculate overall stats and collect all commits for timeline
    total_tool_counts = {}
    total_messages = 0
    all_commits = []  # (timestamp, hash, message, page_num, conv_index)
    for i, conv in enumerate(conversations):
        total_messages += len(conv["messages"])
        stats = analyze_conversation(conv["messages"])
        for tool, count in stats["tool_counts"].items():
            total_tool_counts[tool] = total_tool_counts.get(tool, 0) + count
        page_num = (i // PROMPTS_PER_PAGE) + 1
        for commit_hash, commit_msg, commit_ts in stats["commits"]:
            all_commits.append((commit_ts, commit_hash, commit_msg, page_num, i))
    total_tool_calls = sum(total_tool_counts.values())
    total_commits = len(all_commits)

    # Build timeline items: prompts and commits merged by timestamp
    timeline_items = []

    # Add prompts
    prompt_num = 0
    for i, conv in enumerate(conversations):
        if conv.get("is_continuation"):
            continue
        if conv["user_text"].startswith("Stop hook feedback:"):
            continue
        prompt_num += 1
        page_num = (i // PROMPTS_PER_PAGE) + 1
        msg_id = make_msg_id(conv["timestamp"])
        link = f"page-{page_num:03d}.html#{msg_id}"
        rendered_content = render_markdown_text(conv["user_text"])

        # Collect all messages including from subsequent continuation conversations
        # This ensures long_texts from continuations appear with the original prompt
        all_messages = list(conv["messages"])
        for j in range(i + 1, len(conversations)):
            if not conversations[j].get("is_continuation"):
                break
            all_messages.extend(conversations[j]["messages"])

        # Analyze conversation for stats (excluding commits from inline display now)
        stats = analyze_conversation(all_messages)
        tool_stats_str = format_tool_stats(stats["tool_counts"])

        long_texts_html = ""
        for lt in stats["long_texts"]:
            rendered_lt = render_markdown_text(lt)
            long_texts_html += _macros.index_long_text(rendered_lt)

        stats_html = _macros.index_stats(tool_stats_str, long_texts_html)

        item_html = _macros.index_item(
            prompt_num, link, conv["timestamp"], rendered_content, stats_html
        )
        timeline_items.append((conv["timestamp"], "prompt", item_html))

    # Add commits as separate timeline items
    for commit_ts, commit_hash, commit_msg, page_num, conv_idx in all_commits:
        item_html = _macros.index_commit(
            commit_hash, commit_msg, commit_ts, _github_repo
        )
        timeline_items.append((commit_ts, "commit", item_html))

    # Sort by timestamp
    timeline_items.sort(key=lambda x: x[0])
    index_items = [item[2] for item in timeline_items]

    index_pagination = generate_index_pagination_html(total_pages)
    index_template = get_template("index.html")
    index_content = index_template.render(
        css=CSS,
        js=JS,
        pagination_html=index_pagination,
        prompt_num=prompt_num,
        total_messages=total_messages,
        total_tool_calls=total_tool_calls,
        total_commits=total_commits,
        total_pages=total_pages,
        index_items_html="".join(index_items),
    )
    index_path = output_dir / "index.html"
    index_path.write_text(index_content, encoding="utf-8")
    click.echo(
        f"Generated {index_path.resolve()} ({total_convs} prompts, {total_pages} pages)"
    )


@cli.command("web")
@click.argument("session_id", required=False)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Output directory. If not specified, writes to temp dir and opens in browser.",
)
@click.option(
    "-a",
    "--output-auto",
    is_flag=True,
    help="Auto-name output subdirectory based on session ID (uses -o as parent, or current dir).",
)
@click.option("--token", help="API access token (auto-detected from keychain on macOS)")
@click.option(
    "--org-uuid", help="Organization UUID (auto-detected from ~/.claude.json)"
)
@click.option(
    "--repo",
    help="GitHub repo (owner/name). Filters session list and sets default for commit links.",
)
@click.option(
    "--gist",
    is_flag=True,
    help="Upload to GitHub Gist and output a gisthost.github.io URL.",
)
@click.option(
    "--json",
    "include_json",
    is_flag=True,
    help="Include the JSON session data in the output directory.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated index.html in your default browser (default if no -o specified).",
)
def web_cmd(
    session_id,
    output,
    output_auto,
    token,
    org_uuid,
    repo,
    gist,
    include_json,
    open_browser,
):
    """Select and convert a web session from the Claude API to HTML.

    If SESSION_ID is not provided, displays an interactive picker to select a session.
    """
    try:
        token, org_uuid = resolve_credentials(token, org_uuid)
    except click.ClickException:
        raise

    # If no session ID provided, show interactive picker
    if session_id is None:
        try:
            sessions_data = fetch_sessions(token, org_uuid)
        except httpx.HTTPStatusError as e:
            raise click.ClickException(
                f"API request failed: {e.response.status_code} {e.response.text}"
            )
        except httpx.RequestError as e:
            raise click.ClickException(f"Network error: {e}")

        sessions = sessions_data.get("data", [])
        if not sessions:
            raise click.ClickException("No sessions found.")

        # Enrich sessions with repo information (extracted from session metadata)
        sessions = enrich_sessions_with_repos(sessions)

        # Filter by repo if specified
        if repo:
            sessions = filter_sessions_by_repo(sessions, repo)
            if not sessions:
                raise click.ClickException(f"No sessions found for repo: {repo}")

        # Build choices for questionary
        choices = []
        for s in sessions:
            sid = s.get("id", "unknown")
            display = format_session_for_display(s)
            choices.append(questionary.Choice(title=display, value=sid))

        selected = questionary.select(
            "Select a session to import:",
            choices=choices,
        ).ask()

        if selected is None:
            # User cancelled
            raise click.ClickException("No session selected.")

        session_id = selected

    # Fetch the session
    click.echo(f"Fetching session {session_id}...")
    try:
        session_data = fetch_session(token, org_uuid, session_id)
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"API request failed: {e.response.status_code} {e.response.text}"
        )
    except httpx.RequestError as e:
        raise click.ClickException(f"Network error: {e}")

    # Determine output directory and whether to open browser
    # If no -o specified, use temp dir and open browser by default
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        # Use -o as parent dir (or current dir), with auto-named subdirectory
        parent_dir = Path(output) if output else Path(".")
        output = parent_dir / session_id
    elif output is None:
        output = _temp_output_dir(f"claude-session-{session_id}")

    output = Path(output)
    click.echo(f"Generating HTML in {output}/...")
    generate_html_from_session_data(session_data, output, github_repo=repo)

    # Show output directory
    click.echo(f"Output: {output.resolve()}")

    # Save JSON session data if requested
    if include_json:
        output.mkdir(exist_ok=True)
        json_dest = output / f"{session_id}.json"
        with open(json_dest, "w") as f:
            json.dump(session_data, f, indent=2)
        json_size_kb = json_dest.stat().st_size / 1024
        click.echo(f"JSON: {json_dest} ({json_size_kb:.1f} KB)")

    if gist:
        # Inject gist preview JS and create gist
        inject_gist_preview_js(output)
        click.echo("Creating GitHub gist...")
        gist_id, gist_url = create_gist(output)
        preview_url = f"https://gisthost.github.io/?{gist_id}/index.html"
        click.echo(f"Gist: {gist_url}")
        click.echo(f"Preview: {preview_url}")

    if open_browser or auto_open:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)


@cli.command("all")
@click.option(
    "-s",
    "--source",
    type=click.Path(exists=True),
    help="Source directory containing Claude projects (default: ~/.claude/projects).",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    default="./claude-archive",
    help="Output directory for the archive (default: ./claude-archive).",
)
@click.option(
    "--include-agents",
    is_flag=True,
    help="Include agent-* session files (excluded by default).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be converted without creating files.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated archive in your default browser.",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help="Suppress all output except errors.",
)
def all_cmd(source, output, include_agents, dry_run, open_browser, quiet):
    """Convert all local Claude Code sessions to a browsable HTML archive.

    Creates a directory structure with:
    - Master index listing all projects
    - Per-project pages listing sessions
    - Individual session transcripts
    """
    # Default source folder
    if source is None:
        source = Path.home() / ".claude" / "projects"
    else:
        source = Path(source)

    if not source.exists():
        raise click.ClickException(f"Source directory not found: {source}")

    output = Path(output)

    if not quiet:
        click.echo(f"Scanning {source}...")

    projects = find_all_sessions(source, include_agents=include_agents)

    if not projects:
        if not quiet:
            click.echo("No sessions found.")
        return

    # Calculate totals
    total_sessions = sum(len(p["sessions"]) for p in projects)

    if not quiet:
        click.echo(f"Found {len(projects)} projects with {total_sessions} sessions")

    if dry_run:
        # Dry-run always outputs (it's the point of dry-run), but respects --quiet
        if not quiet:
            click.echo("\nDry run - would convert:")
            for project in projects:
                click.echo(
                    f"\n  {project['name']} ({len(project['sessions'])} sessions)"
                )
                for session in project["sessions"][:3]:  # Show first 3
                    mod_time = datetime.fromtimestamp(session["mtime"])
                    click.echo(
                        f"    - {session['path'].stem} ({mod_time.strftime('%Y-%m-%d')})"
                    )
                if len(project["sessions"]) > 3:
                    click.echo(f"    ... and {len(project['sessions']) - 3} more")
        return

    if not quiet:
        click.echo(f"\nGenerating archive in {output}...")

    # Progress callback for non-quiet mode
    def on_progress(project_name, session_name, current, total):
        if not quiet and current % 10 == 0:
            click.echo(f"  Processed {current}/{total} sessions...")

    # Generate the archive using the library function
    stats = generate_batch_html(
        source,
        output,
        include_agents=include_agents,
        progress_callback=on_progress,
    )

    # Report any failures
    if stats["failed_sessions"]:
        click.echo(f"\nWarning: {len(stats['failed_sessions'])} session(s) failed:")
        for failure in stats["failed_sessions"]:
            click.echo(
                f"  {failure['project']}/{failure['session']}: {failure['error']}"
            )

    if not quiet:
        click.echo(
            f"\nGenerated archive with {stats['total_projects']} projects, "
            f"{stats['total_sessions']} sessions"
        )
        click.echo(f"Output: {output.resolve()}")

    if open_browser:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)


def main():
    cli()
