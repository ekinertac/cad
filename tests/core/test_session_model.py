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
    FIXTURE_DIR,
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


class TestParseSessionFile:
    """Tests for parse_session_file which abstracts both JSON and JSONL formats."""

    def test_parses_json_format(self):
        """Test that standard JSON format is parsed correctly."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        result = parse_session_file(fixture_path)

        assert "loglines" in result
        assert len(result["loglines"]) > 0
        # Check first entry
        first = result["loglines"][0]
        assert first["type"] == "user"
        assert "timestamp" in first
        assert "message" in first

    def test_parses_jsonl_format(self):
        """Test that JSONL format is parsed and converted to standard format."""
        fixture_path = FIXTURE_DIR / "sample_session.jsonl"
        result = parse_session_file(fixture_path)

        assert "loglines" in result
        assert len(result["loglines"]) > 0
        # Check structure matches JSON format
        for entry in result["loglines"]:
            assert "type" in entry
            # Skip summary entries which don't have message
            if entry["type"] in ("user", "assistant"):
                assert "timestamp" in entry
                assert "message" in entry

    def test_jsonl_skips_non_message_entries(self):
        """Test that summary and file-history-snapshot entries are skipped."""
        fixture_path = FIXTURE_DIR / "sample_session.jsonl"
        result = parse_session_file(fixture_path)

        # None of the loglines should be summary or file-history-snapshot
        for entry in result["loglines"]:
            assert entry["type"] in ("user", "assistant")

    def test_jsonl_preserves_message_content(self):
        """Test that message content is preserved correctly."""
        fixture_path = FIXTURE_DIR / "sample_session.jsonl"
        result = parse_session_file(fixture_path)

        # Find the first user message
        user_msg = next(e for e in result["loglines"] if e["type"] == "user")
        assert user_msg["message"]["content"] == "Create a hello world function"

    def test_jsonl_generates_html(self, output_dir, snapshot_html):
        """Test that JSONL files can be converted to HTML."""
        fixture_path = FIXTURE_DIR / "sample_session.jsonl"
        generate_html(fixture_path, output_dir)

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert "hello world" in index_html.lower()
        assert index_html == snapshot_html


class TestGetSessionSummary:
    """Tests for get_session_summary which extracts summary from session files."""

    def test_gets_summary_from_jsonl(self):
        """Test extracting summary from JSONL file."""
        fixture_path = FIXTURE_DIR / "sample_session.jsonl"
        summary = get_session_summary(fixture_path)
        assert summary == "Test session for JSONL parsing"

    def test_gets_first_user_message_if_no_summary(self, tmp_path):
        """Test falling back to first user message when no summary entry."""
        jsonl_file = tmp_path / "test.jsonl"
        jsonl_file.write_text(
            '{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"Hello world test"}}\n'
        )
        summary = get_session_summary(jsonl_file)
        assert summary == "Hello world test"

    def test_returns_no_summary_for_empty_file(self, tmp_path):
        """Test handling empty or invalid files."""
        jsonl_file = tmp_path / "empty.jsonl"
        jsonl_file.write_text("", encoding="utf-8")
        summary = get_session_summary(jsonl_file)
        assert summary == "(no summary)"

    def test_truncates_long_summaries(self, tmp_path):
        """Test that long summaries are truncated."""
        jsonl_file = tmp_path / "long.jsonl"
        long_text = "x" * 300
        jsonl_file.write_text(f'{{"type":"summary","summary":"{long_text}"}}\n')
        summary = get_session_summary(jsonl_file, max_length=100)
        assert len(summary) <= 100
        assert summary.endswith("...")


class TestGetSessionCwd:
    """Tests for the JSONL cwd extractor used by the resume action."""

    def test_returns_first_event_cwd(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text(
            '{"type":"summary","summary":"x"}\n'
            '{"type":"user","cwd":"/Users/x/Code/foo","message":{"content":"hi"}}\n'
            '{"type":"assistant","cwd":"/Users/x/Code/foo","message":{"content":"hello"}}\n'
        )
        assert get_session_cwd(f) == "/Users/x/Code/foo"

    def test_skips_lines_without_cwd(self, tmp_path):
        """Summary/metadata lines often lack cwd; the helper must keep
        scanning until it finds one."""
        f = tmp_path / "s.jsonl"
        f.write_text(
            '{"type":"summary","summary":"x"}\n'
            '{"type":"last-prompt","leafUuid":"abc"}\n'
            '{"type":"user","cwd":"/the/right/dir","message":{"content":"hi"}}\n'
        )
        assert get_session_cwd(f) == "/the/right/dir"

    def test_returns_none_when_no_cwd(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text('{"type":"summary","summary":"x"}\n')
        assert get_session_cwd(f) is None

    def test_tolerates_malformed_lines(self, tmp_path):
        """A bad JSON line shouldn't blow up the scan."""
        f = tmp_path / "s.jsonl"
        f.write_text(
            "not-json\n"
            '{"type":"user","cwd":"/recovered","message":{"content":"hi"}}\n'
        )
        assert get_session_cwd(f) == "/recovered"


