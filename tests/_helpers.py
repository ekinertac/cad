"""
tests/_helpers.py — shared scaffolding for the cad test suite.

Helpers used across multiple test files: synthetic session-file
writers, fake-home builders, mock select_entry/select factories.
Kept out of conftest.py so they can be imported explicitly rather
than auto-injected.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock


# Canonical location of the HTML test fixtures (sample_session.json/.jsonl).
# Tests that pull them in used to do ``Path(__file__).parent / "sample_session.json"``;
# after the test-layout mirror they live two levels deeper, so use this constant
# instead of computing relative-to-test paths inside each file.
FIXTURE_DIR = Path(__file__).parent


def _write_session(folder, name, summary="Session", cwd=None):
    """Helper: write a minimal valid claude session JSONL into folder.

    The JSONL includes a ``cwd`` field on the user line so the new
    cwd-based grouping picks the file up. Tests that need a specific
    project directory pass it via ``cwd``; default is ``str(folder)``
    (so each session group lives under its own synthetic project).
    """
    if cwd is None:
        cwd = str(folder)
    f = folder / name
    f.write_text(
        f'{{"type":"summary","summary":"{summary}"}}\n'
        f'{{"type":"user","cwd":"{cwd}","timestamp":"2025-01-01T00:00:00Z",'
        '"message":{"role":"user","content":"test"}}\n'
    )
    return f


def _make_session(provider, session_id, cwd, filepath=None):
    """Helper: build a session dict with the minimum keys resume_session
    inspects. Tests don't need the lazy-loaded summary/display fields."""
    return {
        "provider": provider,
        "session_id": session_id,
        "cwd": str(cwd),
        "filepath": filepath or Path(f"/fake/{session_id}.jsonl"),
        "mtime": 0.0,
        "size": 0,
        "summary": None,
        "display": None,
    }


def _make_mock_select(returns, calls=None):
    """Build a questionary.select stand-in that returns the next value from
    `returns` on each .ask() call. Used to script the project picker.

    Special sentinel ``"__first__"`` picks the first choice's ``value`` —
    handy for tests that want the discovery layer to run for real and just
    auto-select what it found, instead of hand-constructing project dicts.

    If `calls` (a list) is passed, each invocation appends its kwargs to it
    so tests can assert what was passed to questionary.select.
    """
    queue = list(returns)

    class MockSelect:
        def __init__(self, *args, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            self._kwargs = kwargs
            self._args = args

        def ask(self):
            value = queue.pop(0)
            if value == "__first__":
                choices = self._kwargs.get("choices") or (
                    self._args[1] if len(self._args) > 1 else []
                )
                return choices[0].value
            return value

    return MockSelect


def _make_mock_select_entry(returns):
    """Stand-in for select_entry — pops from a queue on each call.

    ``"__first__"`` returns ``(entries[0], "select")``.
    ``("__first__", "html")`` returns ``(entries[0], "html")``.
    ``None`` returns ``None`` (cancellation).
    Plain tuples are returned as-is.
    """
    queue = list(returns)

    def fake(entries, actions=None, back_action=None, initial_selected=0, **_kw):
        # Tolerate any future select_entry kwargs (refresh_callback,
        # page_size, full_screen, deep_search_callback, …) so test
        # mocks don't need touching every time the picker grows
        # a new option.
        if not queue:
            raise AssertionError("select_entry called more times than scripted")
        v = queue.pop(0)
        if v is None:
            return None
        if v == "__first__":
            return (entries[0], "select")
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "__first__":
            return (entries[0], v[1])
        return v

    return fake


def _set_up_fake_home_with_session(tmp_path, monkeypatch, cwd_dir=None):
    """Build a fake ~/.claude/projects/<x>/<id>.jsonl pointing at cwd_dir.

    Returns (fake_home, project_cwd, session_file). The session file's cwd
    is `cwd_dir` (must exist for resume tests; created if not given).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    if cwd_dir is None:
        cwd_dir = tmp_path / "real-project"
        cwd_dir.mkdir()

    project_dir = fake_home / ".claude" / "projects" / "test-project"
    project_dir.mkdir(parents=True)
    session_file = project_dir / "abc-123.jsonl"
    session_file.write_text(
        '{"type":"summary","summary":"Test"}\n'
        f'{{"type":"user","cwd":"{cwd_dir}","message":{{"content":"hi"}}}}\n'
    )
    return fake_home, cwd_dir, session_file
