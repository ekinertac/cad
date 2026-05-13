"""
core/picker.py — the interactive list picker and text prompts.

The big function here is :func:`select_entry` — a prompt_toolkit-based
list picker with per-key actions, modal search, optional background
refresh, page sizing, full-screen mode, and non-selectable header
rows. Every feature command in cad ends up calling it (or its
backwards-compatible wrapper :func:`select_session_action`).

Also home to the three small text prompts (:func:`prompt_for_title`,
:func:`prompt_confirm`, :func:`prompt_for_cwd`) used by the rename /
move / confirm-destructive-action flows.

May import from: prompt_toolkit, click. May NOT import from:
``features/`` or any other cad module.

Design notes worth knowing before editing:

- The cursor never lands on a row tagged ``header: True``. Navigation
  helpers (``next_selectable``) walk past them; ``viewport()`` snaps
  the cursor off if it would land on one. Used by ``cad live`` to
  render project group labels.
- ``page_size=None`` disables pagination — the window grows to fit
  every entry. Used by ``cad live`` where the user wants the whole
  dashboard visible.
- ``full_screen=True`` takes over the terminal's alternate screen
  buffer (vim / htop style). Inline pickers keep the default so
  scrollback isn't lost.
- ``refresh_callback`` is invoked on a daemon thread every
  ``refresh_interval`` seconds. It mutates the entries list in place
  and triggers a redraw via ``app.invalidate()``. Cursor position is
  preserved by matching ``session_id`` / ``display`` so a row added
  or removed mid-refresh doesn't bounce the selection.
"""

from pathlib import Path

import click


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
    status_window = Window(
        content=FormattedTextControl(text=get_status_text), height=1
    )
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
