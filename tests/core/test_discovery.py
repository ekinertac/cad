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


class TestFindLocalSessions:
    """Tests for find_local_sessions which discovers local JSONL files."""

    def test_finds_jsonl_files(self, tmp_path):
        """Test finding JSONL files in projects directory."""
        # Create mock .claude/projects structure
        projects_dir = tmp_path / ".claude" / "projects" / "test-project"
        projects_dir.mkdir(parents=True)

        # Create a session file
        session_file = projects_dir / "session-123.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Test session"}\n'
            '{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"Hello"}}\n'
        )

        results = find_local_sessions(tmp_path / ".claude" / "projects", limit=10)
        assert len(results) == 1
        assert results[0][0] == session_file
        assert results[0][1] == "Test session"

    def test_excludes_agent_files(self, tmp_path):
        """Test that agent- prefixed files are excluded."""
        projects_dir = tmp_path / ".claude" / "projects" / "test-project"
        projects_dir.mkdir(parents=True)

        # Create agent file (should be excluded)
        agent_file = projects_dir / "agent-123.jsonl"
        agent_file.write_text('{"type":"user","message":{"content":"test"}}\n')

        # Create regular file (should be included)
        session_file = projects_dir / "session-123.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Real session"}\n'
            '{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"Hello"}}\n'
        )

        results = find_local_sessions(tmp_path / ".claude" / "projects", limit=10)
        assert len(results) == 1
        assert "agent-" not in results[0][0].name

    def test_excludes_warmup_sessions(self, tmp_path):
        """Test that warmup sessions are excluded."""
        projects_dir = tmp_path / ".claude" / "projects" / "test-project"
        projects_dir.mkdir(parents=True)

        # Create warmup file (should be excluded)
        warmup_file = projects_dir / "warmup-session.jsonl"
        warmup_file.write_text('{"type":"summary","summary":"warmup"}\n')

        # Create regular file
        session_file = projects_dir / "session-123.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Real session"}\n'
            '{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"Hello"}}\n'
        )

        results = find_local_sessions(tmp_path / ".claude" / "projects", limit=10)
        assert len(results) == 1
        assert results[0][1] == "Real session"

    def test_sorts_by_modification_time(self, tmp_path):
        """Test that results are sorted by modification time, newest first."""
        import time

        projects_dir = tmp_path / ".claude" / "projects" / "test-project"
        projects_dir.mkdir(parents=True)

        # Create files with different mtimes
        file1 = projects_dir / "older.jsonl"
        file1.write_text(
            '{"type":"summary","summary":"Older"}\n{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"test"}}\n'
        )

        time.sleep(0.1)  # Ensure different mtime

        file2 = projects_dir / "newer.jsonl"
        file2.write_text(
            '{"type":"summary","summary":"Newer"}\n{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"test"}}\n'
        )

        results = find_local_sessions(tmp_path / ".claude" / "projects", limit=10)
        assert len(results) == 2
        assert results[0][1] == "Newer"  # Most recent first
        assert results[1][1] == "Older"

    def test_respects_limit(self, tmp_path):
        """Test that limit parameter is respected."""
        projects_dir = tmp_path / ".claude" / "projects" / "test-project"
        projects_dir.mkdir(parents=True)

        # Create 5 files
        for i in range(5):
            f = projects_dir / f"session-{i}.jsonl"
            f.write_text(
                f'{{"type":"summary","summary":"Session {i}"}}\n{{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{{"role":"user","content":"test"}}}}\n'
            )

        results = find_local_sessions(tmp_path / ".claude" / "projects", limit=3)
        assert len(results) == 3

    def test_limit_none_returns_all(self, tmp_path):
        """limit=None means no cap — every session is returned."""
        projects_dir = tmp_path / ".claude" / "projects" / "test-project"
        projects_dir.mkdir(parents=True)

        for i in range(15):
            f = projects_dir / f"session-{i}.jsonl"
            f.write_text(
                f'{{"type":"summary","summary":"Session {i}"}}\n{{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{{"role":"user","content":"test"}}}}\n'
            )

        results = find_local_sessions(tmp_path / ".claude" / "projects", limit=None)
        assert len(results) == 15


class TestQueueOperationFilter:
    """Programmatic `claude -p` calls (from hooks etc.) produce JSONLs
    with `queue-operation` events. Claude's own `--resume` picker hides
    them; cad should too — otherwise the user sees phantom rows they
    didn't create."""

    def test_queue_operation_session_is_filtered(self, tmp_path, monkeypatch):
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects" / "-Users-x-Code-foo"
        projects_dir.mkdir(parents=True)
        # Programmatic session: contains a queue-operation event.
        (projects_dir / "qop.jsonl").write_text(
            '{"type":"summary","summary":"x"}\n'
            '{"type":"user","cwd":"/Users/x/Code/foo",'
            '"message":{"content":"hi"}}\n'
            '{"type":"queue-operation","payload":{}}\n'
        )
        # Real interactive session in same folder.
        (projects_dir / "real.jsonl").write_text(
            '{"type":"summary","summary":"x"}\n'
            '{"type":"user","cwd":"/Users/x/Code/foo",'
            '"message":{"content":"hello"}}\n'
        )

        sessions = ct.find_claude_sessions(tmp_path / ".claude" / "projects")
        ids = sorted(s["session_id"] for s in sessions)
        assert ids == ["real"], f"queue-operation session should be hidden: {ids}"

    def test_queue_operation_check_is_bounded(self, tmp_path):
        """The scan caps at ~50 lines so an interactive multi-MB session
        doesn't trigger a full-file read just to confirm it's clean."""
        import cad as ct

        f = tmp_path / "big.jsonl"
        # 100 lines of regular events; a queue-operation tucked at the end.
        # If the scan is bounded, it won't reach the queue-operation and the
        # session won't be hidden — that's the intended behaviour (interactive
        # sessions with rare late events stay visible).
        lines = ['{"type":"user","message":{"content":"x"}}\n'] * 100
        lines.append('{"type":"queue-operation","payload":{}}\n')
        f.write_text("".join(lines))
        assert ct._is_claude_queue_operation_session(f, scan_lines=50) is False


