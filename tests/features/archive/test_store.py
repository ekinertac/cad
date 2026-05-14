"""Tests for the archive storage layer (features/archive/store)."""

import json
from pathlib import Path

import pytest


class TestArchiveSession:
    """archive_session moves a claude JSONL out of ~/.claude/projects/
    into ~/.cad/archive/ so it stops showing in cad / claude pickers
    but is recoverable via restore_session.
    """

    def _setup(self, tmp_path, monkeypatch):
        """Lay out a claude session at the expected on-disk location.
        Returns (fake_home, session_dict, jsonl_path)."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        proj_dir = fake_home / ".claude" / "projects" / "-Users-x-Code-foo"
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / "abc-123.jsonl"
        jsonl.write_text(
            '{"type":"user","cwd":"/Users/x/Code/foo",'
            '"message":{"content":"hello"}}\n'
        )
        session = {
            "provider": "claude",
            "session_id": "abc-123",
            "cwd": "/Users/x/Code/foo",
            "filepath": jsonl,
        }
        return fake_home, session, jsonl

    def test_moves_jsonl_into_archive_dir(self, tmp_path, monkeypatch):
        """The JSONL is gone from ~/.claude/projects/ after archive
        and exists at ~/.cad/archive/ with the same content."""
        from cad.features.archive.store import archive_session

        fake_home, session, jsonl = self._setup(tmp_path, monkeypatch)
        original_content = jsonl.read_text()

        dest = archive_session(session)

        # Original is gone.
        assert not jsonl.exists(), "JSONL still at original location"
        # Archive dir exists at the conventional location.
        archive_root = fake_home / ".cad" / "archive"
        assert archive_root.is_dir()
        # Returned path is inside archive_root.
        assert str(dest).startswith(str(archive_root))
        # Content survived intact.
        assert dest.read_text() == original_content

    def test_refuses_to_archive_live_session(self, tmp_path, monkeypatch):
        """Archiving a session that's currently being written to would
        race with claude. Refuse with a clear error so the caller can
        tell the user."""
        from cad.features.archive.store import (
            ArchiveError,
            archive_session,
        )

        _, session, jsonl = self._setup(tmp_path, monkeypatch)
        session["live"] = True

        with pytest.raises(ArchiveError):
            archive_session(session)
        # And the file stays put.
        assert jsonl.exists()

    def test_refuses_non_claude_provider(self, tmp_path, monkeypatch):
        """Archive only makes sense for file-backed providers right
        now (claude). Codex/pi could be added later; opencode/forge
        are SQLite-backed and don't fit the move-the-file model.
        Refuse with a clear error rather than silently doing nothing."""
        from cad.features.archive.store import (
            ArchiveError,
            archive_session,
        )

        _, session, jsonl = self._setup(tmp_path, monkeypatch)
        session["provider"] = "codex"

        with pytest.raises(ArchiveError):
            archive_session(session)
        assert jsonl.exists()

    def test_collision_gets_timestamp_suffix(self, tmp_path, monkeypatch):
        """If we archive a session, then a new claude run reuses the
        same UUID (uncommon but possible across machines), the second
        archive should not clobber the first. Add a unique suffix."""
        from cad.features.archive.store import archive_session

        fake_home, session, jsonl = self._setup(tmp_path, monkeypatch)
        first_dest = archive_session(session)
        assert first_dest.exists()

        # Recreate the source and archive again.
        jsonl.write_text("second run\n")
        session["filepath"] = jsonl
        second_dest = archive_session(session)
        assert second_dest.exists()
        assert second_dest != first_dest, "collision overwrote the first archive"
        assert first_dest.read_text() != "second run\n"


class TestRestoreSession:
    """Move an archived session back to its original location so
    claude --resume and cad can see it again."""

    def test_restore_round_trip(self, tmp_path, monkeypatch):
        from cad.features.archive.store import archive_session, restore_session
        import cad.core.discovery as discovery

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        proj_dir = fake_home / ".claude" / "projects" / "-Users-x-Code-foo"
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / "abc-123.jsonl"
        jsonl.write_text(
            '{"type":"user","cwd":"/Users/x/Code/foo",' '"message":{"content":"hi"}}\n'
        )
        original_content = jsonl.read_text()
        session = {
            "provider": "claude",
            "session_id": "abc-123",
            "cwd": "/Users/x/Code/foo",
            "filepath": jsonl,
        }

        dest = archive_session(session)
        assert not jsonl.exists()

        restored = restore_session(
            {
                "provider": "claude",
                "session_id": "abc-123",
                "cwd": "/Users/x/Code/foo",
                "filepath": dest,
            }
        )
        assert restored == jsonl
        assert jsonl.exists()
        assert jsonl.read_text() == original_content
        # Archive entry should be gone after restore.
        assert not dest.exists()


class TestFindArchivedSessions:
    """List sessions currently in the archive — used by `cad archive`."""

    def test_lists_archived_files(self, tmp_path, monkeypatch):
        from cad.features.archive.store import archive_session, find_archived_sessions

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        proj_dir = fake_home / ".claude" / "projects" / "-Users-x-Code-foo"
        proj_dir.mkdir(parents=True)
        # Two sessions, archive both.
        for sid in ("first", "second"):
            jsonl = proj_dir / f"{sid}.jsonl"
            jsonl.write_text(
                f'{{"type":"user","cwd":"/Users/x/Code/foo",'
                f'"message":{{"content":"hi {sid}"}}}}\n'
            )
            archive_session(
                {
                    "provider": "claude",
                    "session_id": sid,
                    "cwd": "/Users/x/Code/foo",
                    "filepath": jsonl,
                }
            )

        sessions = find_archived_sessions()
        # Order doesn't matter; check the set of session_ids.
        sids = {s["session_id"] for s in sessions}
        assert sids == {"first", "second"}
        # Each entry has the standard session-dict shape.
        for s in sessions:
            assert s["provider"] == "claude"
            assert s["cwd"] == "/Users/x/Code/foo"
            assert s["filepath"].exists()

    def test_empty_when_archive_missing(self, tmp_path, monkeypatch):
        """Before anyone archives anything, the archive dir doesn't
        exist yet. Helper returns [] rather than raising."""
        from cad.features.archive.store import find_archived_sessions

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert find_archived_sessions() == []
