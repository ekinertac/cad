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


class TestMigrateClaudeProject:
    """Direct tests for the migration helper: encoding, dir moves, JSONL
    rewrites, backups."""

    def test_encoding_replaces_slash_and_dot(self):
        import cad as ct

        assert (
            ct._claude_encode_path("/Users/x/Code/humbl.ai") == "-Users-x-Code-humbl-ai"
        )
        assert (
            ct._claude_encode_path("/Users/x/Code/claude-code-transcripts")
            == "-Users-x-Code-claude-code-transcripts"
        )

    def test_migration_moves_projects_dir_and_rewrites_cwd(self, tmp_path, monkeypatch):
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        old_cwd = "/Users/x/Code/old"
        new_cwd = "/Users/x/Code/new"
        old_enc = ct._claude_encode_path(old_cwd)
        new_enc = ct._claude_encode_path(new_cwd)

        proj_old = tmp_path / ".claude" / "projects" / old_enc
        proj_old.mkdir(parents=True)
        (proj_old / "s1.jsonl").write_text(
            f'{{"type":"user","cwd":"{old_cwd}","message":{{"content":"hi"}}}}\n'
            f'{{"type":"assistant","cwd":"{old_cwd}","message":{{"content":"hello"}}}}\n'
        )

        result = ct.migrate_claude_project(
            old_cwd, new_cwd, backup_root=tmp_path / "backups"
        )

        proj_new = tmp_path / ".claude" / "projects" / new_enc
        assert proj_new.exists()
        assert not proj_old.exists()
        text = (proj_new / "s1.jsonl").read_text()
        assert f'"cwd":"{new_cwd}"' in text
        assert f'"cwd":"{old_cwd}"' not in text
        assert (tmp_path / "backups").exists()
        assert result["backup_dir"] is not None
        assert len(result["rewritten_files"]) == 1

    def test_migration_also_moves_auxiliary_state_dirs(self, tmp_path, monkeypatch):
        """file-history/, todos/, shell-snapshots/ are moved if they
        exist for the project. Absent ones don't trigger errors."""
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        old_enc = ct._claude_encode_path("/Users/x/Code/old")
        new_enc = ct._claude_encode_path("/Users/x/Code/new")

        # Set up two of the four state dirs only — the migration should
        # move what exists and ignore the rest.
        for base in ("projects", "file-history"):
            d = tmp_path / ".claude" / base / old_enc
            d.mkdir(parents=True)
            (d / "marker.txt").write_text(base)

        ct.migrate_claude_project(
            "/Users/x/Code/old", "/Users/x/Code/new", backup_root=None
        )

        for base in ("projects", "file-history"):
            assert (
                tmp_path / ".claude" / base / new_enc / "marker.txt"
            ).read_text() == base
            assert not (tmp_path / ".claude" / base / old_enc).exists()

    def test_migration_merge_skips_existing_destinations(self, tmp_path, monkeypatch):
        """If a session by the same name already exists at the target,
        skip it instead of clobbering the user's data."""
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        old_enc = ct._claude_encode_path("/Users/x/Code/old")
        new_enc = ct._claude_encode_path("/Users/x/Code/new")

        src = tmp_path / ".claude" / "projects" / old_enc
        src.mkdir(parents=True)
        (src / "shared.jsonl").write_text("OLD")
        dst = tmp_path / ".claude" / "projects" / new_enc
        dst.mkdir(parents=True)
        (dst / "shared.jsonl").write_text("DESTINATION-WINS")

        result = ct.migrate_claude_project(
            "/Users/x/Code/old", "/Users/x/Code/new", backup_root=None
        )

        # Destination's file is untouched (we never clobber)
        assert (dst / "shared.jsonl").read_text() == "DESTINATION-WINS"
        # Skipped item reported
        assert any("shared.jsonl" in s for s in result["skipped"])
