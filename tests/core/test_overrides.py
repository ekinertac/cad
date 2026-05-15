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


class TestAtomicSidecarWrites:
    """save_*_override goes through _atomic_write_json: previous file is
    copied to <path>.bak, new content is staged to <path>.tmp then
    os.replace'd onto <path>. Crash-safe and gives the user a one-step
    undo path."""

    def test_creates_bak_on_second_write(self, tmp_path, monkeypatch):
        """The first write has nothing to back up. The second write copies
        the existing file to .bak before overwriting."""
        import cad as ct
        import json

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        ct.save_title_override("claude", "abc", "first-title")
        bak = ct._titles_file().with_suffix(".json.bak")
        assert not bak.exists(), "no .bak on first write — nothing to back up"

        ct.save_title_override("claude", "abc", "second-title")
        assert bak.exists(), ".bak should appear before the second write"
        # The .bak contains the FIRST title, not the second.
        saved = json.loads(bak.read_text())
        assert saved == {"claude:abc": "first-title"}

    def test_no_tmp_file_left_behind(self, tmp_path, monkeypatch):
        """os.replace consumes the temp; no .tmp should be left."""
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ct.save_title_override("claude", "abc", "x")
        ct.save_title_override("claude", "abc", "y")
        tmp = ct._titles_file().with_suffix(".json.tmp")
        assert not tmp.exists()


class TestCwdOverrideSidecar:
    """Cwd overrides live at ~/.cad/cwd-overrides.json. Discovery swaps
    them in before grouping, so a moved session lands in the new project.
    Agent files are never touched."""

    def test_save_and_read_override(self, tmp_path, monkeypatch):
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        target = tmp_path / "new-project"
        target.mkdir()
        ct.save_cwd_override("claude", "abc-123", str(target))
        # Stored as resolved absolute path
        assert ct.get_cwd_override("claude", "abc-123") == str(target.resolve())

    def test_empty_cwd_clears_override(self, tmp_path, monkeypatch):
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        target = tmp_path / "x"
        target.mkdir()
        ct.save_cwd_override("claude", "abc", str(target))
        ct.save_cwd_override("claude", "abc", "")
        assert ct.get_cwd_override("claude", "abc") is None

    def test_override_moves_session_to_new_project(self, tmp_path, monkeypatch):
        """A claude session whose JSONL records cwd=A but has a sidecar
        override pointing at B groups under B."""
        import cad as ct

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        proj_dir = fake_home / ".claude" / "projects" / "test-project"
        proj_dir.mkdir(parents=True)
        sess_file = proj_dir / "abc-123.jsonl"
        sess_file.write_text(
            '{"type":"summary","summary":"x"}\n'
            '{"type":"user","cwd":"/Users/x/Code/old",'
            '"message":{"content":"hi"}}\n'
        )

        new_cwd = tmp_path / "new-project"
        new_cwd.mkdir()
        ct.save_cwd_override("claude", "abc-123", str(new_cwd))

        projects = ct.find_local_projects(fake_home / ".claude" / "projects")
        assert len(projects) == 1
        assert projects[0]["cwd"] == str(new_cwd.resolve())


class TestTitleOverrideSidecar:
    """Sidecar lives at ~/.cad/titles.json, keyed by '<provider>:<id>'."""

    def test_save_and_read_override(self, tmp_path, monkeypatch):
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ct.save_title_override("claude", "abc-123", "My title")
        sess = {"provider": "claude", "session_id": "abc-123"}
        assert ct.get_title_override(sess) == "My title"

    def test_empty_title_removes_override(self, tmp_path, monkeypatch):
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ct.save_title_override("claude", "abc", "First")
        ct.save_title_override("claude", "abc", "")
        sess = {"provider": "claude", "session_id": "abc"}
        assert ct.get_title_override(sess) is None

    def test_override_wins_over_native_summary(self, tmp_path, monkeypatch):
        """load_session_summary picks the override even when a provider
        summary is already set (opencode/forge case)."""
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ct.save_title_override("opencode", "ses_abc", "Custom name")

        sess = {
            "provider": "opencode",
            "session_id": "ses_abc",
            "filepath": Path("/fake.db"),
            "cwd": "/Users/x",
            "mtime": 1700000000.0,
            "size": 0,
            "summary": "Original DB title",
            "display": None,
        }
        ct.load_session_summary(sess)
        assert sess["summary"] == "Custom name"
        assert "Custom name" in sess["display"]
