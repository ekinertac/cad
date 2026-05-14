"""Auto-split from tests/test_generate_html.py during the
feature-based refactor. See cad/features/ for the production layout
this mirrors.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from cad import (
    cli,
    main,
    # core/session_model
    parse_session_file,
    get_session_summary,
    get_session_cwd,
    get_claude_session_metadata,
    get_session_transcript,
    extract_text_from_content,
    # core/discovery
    find_local_sessions,
    find_claude_sessions,
    find_codex_sessions,
    find_pi_sessions,
    find_opencode_sessions,
    find_forge_sessions,
    get_codex_summary,
    get_pi_summary,
    # core/projects
    find_local_projects,
    find_all_sessions,
    get_project_display_name,
    load_session_summary,
    # core/overrides
    save_title_override,
    get_title_override,
    save_cwd_override,
    get_cwd_override,
    _atomic_write_json,
    # core/picker
    select_entry,
    select_session_action,
    prompt_for_title,
    prompt_for_cwd,
    prompt_confirm,
    # core/providers
    resume_session,
    new_session,
    PROVIDER_BADGES,
    PROVIDER_RESUME_COMMANDS,
    # core/util
    _temp_output_dir,
    _prune_temp_outputs,
    _loading_message,
    # features/html
    generate_html,
    generate_batch_html,
    generate_html_from_session_data,
    detect_github_repo,
    render_markdown_text,
    format_json,
    is_json_like,
    render_todo_write,
    render_write_tool,
    render_edit_tool,
    render_bash_tool,
    render_content_block,
    render_message,
    analyze_conversation,
    format_tool_stats,
    is_tool_result_message,
    inject_gist_preview_js,
    create_gist,
    is_url,
    fetch_url_to_tempfile,
    GIST_PREVIEW_JS,
    # features/web
    resolve_credentials,
    fetch_sessions,
    fetch_session,
    enrich_sessions_with_repos,
    filter_sessions_by_repo,
    extract_repo_from_session,
    format_session_for_display,
    get_access_token_from_keychain,
    get_org_uuid_from_config,
    CredentialsError,
    # features/local
    peek_session,
    summarize_session,
    # features/project_rename
    migrate_claude_project,
    # features/shell_init
    SHELL_WRAPPERS,
    # features/live
    find_live_claude_state,
    _annotate_sessions_with_live_state,
    _build_live_entries,
    focus_live_session,
)

from tests._helpers import (
    _write_session,
    _make_session,
    _make_mock_select,
    _make_mock_select_entry,
    _set_up_fake_home_with_session,
)


class HTMLSnapshotExtension(SingleFileSnapshotExtension):
    """Snapshot extension that saves HTML files."""

    _write_mode = WriteMode.TEXT
    file_extension = "html"


@pytest.fixture
def snapshot_html(snapshot):
    """Fixture for HTML file snapshots."""
    return snapshot.use_extension(HTMLSnapshotExtension)


class TestPeekAction:
    """The picker's `p` action runs peek_session and stays in the loop."""

    def test_peek_invokes_peek_session_and_preserves_cursor(
        self, tmp_path, monkeypatch
    ):
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)

        call_log = []

        def fake_select_entry(
            entries, actions=None, back_action=None, initial_selected=0
        ):
            call_log.append(
                {
                    "n_entries": len(entries),
                    "back_action": back_action,
                    "initial_selected": initial_selected,
                }
            )
            if len(call_log) == 1:
                return entries[0], "open"  # project
            if len(call_log) == 2:
                return entries[0], "peek"  # session: peek
            return None  # session: quit

        peek_calls = []
        monkeypatch.setattr(
            ct, "peek_session", lambda session: peek_calls.append(session)
        )
        monkeypatch.setattr(ct, "select_entry", fake_select_entry)

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0
        # Peek was invoked exactly once.
        assert len(peek_calls) == 1
        # After peek, the picker re-rendered with initial_selected pointing
        # back at the row we peeked at (index 0 in this single-session fixture).
        session_picker_calls = [c for c in call_log if c["back_action"] == "back"]
        assert len(session_picker_calls) >= 2
        assert session_picker_calls[1]["initial_selected"] == 0


