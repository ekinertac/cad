"""Tests for the cross-session text search engine
(features/search/find.search_sessions)."""

from pathlib import Path

import pytest


def _write_claude_session(folder, sid, *user_lines, cwd=None):
    """Write a minimal claude JSONL with the given user message lines."""
    if cwd is None:
        cwd = str(folder)
    f = folder / f"{sid}.jsonl"
    rows = [f'{{"type":"summary","summary":"summary for {sid}"}}']
    for line in user_lines:
        # JSON-escape the line so it's safe inside the JSONL.
        import json as _json

        rows.append(
            f'{{"type":"user","cwd":"{cwd}","timestamp":"2025-01-01T00:00:00Z",'
            f'"message":{{"role":"user","content":{_json.dumps(line)}}}}}'
        )
    f.write_text("\n".join(rows) + "\n")
    return f


class TestSearchSessions:
    """search_sessions scans local sessions for a substring (case-insensitive
    by default) and returns hit dicts with a snippet and match count."""

    def _setup(self, tmp_path, monkeypatch):
        """Lay out two claude projects with three sessions total."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        proj_a = fake_home / ".claude" / "projects" / "-Users-x-Code-alpha"
        proj_a.mkdir(parents=True)
        proj_b = fake_home / ".claude" / "projects" / "-Users-x-Code-beta"
        proj_b.mkdir(parents=True)

        _write_claude_session(
            proj_a,
            "a-1",
            "Implement viewport culling for the tile renderer.",
            cwd="/Users/x/Code/alpha",
        )
        _write_claude_session(
            proj_a,
            "a-2",
            "Unrelated session about coffee orders.",
            cwd="/Users/x/Code/alpha",
        )
        _write_claude_session(
            proj_b,
            "b-1",
            "Fix the DDA edge case in the viewport culling pass.",
            cwd="/Users/x/Code/beta",
        )
        return fake_home

    def test_finds_match_across_projects(self, tmp_path, monkeypatch):
        from cad.features.search.find import search_sessions

        self._setup(tmp_path, monkeypatch)

        hits = search_sessions("viewport culling")

        sids = sorted(h["session_id"] for h in hits)
        assert sids == ["a-1", "b-1"]

    def test_hit_includes_snippet_and_match_count(self, tmp_path, monkeypatch):
        from cad.features.search.find import search_sessions

        self._setup(tmp_path, monkeypatch)
        hits = search_sessions("viewport culling")
        a1 = next(h for h in hits if h["session_id"] == "a-1")
        # Snippet contains the matched phrase.
        assert "viewport culling" in a1["snippet"].lower()
        # Exactly one match in a-1 (one user message mentioning the phrase).
        assert a1["match_count"] == 1

    def test_case_insensitive_by_default(self, tmp_path, monkeypatch):
        from cad.features.search.find import search_sessions

        self._setup(tmp_path, monkeypatch)
        upper = search_sessions("VIEWPORT CULLING")
        lower = search_sessions("viewport culling")
        # Same hits regardless of query casing.
        assert {h["session_id"] for h in upper} == {h["session_id"] for h in lower}

    def test_no_hits_for_missing_phrase(self, tmp_path, monkeypatch):
        from cad.features.search.find import search_sessions

        self._setup(tmp_path, monkeypatch)
        assert search_sessions("nothing matches this") == []

    def test_cwd_scope_filters_to_one_project(self, tmp_path, monkeypatch):
        from cad.features.search.find import search_sessions

        self._setup(tmp_path, monkeypatch)
        hits = search_sessions("viewport culling", cwd="/Users/x/Code/alpha")
        # Only the alpha-project session matches; beta is filtered out.
        assert [h["session_id"] for h in hits] == ["a-1"]

    def test_limit_caps_result_count(self, tmp_path, monkeypatch):
        from cad.features.search.find import search_sessions

        self._setup(tmp_path, monkeypatch)
        hits = search_sessions("viewport culling", limit=1)
        assert len(hits) == 1

    def test_provider_scope(self, tmp_path, monkeypatch):
        from cad.features.search.find import search_sessions

        self._setup(tmp_path, monkeypatch)
        # No codex sessions on disk; provider=codex should return zero hits
        # even though the phrase exists in claude sessions.
        hits = search_sessions("viewport culling", provider="codex")
        assert hits == []

    def test_hit_carries_standard_session_shape(self, tmp_path, monkeypatch):
        """Hits should be drop-in compatible with the existing picker
        — they need provider / session_id / cwd / filepath / mtime so
        load_session_summary and the standard actions work without
        special-casing."""
        from cad.features.search.find import search_sessions

        self._setup(tmp_path, monkeypatch)
        h = search_sessions("viewport culling")[0]
        for key in ("provider", "session_id", "cwd", "filepath", "mtime"):
            assert key in h, f"missing {key!r}"
        assert h["provider"] == "claude"
        assert isinstance(h["filepath"], Path)

    def test_multiple_matches_in_one_session_counted(self, tmp_path, monkeypatch):
        from cad.features.search.find import search_sessions

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        proj = fake_home / ".claude" / "projects" / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        # Three lines, all mentioning the phrase.
        _write_claude_session(
            proj,
            "many",
            "first viewport culling note",
            "second viewport culling note",
            "third viewport culling note",
            cwd="/Users/x/Code/foo",
        )
        hits = search_sessions("viewport culling")
        assert len(hits) == 1
        assert hits[0]["match_count"] == 3
