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


class TestWebCommandRepoFiltering:
    """Tests for the web command repo display and filtering."""

    def test_detect_github_repo_from_session(self):
        """Test that detect_github_repo extracts repo from session loglines."""
        from cad import detect_github_repo

        loglines = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "remote: Create a pull request for 'my-branch' on GitHub by visiting:\nremote:      https://github.com/simonw/datasette/pull/new/my-branch",
                        }
                    ],
                },
            }
        ]
        repo = detect_github_repo(loglines)
        assert repo == "simonw/datasette"

    def test_detect_github_repo_returns_none_when_not_found(self):
        """Test that detect_github_repo returns None when no repo found."""
        from cad import detect_github_repo

        loglines = [
            {
                "type": "user",
                "message": {"role": "user", "content": "Hello"},
            }
        ]
        repo = detect_github_repo(loglines)
        assert repo is None

    def test_enrich_sessions_with_repos(self):
        """Test enriching sessions with repo information from session metadata."""
        from cad import enrich_sessions_with_repos

        # Mock sessions from the API list with session_context
        sessions = [
            {
                "id": "sess1",
                "title": "Session 1",
                "created_at": "2025-01-01T10:00:00Z",
                "session_context": {
                    "outcomes": [
                        {
                            "type": "git_repository",
                            "git_info": {"repo": "simonw/datasette", "type": "github"},
                        }
                    ]
                },
            },
            {
                "id": "sess2",
                "title": "Session 2",
                "created_at": "2025-01-02T10:00:00Z",
                "session_context": {},
            },
        ]

        enriched = enrich_sessions_with_repos(sessions)

        assert enriched[0]["repo"] == "simonw/datasette"
        assert enriched[1]["repo"] is None

    def test_extract_repo_from_session_outcomes(self):
        """Test extracting repo from session_context.outcomes."""
        from cad import extract_repo_from_session

        session = {
            "session_context": {
                "outcomes": [
                    {
                        "type": "git_repository",
                        "git_info": {"repo": "simonw/llm", "type": "github"},
                    }
                ]
            }
        }
        assert extract_repo_from_session(session) == "simonw/llm"

    def test_extract_repo_from_session_sources_url(self):
        """Test extracting repo from session_context.sources URL."""
        from cad import extract_repo_from_session

        session = {
            "session_context": {
                "sources": [
                    {
                        "type": "git_repository",
                        "url": "https://github.com/simonw/datasette",
                    }
                ]
            }
        }
        assert extract_repo_from_session(session) == "simonw/datasette"

    def test_extract_repo_from_session_no_context(self):
        """Test extracting repo when no session_context exists."""
        from cad import extract_repo_from_session

        session = {"id": "sess1", "title": "No context"}
        assert extract_repo_from_session(session) is None

    def test_filter_sessions_by_repo(self):
        """Test filtering sessions by repo."""
        from cad import filter_sessions_by_repo

        sessions = [
            {"id": "sess1", "title": "Session 1", "repo": "simonw/datasette"},
            {"id": "sess2", "title": "Session 2", "repo": "simonw/llm"},
            {"id": "sess3", "title": "Session 3", "repo": None},
        ]

        filtered = filter_sessions_by_repo(sessions, "simonw/datasette")
        assert len(filtered) == 1
        assert filtered[0]["id"] == "sess1"

    def test_filter_sessions_by_repo_none_returns_all(self):
        """Test that filtering with None repo returns all sessions."""
        from cad import filter_sessions_by_repo

        sessions = [
            {"id": "sess1", "title": "Session 1", "repo": "simonw/datasette"},
            {"id": "sess2", "title": "Session 2", "repo": None},
        ]

        filtered = filter_sessions_by_repo(sessions, None)
        assert len(filtered) == 2

    def test_format_session_for_display_with_repo(self):
        """Test formatting session display with repo first."""
        from cad import format_session_for_display

        session = {
            "id": "sess1",
            "title": "Fix the bug",
            "created_at": "2025-01-15T10:30:00.000Z",
            "repo": "simonw/datasette",
        }

        display = format_session_for_display(session)
        # Repo should appear first
        assert display.startswith("simonw/datasette")
        assert "2025-01-15T10:30:00" in display
        assert "Fix the bug" in display

    def test_format_session_for_display_without_repo(self):
        """Test formatting session display without repo."""
        from cad import format_session_for_display

        session = {
            "id": "sess1",
            "title": "Fix the bug",
            "created_at": "2025-01-15T10:30:00.000Z",
            "repo": None,
        }

        display = format_session_for_display(session)
        # Should show (no repo) placeholder
        assert "(no repo)" in display
        assert "Fix the bug" in display