class TestArchiveAction:
    """The picker's `d` action moves the session into ~/.cad/archive/
    and stays in the loop on the same project. Confirm prompt + live
    refusal guard both belong to the storage layer (tested there);
    here we just verify the wiring."""

    def test_archive_action_moves_jsonl_then_resumes_picker(
        self, tmp_path, monkeypatch
    ):
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        fake_home, _, jsonl = _set_up_fake_home_with_session(tmp_path, monkeypatch)
        # Auto-confirm the "Archive this session?" prompt.
        monkeypatch.setattr(ct, "prompt_confirm", lambda *a, **kw: True)

        archive_calls = []
        original_archive = ct.archive_session

        def spy_archive(session):
            archive_calls.append(session["session_id"])
            return original_archive(session)

        monkeypatch.setattr(ct, "archive_session", spy_archive)

        # Sequence: pick project, archive session, then session list
        # is empty so the picker bounces back to projects — third call
        # quits.
        call_log = []

        def fake_select(entries, actions=None, **kwargs):
            call_log.append(actions)
            if len(call_log) == 1:
                return entries[0], "open"
            if len(call_log) == 2:
                return entries[0], "archive"
            return None

        monkeypatch.setattr(ct, "select_entry", fake_select)

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        # archive_session ran exactly once with our fixture session id.
        assert len(archive_calls) == 1
        # Original JSONL is gone, archived copy exists.
        assert not jsonl.exists()
        archive_dir = fake_home / ".cad" / "archive"
        assert any(archive_dir.glob("*.jsonl"))
        # The `d` action was advertised on the session picker.
        assert call_log[1].get("d") == "archive"