class TestClaudeSessionMetadata:
    """Single-pass extractor that pulls both the first-prompt summary and
    the most recent /rename name from a claude JSONL."""

    def test_no_custom_title_returns_none_name(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text(
            '{"type":"summary","summary":"Some prompt"}\n'
            '{"type":"user","message":{"content":"hi"}}\n'
        )
        meta = get_claude_session_metadata(f)
        assert meta["name"] is None
        assert meta["summary"] == "Some prompt"

    def test_extracts_custom_title(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text(
            '{"type":"summary","summary":"prompt"}\n'
            '{"type":"user","message":{"content":"hi"}}\n'
            '{"type":"custom-title","customTitle":"MyName"}\n'
        )
        assert get_claude_session_metadata(f)["name"] == "MyName"

    def test_keeps_last_custom_title_when_renamed_multiple_times(self, tmp_path):
        """Claude lets you /rename more than once; the resume picker uses
        whichever name was set last."""
        f = tmp_path / "s.jsonl"
        f.write_text(
            '{"type":"summary","summary":"prompt"}\n'
            '{"type":"custom-title","customTitle":"First"}\n'
            '{"type":"user","message":{"content":"hi"}}\n'
            '{"type":"custom-title","customTitle":"Second"}\n'
            '{"type":"custom-title","customTitle":"Third"}\n'
        )
        assert get_claude_session_metadata(f)["name"] == "Third"

    def test_named_sessions_show_name_in_display(self, tmp_path):
        """The picker row should surface the user-given name as
        provider/Name — prompt instead of provider/ prompt."""
        f = tmp_path / "s.jsonl"
        f.write_text(
            '{"type":"summary","summary":"build the thing"}\n'
            '{"type":"custom-title","customTitle":"BuildIt"}\n'
        )
        sess = {
            "provider": "claude",
            "session_id": "x",
            "filepath": f,
            "cwd": "/x",
            "mtime": 1700000000.0,
            "size": 1234,
            "summary": None,
            "name": None,
            "display": None,
        }
        load_session_summary(sess)
        assert sess["name"] == "BuildIt"
        assert "claude/BuildIt — build the thing" in sess["display"]

    def test_unnamed_sessions_show_no_name(self, tmp_path):
        f = tmp_path / "s.jsonl"
        f.write_text('{"type":"summary","summary":"prompt"}\n')
        sess = {
            "provider": "claude",
            "session_id": "x",
            "filepath": f,
            "cwd": "/x",
            "mtime": 1700000000.0,
            "size": 1234,
            "summary": None,
            "name": None,
            "display": None,
        }
        load_session_summary(sess)
        # Old unnamed format: "claude/ <prompt>"
        assert "claude/ prompt" in sess["display"]
        assert "—" not in sess["display"]


class TestGetSessionTranscript:
    """Peek extracts user/assistant pairs from JSONL providers and skips
    tool calls / system meta."""

    def test_claude_extracts_user_and_assistant(self, tmp_path):
        import cad as ct

        f = tmp_path / "s.jsonl"
        f.write_text(
            '{"type":"summary","summary":"x"}\n'
            '{"type":"user","cwd":"/x","message":{"role":"user","content":"hi"}}\n'
            '{"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"text","text":"hello"}]}}\n'
            # Tool calls and tool results should be filtered out.
            '{"type":"user","isMeta":true,"message":{"role":"user","content":"sys"}}\n'
            '{"type":"user","message":{"role":"user","content":"again"}}\n'
        )
        sess = {"provider": "claude", "filepath": f}
        rows = ct.get_session_transcript(sess)
        assert rows == [("user", "hi"), ("assistant", "hello"), ("user", "again")]

    def test_codex_extracts_user_message_and_agent_message(self, tmp_path):
        import cad as ct

        f = tmp_path / "s.jsonl"
        f.write_text(
            '{"type":"session_meta","payload":{"id":"x","cwd":"/x"}}\n'
            '{"type":"event_msg","payload":{"type":"user_message","message":"hi"}}\n'
            '{"type":"event_msg","payload":{"type":"agent_message","message":"hello"}}\n'
            '{"type":"event_msg","payload":{"type":"token_count","tokens":1}}\n'
        )
        sess = {"provider": "codex", "filepath": f}
        rows = ct.get_session_transcript(sess)
        assert rows == [("user", "hi"), ("assistant", "hello")]

    def test_returns_empty_for_unsupported_provider(self, tmp_path):
        import cad as ct

        sess = {"provider": "opencode", "filepath": tmp_path / "x.db"}
        assert ct.get_session_transcript(sess) == []
