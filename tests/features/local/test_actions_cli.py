"""Smoke tests for the action-as-command CLI surface
(features/local/commands_action). Verifies that the click wrappers
resolve the ref and call the right primitive — actual primitive
behavior is covered by their own dedicated tests."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner


def _setup_single_session(tmp_path, monkeypatch):
    """Lay out one claude session at a predictable location and
    return (fake_home, jsonl_path, session_id)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    proj = fake_home / ".claude" / "projects" / "-Users-x-Code-foo"
    proj.mkdir(parents=True)
    sid = "abc1-2345-6789-aaaa-bbbbccccdddd"
    jsonl = proj / f"{sid}.jsonl"
    jsonl.write_text(
        '{"type":"summary","summary":"hi"}\n'
        f'{{"type":"user","cwd":"/Users/x/Code/foo","timestamp":"2025-01-01T00:00:00Z",'
        '"message":{"role":"user","content":"hello"}}\n'
    )
    return fake_home, jsonl, sid


class TestRenameCmd:
    def test_rename_sets_title_override(self, tmp_path, monkeypatch):
        from cad import cli

        fake_home, _, sid = _setup_single_session(tmp_path, monkeypatch)

        result = CliRunner().invoke(cli, ["rename", sid, "My new title"])
        assert result.exit_code == 0, result.output

        # Verify the sidecar got written with the expected key.
        titles_file = fake_home / ".cad" / "titles.json"
        assert titles_file.exists()
        data = json.loads(titles_file.read_text())
        assert data == {f"claude:{sid}": "My new title"}

    def test_rename_accepts_prefix(self, tmp_path, monkeypatch):
        from cad import cli

        fake_home, _, sid = _setup_single_session(tmp_path, monkeypatch)

        result = CliRunner().invoke(cli, ["rename", "abc1", "Prefix worked"])
        assert result.exit_code == 0, result.output
        data = json.loads((fake_home / ".cad" / "titles.json").read_text())
        assert data[f"claude:{sid}"] == "Prefix worked"

    def test_rename_unknown_ref_errors(self, tmp_path, monkeypatch):
        from cad import cli

        _setup_single_session(tmp_path, monkeypatch)
        result = CliRunner().invoke(cli, ["rename", "nope", "x"])
        assert result.exit_code != 0
        assert "No session matches" in result.output


class TestArchiveCmd:
    def test_archive_with_ref_moves_session(self, tmp_path, monkeypatch):
        from cad import cli

        fake_home, jsonl, sid = _setup_single_session(tmp_path, monkeypatch)

        result = CliRunner().invoke(cli, ["archive", sid])
        assert result.exit_code == 0, result.output
        # Original gone, archive entry present.
        assert not jsonl.exists()
        archive_dir = fake_home / ".cad" / "archive"
        archived = list(archive_dir.glob("*.jsonl"))
        assert len(archived) == 1
        assert archived[0].name == f"{sid}.jsonl"

    def test_archive_at_last_works(self, tmp_path, monkeypatch):
        from cad import cli

        fake_home, jsonl, sid = _setup_single_session(tmp_path, monkeypatch)

        result = CliRunner().invoke(cli, ["archive", "@last"])
        assert result.exit_code == 0, result.output
        assert not jsonl.exists()


class TestMoveCmd:
    def test_move_writes_cwd_override(self, tmp_path, monkeypatch):
        from cad import cli

        fake_home, _, sid = _setup_single_session(tmp_path, monkeypatch)
        new_cwd = tmp_path / "new-home"
        new_cwd.mkdir()

        result = CliRunner().invoke(cli, ["move", sid, str(new_cwd)])
        assert result.exit_code == 0, result.output

        overrides = json.loads((fake_home / ".cad" / "cwd-overrides.json").read_text())
        # Path gets expanded + resolved (so we compare via resolve()).
        assert overrides[f"claude:{sid}"] == str(new_cwd.resolve())

    def test_move_refuses_non_directory(self, tmp_path, monkeypatch):
        from cad import cli

        _setup_single_session(tmp_path, monkeypatch)
        result = CliRunner().invoke(cli, ["move", "abc1", "/no/such/path/here"])
        assert result.exit_code != 0
        assert "Not a directory" in result.output


class TestRestoreCmd:
    def test_restore_round_trip(self, tmp_path, monkeypatch):
        from cad import cli

        fake_home, jsonl, sid = _setup_single_session(tmp_path, monkeypatch)
        # Archive first.
        archive_result = CliRunner().invoke(cli, ["archive", sid])
        assert archive_result.exit_code == 0, archive_result.output
        assert not jsonl.exists()
        # Now restore.
        restore_result = CliRunner().invoke(cli, ["restore", sid])
        assert restore_result.exit_code == 0, restore_result.output
        assert jsonl.exists()


class TestResumeCmd:
    """``cad resume`` replaces the process via os.execvp. We monkeypatch
    that out and verify the wrapper called it with the right argv."""

    def test_resume_execs_claude_with_resume_uuid(self, tmp_path, monkeypatch):
        from cad import cli
        import cad as ct
        from cad.core import providers as providers_mod

        fake_home, _, sid = _setup_single_session(tmp_path, monkeypatch)
        # resume_session checks Path(cwd).is_dir() before exec'ing.
        # We can't actually create /Users/x/Code/foo on the test
        # machine, so monkeypatch the gate to always pass for this
        # smoke test.
        from cad.core import providers as providers_mod

        monkeypatch.setattr(
            providers_mod.Path,
            "is_dir",
            lambda self: True,
        )

        execs = []
        monkeypatch.setattr(providers_mod.os, "chdir", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            providers_mod.os, "execvp", lambda cmd, argv: execs.append((cmd, argv))
        )

        result = CliRunner().invoke(cli, ["resume", sid])
        assert result.exit_code == 0, result.output
        assert len(execs) == 1
        cmd, argv = execs[0]
        assert cmd == "claude"
        # argv ends with the resolved UUID, not the prefix.
        assert argv[-1] == sid
