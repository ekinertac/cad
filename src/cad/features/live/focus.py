"""
features/live/focus.py — bring the terminal tab running a live session
to the foreground.

Today this means iTerm2. cad resolves the live claude PID's tty via
``ps -p <pid> -o tty=``, then runs an AppleScript that walks every
iTerm2 window/tab/session looking for one whose ``tty`` property
matches. On a match it selects the chain and ``activate``\\s the app.

Designed as the integration seam for other terminals (agamon next):
a feature like this would add another branch keyed off
``$TERM_PROGRAM``. Until that lands, focus on Apple Terminal / tmux /
Alacritty / ssh contexts silently returns False and the caller falls
back to peek.

May import from: stdlib. May NOT import from: ``core/`` (no cad
state needed — focus is a pure pid→terminal-tab operation) or
sibling features.
"""

import os
import subprocess


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
