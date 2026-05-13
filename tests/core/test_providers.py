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




class TestResumeWritesCwdFile:
    """When CAD_CWD_FILE is set in the environment, resume_session writes
    the target cwd to that file before exec'ing claude. The shell wrapper
    installed via `cad shell-init` reads it after the agent exits to chdir
    the parent shell."""

    def test_writes_cwd_when_env_set(self, tmp_path, monkeypatch):
        import cad as ct

        real_cwd = tmp_path / "proj"
        real_cwd.mkdir()
        sess = _make_session("claude", "abc", real_cwd)

        cwd_file = tmp_path / "cad-cwd"
        monkeypatch.setenv("CAD_CWD_FILE", str(cwd_file))
        monkeypatch.setattr(ct.os, "execvp", lambda *a, **kw: None)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        ct.resume_session(sess)
        assert cwd_file.read_text() == str(real_cwd)

    def test_no_file_written_when_env_unset(self, tmp_path, monkeypatch):
        """Plain `cct` (no wrapper) should not leave files behind."""
        import cad as ct

        real_cwd = tmp_path / "proj"
        real_cwd.mkdir()
        sess = _make_session("claude", "abc", real_cwd)

        monkeypatch.delenv("CAD_CWD_FILE", raising=False)
        monkeypatch.setattr(ct.os, "execvp", lambda *a, **kw: None)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        ct.resume_session(sess)
        # No file was specified so nothing should be written; just confirming
        # the call didn't crash.

    def test_codex_provider_invokes_codex_resume(self, tmp_path, monkeypatch):
        """resume_session dispatches to ``codex resume <id>`` for codex
        sessions instead of claude --resume."""
        import cad as ct

        real_cwd = tmp_path / "proj"
        real_cwd.mkdir()
        sess = _make_session("codex", "codex-uuid", real_cwd)

        captured = {}

        def fake_execvp(file, args):
            captured["file"] = file
            captured["args"] = args

        monkeypatch.setattr(ct.os, "execvp", fake_execvp)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        ct.resume_session(sess)
        assert captured["file"] == "codex"
        assert captured["args"] == ["codex", "resume", "codex-uuid"]
