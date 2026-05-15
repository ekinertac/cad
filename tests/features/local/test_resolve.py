"""Tests for the session-ID resolver used by the action-as-command CLI."""

from pathlib import Path

import pytest


def _setup(tmp_path, monkeypatch):
    """Lay out three claude sessions across two projects with distinct
    mtimes so the resolver has something to disambiguate."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    proj_a = fake_home / ".claude" / "projects" / "-Users-x-Code-alpha"
    proj_a.mkdir(parents=True)
    proj_b = fake_home / ".claude" / "projects" / "-Users-x-Code-beta"
    proj_b.mkdir(parents=True)
    sessions = {}
    for project, sid, mtime in (
        (proj_a, "aaaa-1111-1111", 1000.0),
        (proj_a, "aaaa-2222-2222", 2000.0),
        (proj_b, "bbbb-3333-3333", 3000.0),
    ):
        f = project / f"{sid}.jsonl"
        cwd = "/Users/x/Code/alpha" if project == proj_a else "/Users/x/Code/beta"
        f.write_text(
            f'{{"type":"summary","summary":"s {sid}"}}\n'
            f'{{"type":"user","cwd":"{cwd}",'
            f'"message":{{"content":"hi"}}}}\n'
        )
        # Stamp predictable mtimes so @last is deterministic.
        import os

        os.utime(f, (mtime, mtime))
        sessions[sid] = f
    return sessions


class TestResolveSessionId:
    def test_full_uuid_match(self, tmp_path, monkeypatch):
        from cad.features.local.resolve import resolve_session_id

        _setup(tmp_path, monkeypatch)
        s = resolve_session_id("aaaa-1111-1111")
        assert s["session_id"] == "aaaa-1111-1111"

    def test_prefix_match(self, tmp_path, monkeypatch):
        from cad.features.local.resolve import resolve_session_id

        _setup(tmp_path, monkeypatch)
        s = resolve_session_id("aaaa-1111")
        assert s["session_id"] == "aaaa-1111-1111"

    def test_ambiguous_prefix_raises(self, tmp_path, monkeypatch):
        from cad.features.local.resolve import resolve_session_id, AmbiguousSessionRef

        _setup(tmp_path, monkeypatch)
        with pytest.raises(AmbiguousSessionRef):
            resolve_session_id("aaaa")

    def test_no_match_raises(self, tmp_path, monkeypatch):
        from cad.features.local.resolve import resolve_session_id, SessionNotFound

        _setup(tmp_path, monkeypatch)
        with pytest.raises(SessionNotFound):
            resolve_session_id("zzzz-9999")

    def test_at_last_returns_newest(self, tmp_path, monkeypatch):
        from cad.features.local.resolve import resolve_session_id

        _setup(tmp_path, monkeypatch)
        # bbbb-... has the highest mtime (3000.0).
        s = resolve_session_id("@last")
        assert s["session_id"] == "bbbb-3333-3333"

    def test_at_last_scoped_to_cwd(self, tmp_path, monkeypatch):
        from cad.features.local.resolve import resolve_session_id

        _setup(tmp_path, monkeypatch)
        # Among alpha's sessions, aaaa-2222 (mtime 2000) is newer.
        s = resolve_session_id("@last", cwd="/Users/x/Code/alpha")
        assert s["session_id"] == "aaaa-2222-2222"

    def test_cwd_scope_filters_prefix(self, tmp_path, monkeypatch):
        from cad.features.local.resolve import resolve_session_id

        _setup(tmp_path, monkeypatch)
        # `aaaa` is ambiguous globally but unique inside beta (no aaaa
        # sessions there). Scoped to alpha, both aaaa-* sessions are
        # still ambiguous so we still raise — but scoping to beta with
        # the `bbbb` prefix should succeed.
        s = resolve_session_id("bbbb", cwd="/Users/x/Code/beta")
        assert s["session_id"] == "bbbb-3333-3333"

    def test_at_live_uses_live_callback(self, tmp_path, monkeypatch):
        """@live resolves through a caller-supplied callback so the
        resolver itself stays decoupled from features/live."""
        from cad.features.local.resolve import resolve_session_id, NoLiveSession

        sessions = _setup(tmp_path, monkeypatch)
        fake_path = sessions["aaaa-2222-2222"]

        # Stub the live-state lookup: exactly one bound session.
        def fake_live_state():
            return {
                "bound_uuids": {
                    "aaaa-2222-2222": {"pid": 1, "cwd": "/Users/x/Code/alpha"}
                },
                "unbound_cwds": {},
            }

        import cad

        monkeypatch.setattr(cad, "find_live_claude_state", fake_live_state)

        s = resolve_session_id("@live")
        assert s["session_id"] == "aaaa-2222-2222"

    def test_at_live_raises_when_nothing_running(self, tmp_path, monkeypatch):
        from cad.features.local.resolve import resolve_session_id, NoLiveSession

        _setup(tmp_path, monkeypatch)

        import cad

        monkeypatch.setattr(
            cad,
            "find_live_claude_state",
            lambda: {"bound_uuids": {}, "unbound_cwds": {}},
        )
        with pytest.raises(NoLiveSession):
            resolve_session_id("@live")

    def test_at_live_raises_when_ambiguous(self, tmp_path, monkeypatch):
        from cad.features.local.resolve import resolve_session_id, AmbiguousSessionRef

        _setup(tmp_path, monkeypatch)

        import cad

        monkeypatch.setattr(
            cad,
            "find_live_claude_state",
            lambda: {
                "bound_uuids": {
                    "aaaa-2222-2222": {"pid": 1, "cwd": "/Users/x/Code/alpha"},
                    "bbbb-3333-3333": {"pid": 2, "cwd": "/Users/x/Code/beta"},
                },
                "unbound_cwds": {},
            },
        )
        with pytest.raises(AmbiguousSessionRef):
            resolve_session_id("@live")