class TestFindPiSessions:
    """Pi sessions live at ~/.pi/agent/sessions/<encoded>/<ts>_<uuid>.jsonl
    with a session-meta line at the top."""

    def test_returns_empty_when_root_missing(self, tmp_path):
        assert find_pi_sessions(tmp_path / "no-such") == []

    def test_discovers_sessions_with_cwd(self, tmp_path):
        root = tmp_path / "sessions"
        proj = root / "--Users-x-Code-foo--"
        proj.mkdir(parents=True)
        f = proj / "2026-01-01T00-00-00-000Z_abc-123.jsonl"
        f.write_text('{"type":"session","id":"abc-123","cwd":"/Users/x/Code/foo"}\n')
        results = find_pi_sessions(root)
        assert len(results) == 1
        assert results[0]["provider"] == "pi"
        assert results[0]["session_id"] == "abc-123"
        assert results[0]["cwd"] == "/Users/x/Code/foo"

    def test_skips_sessions_missing_id_or_cwd(self, tmp_path):
        root = tmp_path / "sessions"
        proj = root / "--x--"
        proj.mkdir(parents=True)
        (proj / "no-cwd.jsonl").write_text('{"type":"session","id":"x"}\n')
        (proj / "no-id.jsonl").write_text('{"type":"session","cwd":"/some/dir"}\n')
        assert find_pi_sessions(root) == []


class TestFindOpencodeSessions:
    """opencode keeps sessions in a SQLite DB. Discovery reads `directory`
    (cwd), `title` (summary), and `time_updated`."""

    def _make_db(self, path):
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE session(
                id TEXT, project_id TEXT, parent_id TEXT, slug TEXT,
                directory TEXT, title TEXT, version TEXT,
                share_url TEXT,
                summary_additions INTEGER, summary_deletions INTEGER,
                summary_files INTEGER, summary_diffs TEXT, revert TEXT,
                permission TEXT,
                time_created INTEGER, time_updated INTEGER,
                time_compacting INTEGER, time_archived INTEGER,
                workspace_id TEXT, path TEXT, agent TEXT, model TEXT
            );
            """
        )
        return conn

    def test_returns_empty_when_db_missing(self, tmp_path):
        assert find_opencode_sessions(tmp_path / "no.db") == []

    def test_discovers_session_with_directory_and_title(self, tmp_path):
        db = tmp_path / "opencode.db"
        conn = self._make_db(db)
        conn.execute(
            "INSERT INTO session(id, slug, directory, title, version, "
            "time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "ses_abc",
                "slug",
                "/Users/x/Code/foo",
                "Some session title",
                "0.1",
                1700000000,
                1700000100,
            ),
        )
        conn.commit()
        conn.close()

        results = find_opencode_sessions(db)
        assert len(results) == 1
        assert results[0]["provider"] == "opencode"
        assert results[0]["session_id"] == "ses_abc"
        assert results[0]["cwd"] == "/Users/x/Code/foo"
        assert results[0]["summary"] == "Some session title"
        # Stored value is epoch ms, converted to seconds for our model
        assert results[0]["mtime"] == 1700000.1


class TestFindForgeSessions:
    """Forge keeps conversations in SQLite. cwd is embedded in the context
    blob inside <current_working_directory> tags — extracted via regex."""

    def _make_db(self, path):
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE conversations(
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                workspace_id BIGINT,
                context TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                metrics TEXT
            );
            """
        )
        return conn

    def test_returns_empty_when_db_missing(self, tmp_path):
        assert find_forge_sessions(tmp_path / "no.db") == []

    def test_discovers_conversation_and_extracts_cwd(self, tmp_path):
        db = tmp_path / "forge.db"
        conn = self._make_db(db)
        ctx = (
            "Here is the system prompt with embedded "
            "<current_working_directory>/Users/x/Code/foo</current_working_directory>"
            " more text follows."
        )
        conn.execute(
            "INSERT INTO conversations VALUES (?, ?, 0, ?, ?, ?, NULL)",
            (
                "conv-123",
                "Conversation title",
                ctx,
                "2026-01-01 10:00:00",
                "2026-05-10 14:00:00",
            ),
        )
        conn.commit()
        conn.close()

        results = find_forge_sessions(db)
        assert len(results) == 1
        assert results[0]["provider"] == "forge"
        assert results[0]["session_id"] == "conv-123"
        assert results[0]["cwd"] == "/Users/x/Code/foo"
        assert results[0]["summary"] == "Conversation title"

    def test_skips_conversation_without_cwd_tag(self, tmp_path):
        db = tmp_path / "forge.db"
        conn = self._make_db(db)
        conn.execute(
            "INSERT INTO conversations VALUES (?, ?, 0, ?, ?, ?, NULL)",
            (
                "conv-no-cwd",
                "x",
                "system prompt without the magic tag",
                "2026-01-01 10:00:00",
                "2026-01-01 10:00:00",
            ),
        )
        conn.commit()
        conn.close()
        assert find_forge_sessions(db) == []