class TestLocalSessionCLI:
    """End-to-end CLI tests. Discovery runs for real against tmp fixtures;
    only ``select_entry`` (the picker) is mocked. Both pickers (project
    and session) go through the same function now."""

    def test_local_html_action_generates_transcript(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)
        monkeypatch.setattr(
            ct,
            "select_entry",
            _make_mock_select_entry(["__first__", ("__first__", "html")]),
        )

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        assert "Loading projects" in result.output
        assert "Generated" in result.output

    def test_local_resume_claude(self, tmp_path, monkeypatch):
        """Default Enter (resume) chdir's to cwd and exec's claude with
        skip-permissions."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _, real_cwd, _ = _set_up_fake_home_with_session(tmp_path, monkeypatch)
        monkeypatch.setattr(
            ct,
            "select_entry",
            _make_mock_select_entry(["__first__", ("__first__", "resume")]),
        )

        exec_calls = []
        monkeypatch.setattr(
            ct.os,
            "execvp",
            lambda file, args: exec_calls.append((file, args, os.getcwd())),
        )

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        assert len(exec_calls) == 1
        file, args, cwd_at_exec = exec_calls[0]
        assert file == "claude"
        assert "--dangerously-skip-permissions" in args
        assert "--resume" in args
        assert args[-1] == "abc-123"
        assert cwd_at_exec == str(real_cwd)

    def test_local_resume_codex(self, tmp_path, monkeypatch):
        """Resume on a codex session execs `codex resume <id>` instead."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        real_cwd = tmp_path / "real-project"
        real_cwd.mkdir()

        codex_dir = fake_home / ".codex" / "sessions" / "2026" / "01" / "01"
        codex_dir.mkdir(parents=True)
        codex_file = codex_dir / "rollout-2026-01-01T00-00-00-codex-uuid.jsonl"
        codex_file.write_text(
            '{"type":"session_meta","payload":'
            f'{{"id":"codex-uuid","cwd":"{real_cwd}"}}}}\n'
        )

        monkeypatch.setattr(
            ct,
            "select_entry",
            _make_mock_select_entry(["__first__", ("__first__", "resume")]),
        )

        exec_calls = []
        monkeypatch.setattr(
            ct.os,
            "execvp",
            lambda file, args: exec_calls.append((file, args)),
        )

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        assert exec_calls == [("codex", ["codex", "resume", "codex-uuid"])]

    def test_local_html_on_codex_shows_unsupported(self, tmp_path, monkeypatch):
        """Pressing h on a codex session prints the not-yet-supported note
        and returns without trying to render."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        codex_dir = fake_home / ".codex" / "sessions" / "2026" / "01" / "01"
        codex_dir.mkdir(parents=True)
        codex_file = codex_dir / "rollout-2026-01-01T00-00-00-codex-uuid.jsonl"
        codex_file.write_text(
            '{"type":"session_meta","payload":'
            '{"id":"codex-uuid","cwd":"/Users/x/Code/foo"}}\n'
        )

        monkeypatch.setattr(
            ct,
            "select_entry",
            _make_mock_select_entry(["__first__", ("__first__", "html")]),
        )

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        assert "not supported for codex" in result.output.lower()

    def test_local_handles_cancelled_project_selection(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)
        monkeypatch.setattr(ct, "select_entry", _make_mock_select_entry([None]))

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0
        assert "No project selected" in result.output

    def test_local_handles_cancelled_session_selection(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)
        monkeypatch.setattr(
            ct,
            "select_entry",
            _make_mock_select_entry(["__first__", None]),
        )

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0
        assert "No session selected" in result.output

    def test_rename_action_saves_override_and_loops(self, tmp_path, monkeypatch):
        """Pressing r prompts for a new title, saves it to the sidecar,
        and re-enters the session picker. Picking 'resume' on the second
        round then triggers the exec."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _, real_cwd, _ = _set_up_fake_home_with_session(tmp_path, monkeypatch)

        # Project pick, then session pick #1 = rename, then session pick #2 = resume
        monkeypatch.setattr(
            ct,
            "select_entry",
            _make_mock_select_entry(
                [
                    "__first__",
                    ("__first__", "rename"),
                    ("__first__", "resume"),
                ]
            ),
        )
        monkeypatch.setattr(ct, "prompt_for_title", lambda default="": "My session")
        monkeypatch.setattr(ct.os, "execvp", lambda *a, **kw: None)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output

        # Sidecar was written
        sess = {"provider": "claude", "session_id": "abc-123"}
        assert ct.get_title_override(sess) == "My session"

    def test_rename_flags_session_as_recently_updated(self, tmp_path, monkeypatch):
        """After rename, the session dict carries _recently_updated=True so
        the picker can green-highlight it on the next render."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)

        call_count = {"n": 0}
        captured = {}

        def fake_select_entry(
            entries, actions=None, back_action=None, initial_selected=0
        ):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Project picker — pick first.
                return entries[0], "open"
            if call_count["n"] == 2:
                # Session picker, first round — trigger rename.
                return entries[0], "rename"
            # Session picker, second round — capture flag and cancel.
            captured["entry"] = entries[0]
            return None

        monkeypatch.setattr(ct, "select_entry", fake_select_entry)
        monkeypatch.setattr(ct, "prompt_for_title", lambda default="": "Fresh title")

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        assert captured["entry"].get("_recently_updated") is True

    def test_new_action_on_project_picker_launches_claude(self, tmp_path, monkeypatch):
        """Pressing n on a project row execs `claude` (no resume) in the
        project's cwd. Same chdir + CAD_CWD_FILE plumbing as resume."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _, real_cwd, _ = _set_up_fake_home_with_session(tmp_path, monkeypatch)

        def fake_select_entry(
            entries, actions=None, back_action=None, initial_selected=0
        ):
            return entries[0], "new"

        monkeypatch.setattr(ct, "select_entry", fake_select_entry)

        exec_calls = []
        monkeypatch.setattr(
            ct.os,
            "execvp",
            lambda file, args: exec_calls.append((file, args, os.getcwd())),
        )

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        assert len(exec_calls) == 1
        file, args, cwd_at_exec = exec_calls[0]
        assert file == "claude"
        # No --resume because this is a fresh session.
        assert "--resume" not in args
        assert "--dangerously-skip-permissions" in args
        assert cwd_at_exec == str(real_cwd)

    def test_auto_pick_when_inside_known_project(self, tmp_path, monkeypatch):
        """If cad is launched from inside a known project's cwd, the
        project picker is skipped — drop straight into that project's
        session list."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _, real_cwd, _ = _set_up_fake_home_with_session(tmp_path, monkeypatch)
        # Pretend the user invoked `cad` from inside the project.
        monkeypatch.setattr(ct, "Path", ct.Path)  # keep Path import
        monkeypatch.chdir(real_cwd)

        call_log = []

        def fake_select_entry(
            entries, actions=None, back_action=None, initial_selected=0
        ):
            call_log.append(
                {
                    "n_entries": len(entries),
                    "back_action": back_action,
                    "actions": actions,
                }
            )
            # First (and only) call should be the session picker.
            return None

        monkeypatch.setattr(ct, "select_entry", fake_select_entry)

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        # No project picker call — only the session picker (which has
        # back_action="back").
        assert len(call_log) == 1
        assert call_log[0]["back_action"] == "back"
        assert "Auto-opening project" in result.output

    def test_back_action_returns_to_project_picker(self, tmp_path, monkeypatch):
        """Esc/Bksp on the session picker routes back to the project
        picker (outer loop) instead of quitting cct."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)

        call_count = {"n": 0}

        def fake_select_entry(
            entries, actions=None, back_action=None, initial_selected=0
        ):
            call_count["n"] += 1
            # 1: project picker → open first; 2: session picker → back;
            # 3: project picker re-entered → cancel (quit).
            if call_count["n"] == 1:
                assert back_action is None, "project picker shouldn't allow back"
                return entries[0], "open"
            if call_count["n"] == 2:
                assert back_action == "back", "session picker should allow back"
                return (None, "back")
            return None

        monkeypatch.setattr(ct, "select_entry", fake_select_entry)

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0
        assert call_count["n"] == 3, call_count
        assert "No project selected" in result.output

    def test_project_rename_full_migration(self, tmp_path, monkeypatch):
        """The full project rename: cad does fs mv + claude state move +
        cwd rewrite + clears sidecar overrides after success."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        # Simulate a real project on the user's disk and its matching
        # claude state under fake_home/.claude/projects.
        old_user_dir = tmp_path / "Code" / "old-project"
        old_user_dir.mkdir(parents=True)
        new_user_dir = tmp_path / "Code" / "renamed-project"
        # Important: new_user_dir does NOT exist yet — cad will create it
        # via the mv. We just make sure its parent exists.
        assert not new_user_dir.exists()

        old_enc = ct._claude_encode_path(old_user_dir)
        new_enc = ct._claude_encode_path(new_user_dir)
        claude_proj = fake_home / ".claude" / "projects" / old_enc
        claude_proj.mkdir(parents=True)
        sid = "abc-123"
        (claude_proj / f"{sid}.jsonl").write_text(
            '{"type":"summary","summary":"x"}\n'
            f'{{"type":"user","cwd":"{old_user_dir}","message":{{"content":"hi"}}}}\n'
        )

        call_count = {"n": 0}

        def fake_select_entry(
            entries, actions=None, back_action=None, initial_selected=0
        ):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return entries[0], "rename"
            return None

        monkeypatch.setattr(ct, "select_entry", fake_select_entry)
        monkeypatch.setattr(
            ct,
            "prompt_for_cwd",
            lambda default="", must_exist=True, label="New cwd": str(new_user_dir),
        )
        monkeypatch.setattr(ct, "prompt_confirm", lambda m, default=False: True)

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output

        # User-side dir was moved
        assert not old_user_dir.exists()
        assert new_user_dir.exists()

        # claude state dir was moved
        assert not (fake_home / ".claude" / "projects" / old_enc).exists()
        new_claude_dir = fake_home / ".claude" / "projects" / new_enc
        assert new_claude_dir.exists()

        # cwd inside the JSONL was rewritten
        jsonl_content = (new_claude_dir / f"{sid}.jsonl").read_text()
        assert str(new_user_dir) in jsonl_content
        assert str(old_user_dir) not in jsonl_content

        # Claude sidecar override was cleared (source-of-truth is the JSONL now)
        assert ct.get_cwd_override("claude", sid) is None

        # Backup exists under ~/.cad/agent-backups/
        backups = fake_home / ".cad" / "agent-backups"
        assert backups.exists()
        assert any(backups.iterdir()), "expected at least one backup dir"

    def test_project_rename_aborts_when_user_declines_confirmation(
        self, tmp_path, monkeypatch
    ):
        """If the user types N at the confirm, nothing on disk is touched."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        old_user_dir = tmp_path / "Code" / "old-project"
        old_user_dir.mkdir(parents=True)
        new_user_dir = tmp_path / "Code" / "renamed-project"

        old_enc = ct._claude_encode_path(old_user_dir)
        claude_proj = fake_home / ".claude" / "projects" / old_enc
        claude_proj.mkdir(parents=True)
        (claude_proj / "abc-123.jsonl").write_text(
            '{"type":"summary","summary":"x"}\n'
            f'{{"type":"user","cwd":"{old_user_dir}",'
            '"message":{"content":"hi"}}\n'
        )

        call_count = {"n": 0}

        def fake_select_entry(
            entries, actions=None, back_action=None, initial_selected=0
        ):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return entries[0], "rename"
            return None

        monkeypatch.setattr(ct, "select_entry", fake_select_entry)
        monkeypatch.setattr(
            ct,
            "prompt_for_cwd",
            lambda default="", must_exist=True, label="New cwd": str(new_user_dir),
        )
        monkeypatch.setattr(ct, "prompt_confirm", lambda m, default=False: False)

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        assert "Cancelled" in result.output
        # Nothing moved
        assert old_user_dir.exists()
        assert not new_user_dir.exists()
        assert (fake_home / ".claude" / "projects" / old_enc).exists()

    def test_move_action_saves_cwd_override(self, tmp_path, monkeypatch):
        """Pressing m, entering a valid path, saves the cwd override and
        marks the row as recently updated."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)
        target = tmp_path / "new-target"
        target.mkdir()

        call_count = {"n": 0}

        def fake_select_entry(
            entries, actions=None, back_action=None, initial_selected=0
        ):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return entries[0], "open"  # project picker
            if call_count["n"] == 2:
                return entries[0], "move"  # session picker, trigger move
            return None  # session picker round 2 — cancel

        monkeypatch.setattr(ct, "select_entry", fake_select_entry)
        monkeypatch.setattr(ct, "prompt_for_cwd", lambda default="": str(target))

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output

        assert ct.get_cwd_override("claude", "abc-123") == str(target.resolve())

    def test_summarize_action_calls_llm_and_loops(self, tmp_path, monkeypatch):
        """Pressing s runs summarize_session, saves its return as the
        sidecar title, and loops back to the picker."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _, real_cwd, _ = _set_up_fake_home_with_session(tmp_path, monkeypatch)

        monkeypatch.setattr(
            ct,
            "select_entry",
            _make_mock_select_entry(
                [
                    "__first__",
                    ("__first__", "summarize"),
                    ("__first__", "resume"),
                ]
            ),
        )
        monkeypatch.setattr(ct, "summarize_session", lambda session: "Generated title")
        monkeypatch.setattr(ct.os, "execvp", lambda *a, **kw: None)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output

        sess = {"provider": "claude", "session_id": "abc-123"}
        assert ct.get_title_override(sess) == "Generated title"

    def test_both_pickers_use_select_entry(self, tmp_path, monkeypatch):
        """Both pickers now route through the same select_entry function,
        which guarantees identical search-via-/ UX. Asserts that
        select_entry is called twice (project step + session step)."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)

        call_log = []

        def fake_select_entry(
            entries, actions=None, back_action=None, initial_selected=0
        ):
            call_log.append({"actions": actions, "n_entries": len(entries)})
            return entries[0], list(actions.keys())[0] if actions else "select"

        monkeypatch.setattr(ct, "select_entry", fake_select_entry)

        # Need to also intercept exec so the resume action doesn't try to
        # spawn claude during the test.
        monkeypatch.setattr(ct.os, "execvp", lambda *a, **kw: None)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0, result.output
        assert len(call_log) == 2, call_log
        # First call is the project picker — has the single "open" action.
        assert "enter" in call_log[0]["actions"]
        # Second call is the session picker — has at least resume + html.
        assert "h" in call_log[1]["actions"]
