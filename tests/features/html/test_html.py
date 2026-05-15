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

class TestGenerateHtml:
    """Tests for the main generate_html function."""

    def test_generates_index_html(self, output_dir, snapshot_html):
        """Test index.html generation."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert index_html == snapshot_html

    def test_generates_page_001_html(self, output_dir, snapshot_html):
        """Test page-001.html generation."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        page_html = (output_dir / "page-001.html").read_text(encoding="utf-8")
        assert page_html == snapshot_html

    def test_generates_page_002_html(self, output_dir, snapshot_html):
        """Test page-002.html generation (continuation page)."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        page_html = (output_dir / "page-002.html").read_text(encoding="utf-8")
        assert page_html == snapshot_html

    def test_github_repo_autodetect(self, sample_session):
        """Test GitHub repo auto-detection from git push output."""
        loglines = sample_session["loglines"]
        repo = detect_github_repo(loglines)
        assert repo == "example/project"

    def test_handles_array_content_format(self, tmp_path):
        """Test that user messages with array content format are recognized.

        Claude Code v2.0.76+ uses array content format like:
        {"type": "user", "message": {"content": [{"type": "text", "text": "..."}]}}
        instead of the simpler string format:
        {"type": "user", "message": {"content": "..."}}
        """
        jsonl_file = tmp_path / "session.jsonl"
        jsonl_file.write_text(
            '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Hello from array format"}]}}\n'
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Hi there!"}]}}\n'
        )

        output_dir = tmp_path / "output"
        output_dir.mkdir()

        generate_html(jsonl_file, output_dir)

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")
        # Should have 1 prompt, not 0
        assert "1 prompts" in index_html or "1 prompt" in index_html
        assert "0 prompts" not in index_html
        # The page file should exist
        assert (output_dir / "page-001.html").exists()

class TestRenderFunctions:
    """Tests for individual render functions."""

    def test_render_markdown_text(self, snapshot_html):
        """Test markdown rendering."""
        result = render_markdown_text("**bold** and `code`\n\n- item 1\n- item 2")
        assert result == snapshot_html

    def test_render_markdown_text_empty(self):
        """Test markdown rendering with empty input."""
        assert render_markdown_text("") == ""
        assert render_markdown_text(None) == ""

    def test_format_json(self, snapshot_html):
        """Test JSON formatting."""
        result = format_json({"key": "value", "number": 42, "nested": {"a": 1}})
        assert result == snapshot_html

    def test_is_json_like(self):
        """Test JSON-like string detection."""
        assert is_json_like('{"key": "value"}')
        assert is_json_like("[1, 2, 3]")
        assert not is_json_like("plain text")
        assert not is_json_like("")
        assert not is_json_like(None)

    def test_render_todo_write(self, snapshot_html):
        """Test TodoWrite rendering."""
        tool_input = {
            "todos": [
                {"content": "First task", "status": "completed", "activeForm": "First"},
                {
                    "content": "Second task",
                    "status": "in_progress",
                    "activeForm": "Second",
                },
                {"content": "Third task", "status": "pending", "activeForm": "Third"},
            ]
        }
        result = render_todo_write(tool_input, "tool-123")
        assert result == snapshot_html

    def test_render_todo_write_empty(self):
        """Test TodoWrite with no todos."""
        result = render_todo_write({"todos": []}, "tool-123")
        assert result == ""

    def test_render_write_tool(self, snapshot_html):
        """Test Write tool rendering."""
        tool_input = {
            "file_path": "/project/src/main.py",
            "content": "def hello():\n    print('hello world')\n",
        }
        result = render_write_tool(tool_input, "tool-123")
        assert result == snapshot_html

    def test_render_edit_tool(self, snapshot_html):
        """Test Edit tool rendering."""
        tool_input = {
            "file_path": "/project/file.py",
            "old_string": "old code here",
            "new_string": "new code here",
        }
        result = render_edit_tool(tool_input, "tool-123")
        assert result == snapshot_html

    def test_render_edit_tool_replace_all(self, snapshot_html):
        """Test Edit tool with replace_all flag."""
        tool_input = {
            "file_path": "/project/file.py",
            "old_string": "old",
            "new_string": "new",
            "replace_all": True,
        }
        result = render_edit_tool(tool_input, "tool-123")
        assert result == snapshot_html

    def test_render_bash_tool(self, snapshot_html):
        """Test Bash tool rendering."""
        tool_input = {
            "command": "pytest tests/ -v",
            "description": "Run tests with verbose output",
        }
        result = render_bash_tool(tool_input, "tool-123")
        assert result == snapshot_html

class TestRenderContentBlock:
    """Tests for render_content_block function."""

    def test_image_block(self, snapshot_html):
        """Test image block rendering with base64 data URL."""
        # 200x200 black GIF - minimal valid GIF with black pixels
        # Generated with: from PIL import Image; img = Image.new('RGB', (200, 200), (0, 0, 0)); img.save('black.gif')
        import base64
        import io

        # Create a minimal 200x200 black GIF using raw bytes
        # GIF89a header + logical screen descriptor + global color table + image data
        gif_data = (
            b"GIF89a"  # Header
            b"\xc8\x00\xc8\x00"  # Width 200, Height 200
            b"\x80"  # Global color table flag (1 color: 2^(0+1)=2 colors)
            b"\x00"  # Background color index
            b"\x00"  # Pixel aspect ratio
            b"\x00\x00\x00"  # Color 0: black
            b"\x00\x00\x00"  # Color 1: black (padding)
            b","  # Image separator
            b"\x00\x00\x00\x00"  # Left, Top
            b"\xc8\x00\xc8\x00"  # Width 200, Height 200
            b"\x00"  # No local color table
            b"\x08"  # LZW minimum code size
            b"\x02\x04\x01\x00"  # Compressed data (minimal)
            b";"  # GIF trailer
        )
        black_gif_base64 = base64.b64encode(gif_data).decode("ascii")

        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/gif",
                "data": black_gif_base64,
            },
        }
        result = render_content_block(block)
        # The result should contain an img tag with data URL
        assert 'src="data:image/gif;base64,' in result
        assert "max-width: 100%" in result
        assert result == snapshot_html

    def test_thinking_block(self, snapshot_html):
        """Test thinking block rendering."""
        block = {
            "type": "thinking",
            "thinking": "Let me think about this...\n\n1. First consideration\n2. Second point",
        }
        result = render_content_block(block)
        assert result == snapshot_html

    def test_text_block(self, snapshot_html):
        """Test text block rendering."""
        block = {"type": "text", "text": "Here is my response with **markdown**."}
        result = render_content_block(block)
        assert result == snapshot_html

    def test_tool_result_block(self, snapshot_html):
        """Test tool result rendering."""
        block = {
            "type": "tool_result",
            "content": "Command completed successfully\nOutput line 1\nOutput line 2",
            "is_error": False,
        }
        result = render_content_block(block)
        assert result == snapshot_html

    def test_tool_result_error(self, snapshot_html):
        """Test tool result error rendering."""
        block = {
            "type": "tool_result",
            "content": "Error: file not found\nTraceback follows...",
            "is_error": True,
        }
        result = render_content_block(block)
        assert result == snapshot_html

    def test_tool_result_with_commit(self, snapshot_html):
        """Test tool result with git commit output."""
        # Need to set the global _github_repo for commit link rendering.
        # After the feature-based refactor this lives in the html
        # feature module rather than directly on cad.
        from cad.features import html as html_feature

        old_repo = html_feature._github_repo
        html_feature._github_repo = "example/repo"
        try:
            block = {
                "type": "tool_result",
                "content": "[main abc1234] Add new feature\n 2 files changed, 10 insertions(+)",
                "is_error": False,
            }
            result = render_content_block(block)
            assert result == snapshot_html
        finally:
            html_feature._github_repo = old_repo

    def test_tool_result_with_image(self, snapshot_html):
        """Test tool result containing image blocks in content array.

        This tests the case where a tool (like a screenshot tool) returns
        both text and image content in the same tool_result.
        """
        import base64

        # Create a minimal GIF image
        gif_data = (
            b"GIF89a"  # Header
            b"\xc8\x00\xc8\x00"  # Width 200, Height 200
            b"\x80"  # Global color table flag
            b"\x00"  # Background color index
            b"\x00"  # Pixel aspect ratio
            b"\x00\x00\x00"  # Color 0: black
            b"\x00\x00\x00"  # Color 1: black
            b","  # Image separator
            b"\x00\x00\x00\x00"  # Left, Top
            b"\xc8\x00\xc8\x00"  # Width 200, Height 200
            b"\x00"  # No local color table
            b"\x08"  # LZW minimum code size
            b"\x02\x04\x01\x00"  # Compressed data
            b";"  # GIF trailer
        )
        gif_base64 = base64.b64encode(gif_data).decode("ascii")

        block = {
            "type": "tool_result",
            "content": [
                {
                    "type": "text",
                    "text": "Successfully captured screenshot (807x782, jpeg) - ID: ss_123",
                },
                {
                    "type": "text",
                    "text": "\n\nTab Context:\n- Executed on tabId: 12345",
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/gif",
                        "data": gif_base64,
                    },
                },
            ],
            "is_error": False,
        }
        result = render_content_block(block)

        # The result should contain the text content
        assert "Successfully captured screenshot" in result
        assert "Tab Context" in result

        # The result should contain an img tag with data URL for the image
        assert 'src="data:image/gif;base64,' in result
        assert "max-width: 100%" in result

        # Tool results with images should NOT be truncatable
        assert "truncatable" not in result

        assert result == snapshot_html

class TestAnalyzeConversation:
    """Tests for conversation analysis."""

    def test_counts_tools(self):
        """Test that tool usage is counted."""
        messages = [
            (
                "assistant",
                json.dumps(
                    {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "id": "1",
                                "input": {},
                            },
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "id": "2",
                                "input": {},
                            },
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "id": "3",
                                "input": {},
                            },
                        ]
                    }
                ),
                "2025-01-01T00:00:00Z",
            ),
        ]
        result = analyze_conversation(messages)
        assert result["tool_counts"]["Bash"] == 2
        assert result["tool_counts"]["Write"] == 1

    def test_extracts_commits(self):
        """Test that git commits are extracted."""
        messages = [
            (
                "user",
                json.dumps(
                    {
                        "content": [
                            {
                                "type": "tool_result",
                                "content": "[main abc1234] Add new feature\n 1 file changed",
                            }
                        ]
                    }
                ),
                "2025-01-01T00:00:00Z",
            ),
        ]
        result = analyze_conversation(messages)
        assert len(result["commits"]) == 1
        assert result["commits"][0][0] == "abc1234"
        assert "Add new feature" in result["commits"][0][1]

class TestFormatToolStats:
    """Tests for tool stats formatting."""

    def test_formats_counts(self):
        """Test tool count formatting."""
        counts = {"Bash": 5, "Read": 3, "Write": 1}
        result = format_tool_stats(counts)
        assert "5 bash" in result
        assert "3 read" in result
        assert "1 write" in result

    def test_empty_counts(self):
        """Test empty tool counts."""
        assert format_tool_stats({}) == ""

class TestIsToolResultMessage:
    """Tests for tool result message detection."""

    def test_detects_tool_result_only(self):
        """Test detection of tool-result-only messages."""
        message = {"content": [{"type": "tool_result", "content": "result"}]}
        assert is_tool_result_message(message) is True

    def test_rejects_mixed_content(self):
        """Test rejection of mixed content messages."""
        message = {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_result", "content": "result"},
            ]
        }
        assert is_tool_result_message(message) is False

    def test_rejects_empty(self):
        """Test rejection of empty content."""
        assert is_tool_result_message({"content": []}) is False
        assert is_tool_result_message({"content": "string"}) is False

class TestInjectGistPreviewJs:
    """Tests for the inject_gist_preview_js function."""

    def test_injects_js_into_html_files(self, output_dir):
        """Test that JS is injected before </body> tag."""
        # Create test HTML files
        (output_dir / "index.html").write_text(
            "<html><body><h1>Test</h1></body></html>", encoding="utf-8"
        )
        (output_dir / "page-001.html").write_text(
            "<html><body><p>Page 1</p></body></html>", encoding="utf-8"
        )

        inject_gist_preview_js(output_dir)

        index_content = (output_dir / "index.html").read_text(encoding="utf-8")
        page_content = (output_dir / "page-001.html").read_text(encoding="utf-8")

        # Check JS was injected
        assert GIST_PREVIEW_JS in index_content
        assert GIST_PREVIEW_JS in page_content

        # Check JS is before </body>
        assert index_content.endswith("</body></html>")
        assert "<script>" in index_content

    def test_gist_preview_js_handles_fragment_navigation(self):
        """Test that GIST_PREVIEW_JS includes fragment navigation handling.

        When accessing a gistpreview URL with a fragment like:
        https://gistpreview.github.io/?GIST_ID/page-001.html#msg-2025-12-26T15-30-45-910Z

        The content loads dynamically, so the browser's native fragment
        navigation fails because the element doesn't exist yet. The JS
        should scroll to the fragment element after content loads.
        """
        # The JS should check for fragment in URL
        assert (
            "location.hash" in GIST_PREVIEW_JS
            or "window.location.hash" in GIST_PREVIEW_JS
        )
        # The JS should scroll to the element
        assert "scrollIntoView" in GIST_PREVIEW_JS

    def test_skips_files_without_body(self, output_dir):
        """Test that files without </body> are not modified."""
        original_content = "<html><head><title>Test</title></head></html>"
        (output_dir / "fragment.html").write_text(original_content, encoding="utf-8")

        inject_gist_preview_js(output_dir)

        assert (output_dir / "fragment.html").read_text(
            encoding="utf-8"
        ) == original_content

    def test_handles_empty_directory(self, output_dir):
        """Test that empty directories don't cause errors."""
        inject_gist_preview_js(output_dir)
        # Should complete without error

    def test_gist_preview_js_skips_already_rewritten_links(self):
        """Test that GIST_PREVIEW_JS skips links that have already been rewritten.

        When navigating between pages on gistpreview.github.io, the JS may run
        multiple times. Links that have already been rewritten to the
        ?GIST_ID/filename.html format should be skipped to avoid double-rewriting.

        This fixes issue #26 where pagination links break on later pages.
        """
        # The JS should check if href already starts with '?'
        assert "href.startsWith('?')" in GIST_PREVIEW_JS

    def test_gist_preview_js_uses_mutation_observer(self):
        """Test that GIST_PREVIEW_JS uses MutationObserver for dynamic content.

        gistpreview.github.io loads content dynamically. When navigating between
        pages via SPA-style navigation, new content is inserted without a full
        page reload. The JS needs to use MutationObserver to detect and rewrite
        links in dynamically added content.

        This fixes issue #26 where pagination links break on later pages.
        """
        # The JS should use MutationObserver
        assert "MutationObserver" in GIST_PREVIEW_JS

    def test_gist_preview_js_runs_on_dom_content_loaded(self):
        """Test that GIST_PREVIEW_JS runs on DOMContentLoaded.

        The script is injected at the end of the body, but in some cases
        (especially on gistpreview.github.io), the DOM might not be fully ready
        when the script runs. We should also run on DOMContentLoaded as a fallback.

        This fixes issue #26 where pagination links break on later pages.
        """
        # The JS should listen for DOMContentLoaded
        assert "DOMContentLoaded" in GIST_PREVIEW_JS

class TestCreateGist:
    """Tests for the create_gist function."""

    def test_creates_gist_successfully(self, output_dir, monkeypatch):
        """Test successful gist creation."""
        import subprocess
        import click

        # Create test HTML files
        (output_dir / "index.html").write_text(
            "<html><body>Index</body></html>", encoding="utf-8"
        )
        (output_dir / "page-001.html").write_text(
            "<html><body>Page</body></html>", encoding="utf-8"
        )

        # Mock subprocess.run to simulate successful gh gist create
        mock_result = subprocess.CompletedProcess(
            args=["gh", "gist", "create"],
            returncode=0,
            stdout="https://gist.github.com/testuser/abc123def456\n",
            stderr="",
        )

        def mock_run(*args, **kwargs):
            return mock_result

        monkeypatch.setattr(subprocess, "run", mock_run)

        gist_id, gist_url = create_gist(output_dir)

        assert gist_id == "abc123def456"
        assert gist_url == "https://gist.github.com/testuser/abc123def456"

    def test_raises_on_no_html_files(self, output_dir):
        """Test that error is raised when no HTML files exist."""
        import click

        with pytest.raises(click.ClickException) as exc_info:
            create_gist(output_dir)

        assert "No HTML files found" in str(exc_info.value)

    def test_raises_on_gh_cli_error(self, output_dir, monkeypatch):
        """Test that error is raised when gh CLI fails."""
        import subprocess
        import click

        # Create test HTML file
        (output_dir / "index.html").write_text(
            "<html><body>Test</body></html>", encoding="utf-8"
        )

        # Mock subprocess.run to simulate gh error
        def mock_run(*args, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=["gh", "gist", "create"],
                stderr="error: Not logged in",
            )

        monkeypatch.setattr(subprocess, "run", mock_run)

        with pytest.raises(click.ClickException) as exc_info:
            create_gist(output_dir)

        assert "Failed to create gist" in str(exc_info.value)

    def test_raises_on_gh_not_found(self, output_dir, monkeypatch):
        """Test that error is raised when gh CLI is not installed."""
        import subprocess
        import click

        # Create test HTML file
        (output_dir / "index.html").write_text(
            "<html><body>Test</body></html>", encoding="utf-8"
        )

        # Mock subprocess.run to simulate gh not found
        def mock_run(*args, **kwargs):
            raise FileNotFoundError()

        monkeypatch.setattr(subprocess, "run", mock_run)

        with pytest.raises(click.ClickException) as exc_info:
            create_gist(output_dir)

        assert "gh CLI not found" in str(exc_info.value)

class TestSessionGistOption:
    """Tests for the session command --gist option."""

    def test_session_gist_creates_gist(self, monkeypatch, tmp_path):
        """Test that session --gist creates a gist."""
        from click.testing import CliRunner
        from cad import cli
        import subprocess

        # Create sample session file
        fixture_path = FIXTURE_DIR / "sample_session.json"

        # Mock subprocess.run for gh gist create
        mock_result = subprocess.CompletedProcess(
            args=["gh", "gist", "create"],
            returncode=0,
            stdout="https://gist.github.com/testuser/abc123\n",
            stderr="",
        )

        def mock_run(*args, **kwargs):
            return mock_result

        monkeypatch.setattr(subprocess, "run", mock_run)

        # Mock tempfile.gettempdir to use our tmp_path
        monkeypatch.setattr("cad.tempfile.gettempdir", lambda: str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "--gist"],
        )

        assert result.exit_code == 0
        assert "Creating GitHub gist" in result.output
        assert "gist.github.com" in result.output
        assert "gisthost.github.io" in result.output

    def test_session_gist_with_output_dir(self, monkeypatch, output_dir):
        """Test that session --gist with -o uses specified directory."""
        from click.testing import CliRunner
        from cad import cli
        import subprocess

        fixture_path = FIXTURE_DIR / "sample_session.json"

        # Mock subprocess.run for gh gist create
        mock_result = subprocess.CompletedProcess(
            args=["gh", "gist", "create"],
            returncode=0,
            stdout="https://gist.github.com/testuser/abc123\n",
            stderr="",
        )

        def mock_run(*args, **kwargs):
            return mock_result

        monkeypatch.setattr(subprocess, "run", mock_run)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-o", str(output_dir), "--gist"],
        )

        assert result.exit_code == 0
        assert (output_dir / "index.html").exists()
        # Verify JS was injected (checks for both domains for backwards compatibility)
        index_content = (output_dir / "index.html").read_text(encoding="utf-8")
        assert "gisthost.github.io" in index_content

class TestContinuationLongTexts:
    """Tests for long text extraction from continuation conversations."""

    def test_long_text_in_continuation_appears_in_index(self, output_dir):
        """Test that long texts from continuation conversations appear in index.

        This is a regression test for a bug where conversations marked as
        continuations (isCompactSummary=True) were completely skipped when
        building the index, causing their long_texts to be lost.
        """
        # Create a session with:
        # 1. An initial user prompt
        # 2. Some messages
        # 3. A continuation prompt (isCompactSummary=True)
        # 4. An assistant message with a long text summary (>300 chars)
        session_data = {
            "loglines": [
                # Initial user prompt
                {
                    "type": "user",
                    "timestamp": "2025-01-01T10:00:00.000Z",
                    "message": {
                        "content": "Build a Redis JavaScript module",
                        "role": "user",
                    },
                },
                # Some assistant work
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T10:00:05.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "I'll start working on this."}
                        ],
                    },
                },
                # Continuation prompt (context was summarized)
                {
                    "type": "user",
                    "timestamp": "2025-01-01T11:00:00.000Z",
                    "isCompactSummary": True,
                    "message": {
                        "content": "This session is being continued from a previous conversation...",
                        "role": "user",
                    },
                },
                # More assistant work after continuation
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T11:00:05.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Continuing the work..."}],
                    },
                },
                # Final summary - this is a LONG text (>300 chars) that should appear in index
                {
                    "type": "assistant",
                    "timestamp": "2025-01-01T12:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "All tasks completed successfully. Here's a summary of what was built:\n\n"
                                    "## Redis JavaScript Module\n\n"
                                    "A loadable Redis module providing JavaScript scripting via the mquickjs engine.\n\n"
                                    "### Commands Implemented\n"
                                    "- JS.EVAL - Execute JavaScript with KEYS/ARGV arrays\n"
                                    "- JS.LOAD / JS.CALL - Cache and call scripts by SHA1\n"
                                    "- JS.EXISTS / JS.FLUSH - Manage script cache\n\n"
                                    "All 41 tests pass. Changes pushed to branch."
                                ),
                            }
                        ],
                    },
                },
            ]
        }

        # Write the session to a temp file
        session_file = output_dir / "test_session.json"
        session_file.write_text(json.dumps(session_data), encoding="utf-8")

        # Generate HTML
        generate_html(session_file, output_dir)

        # Read the index.html
        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # The long text summary should appear in the index
        # This is the bug: currently it doesn't because the continuation
        # conversation is skipped entirely
        assert (
            "All tasks completed successfully" in index_html
        ), "Long text from continuation conversation should appear in index"
        assert "Redis JavaScript Module" in index_html

class TestSessionJsonOption:
    """Tests for the session command --json option."""

    def test_session_json_copies_file(self, output_dir):
        """Test that session --json copies the JSON file to output."""
        from click.testing import CliRunner
        from cad import cli

        fixture_path = FIXTURE_DIR / "sample_session.json"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-o", str(output_dir), "--json"],
        )

        assert result.exit_code == 0
        json_file = output_dir / "sample_session.json"
        assert json_file.exists()
        assert "JSON:" in result.output
        assert "KB" in result.output

    def test_session_json_preserves_original_name(self, output_dir):
        """Test that --json preserves the original filename."""
        from click.testing import CliRunner
        from cad import cli

        fixture_path = FIXTURE_DIR / "sample_session.json"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-o", str(output_dir), "--json"],
        )

        assert result.exit_code == 0
        # Should use original filename, not "session.json"
        assert (output_dir / "sample_session.json").exists()
        assert not (output_dir / "session.json").exists()

class TestOpenOption:
    """Tests for the --open option."""

    def test_session_open_calls_webbrowser(self, output_dir, monkeypatch):
        """Test that session --open opens the browser."""
        from click.testing import CliRunner
        from cad import cli

        fixture_path = FIXTURE_DIR / "sample_session.json"

        # Track webbrowser.open calls
        opened_urls = []

        def mock_open(url):
            opened_urls.append(url)
            return True

        monkeypatch.setattr("cad.webbrowser.open", mock_open)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-o", str(output_dir), "--open"],
        )

        assert result.exit_code == 0
        assert len(opened_urls) == 1
        assert "index.html" in opened_urls[0]
        assert opened_urls[0].startswith("file://")


class TestOutputAutoOption:
    """Tests for the -a/--output-auto flag."""

    def test_json_output_auto_creates_subdirectory(self, tmp_path):
        """Test that json -a creates output subdirectory named after file stem."""
        from click.testing import CliRunner
        from cad import cli

        fixture_path = FIXTURE_DIR / "sample_session.json"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-a", "-o", str(tmp_path)],
        )

        assert result.exit_code == 0
        # Output should be in tmp_path/sample_session/
        expected_dir = tmp_path / "sample_session"
        assert expected_dir.exists()
        assert (expected_dir / "index.html").exists()

    def test_json_output_auto_uses_cwd_when_no_output(self, tmp_path, monkeypatch):
        """Test that json -a uses current directory when -o not specified."""
        from click.testing import CliRunner
        from cad import cli
        import os

        fixture_path = FIXTURE_DIR / "sample_session.json"

        # Change to tmp_path
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-a"],
        )

        assert result.exit_code == 0
        # Output should be in ./sample_session/
        expected_dir = tmp_path / "sample_session"
        assert expected_dir.exists()
        assert (expected_dir / "index.html").exists()

    def test_json_output_auto_no_browser_open(self, tmp_path, monkeypatch):
        """Test that json -a does not auto-open browser."""
        from click.testing import CliRunner
        from cad import cli

        fixture_path = FIXTURE_DIR / "sample_session.json"

        # Track webbrowser.open calls
        opened_urls = []

        def mock_open(url):
            opened_urls.append(url)
            return True

        monkeypatch.setattr("cad.webbrowser.open", mock_open)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-a", "-o", str(tmp_path)],
        )

        assert result.exit_code == 0
        assert len(opened_urls) == 0  # No browser opened

    def test_local_output_auto_creates_subdirectory(self, tmp_path, monkeypatch):
        """`local -a` creates an output subdirectory named after the
        chosen session's stem."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        cwd_dir = tmp_path / "real-cwd"
        cwd_dir.mkdir()
        project_dir = fake_home / ".claude" / "projects" / "test-project"
        project_dir.mkdir(parents=True)
        session_file = project_dir / "my-session-file.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Test"}\n'
            f'{{"type":"user","cwd":"{cwd_dir}","message":{{"content":"hi"}}}}\n'
        )

        output_parent = tmp_path / "output"
        output_parent.mkdir()

        monkeypatch.setattr(
            ct,
            "select_entry",
            _make_mock_select_entry(["__first__", ("__first__", "html")]),
        )

        result = CliRunner().invoke(cli, ["local", "-a", "-o", str(output_parent)])
        assert result.exit_code == 0, result.output
        expected_dir = output_parent / "my-session-file"
        assert expected_dir.exists()
        assert (expected_dir / "index.html").exists()

    def test_output_auto_with_jsonl_uses_stem(self, tmp_path, monkeypatch):
        """Test that -a with JSONL file uses file stem (without .jsonl extension)."""
        from click.testing import CliRunner
        from cad import cli

        # Create a JSONL file
        fixture_path = FIXTURE_DIR / "sample_session.jsonl"

        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-a"],
        )

        assert result.exit_code == 0
        # Output should be in ./sample_session/ (not ./sample_session.jsonl/)
        expected_dir = tmp_path / "sample_session"
        assert expected_dir.exists()
        assert (expected_dir / "index.html").exists()

class TestSearchFeature:
    """Tests for the search feature on index.html pages."""

    def test_search_box_in_index_html(self, output_dir):
        """Test that search box is present in index.html."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # Search box should be present with id="search-box"
        assert 'id="search-box"' in index_html
        # Search input should be present
        assert 'id="search-input"' in index_html
        # Search button should be present
        assert 'id="search-btn"' in index_html

    def test_search_modal_in_index_html(self, output_dir):
        """Test that search modal dialog is present in index.html."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # Search modal should be present
        assert 'id="search-modal"' in index_html
        # Results container should be present
        assert 'id="search-results"' in index_html

    def test_search_javascript_present(self, output_dir):
        """Test that search JavaScript functionality is present."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # JavaScript should handle DOMParser for parsing fetched pages
        assert "DOMParser" in index_html
        # JavaScript should handle fetch for getting pages
        assert "fetch(" in index_html
        # JavaScript should handle #search= URL fragment
        assert "#search=" in index_html or "search=" in index_html

    def test_search_css_present(self, output_dir):
        """Test that search CSS styles are present."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # CSS should style the search box
        assert "#search-box" in index_html or ".search-box" in index_html
        # CSS should style the search modal
        assert "#search-modal" in index_html or ".search-modal" in index_html

    def test_search_box_hidden_by_default_in_css(self, output_dir):
        """Test that search box is hidden by default (for progressive enhancement)."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # Search box should be hidden by default in CSS
        # JavaScript will show it when loaded
        assert "search-box" in index_html
        # The JS should show the search box
        assert "style.display" in index_html or "classList" in index_html

    def test_search_total_pages_available(self, output_dir):
        """Test that total_pages is available to JavaScript for fetching."""
        fixture_path = FIXTURE_DIR / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # Total pages should be embedded for JS to know how many pages to fetch
        assert "totalPages" in index_html or "total_pages" in index_html

class TestGenerateBatchHtml:
    """Tests for generate_batch_html function."""

    def test_creates_output_directory(self, mock_projects_dir, output_dir):
        """Test that output directory is created."""
        generate_batch_html(mock_projects_dir, output_dir)
        assert output_dir.exists()

    def test_creates_master_index(self, mock_projects_dir, output_dir):
        """Test that master index.html is created."""
        generate_batch_html(mock_projects_dir, output_dir)
        assert (output_dir / "index.html").exists()

    def test_creates_project_directories(self, mock_projects_dir, output_dir):
        """Test that project directories are created."""
        generate_batch_html(mock_projects_dir, output_dir)

        assert (output_dir / "project-a").exists()
        assert (output_dir / "project-b").exists()

    def test_creates_project_indexes(self, mock_projects_dir, output_dir):
        """Test that project index.html files are created."""
        generate_batch_html(mock_projects_dir, output_dir)

        assert (output_dir / "project-a" / "index.html").exists()
        assert (output_dir / "project-b" / "index.html").exists()

    def test_creates_session_directories(self, mock_projects_dir, output_dir):
        """Test that session directories are created with transcripts."""
        generate_batch_html(mock_projects_dir, output_dir)

        # Check project-a has session directories
        project_a_dir = output_dir / "project-a"
        session_dirs = [d for d in project_a_dir.iterdir() if d.is_dir()]
        assert len(session_dirs) == 2

        # Each session directory should have an index.html
        for session_dir in session_dirs:
            assert (session_dir / "index.html").exists()

    def test_master_index_lists_all_projects(self, mock_projects_dir, output_dir):
        """Test that master index lists all projects."""
        generate_batch_html(mock_projects_dir, output_dir)

        index_html = (output_dir / "index.html").read_text()
        assert "project-a" in index_html
        assert "project-b" in index_html

    def test_master_index_shows_session_counts(self, mock_projects_dir, output_dir):
        """Test that master index shows session counts per project."""
        generate_batch_html(mock_projects_dir, output_dir)

        index_html = (output_dir / "index.html").read_text()
        # project-a has 2 sessions, project-b has 1
        assert "2 sessions" in index_html or "2 session" in index_html
        assert "1 session" in index_html

    def test_project_index_lists_sessions(self, mock_projects_dir, output_dir):
        """Test that project index lists all sessions."""
        generate_batch_html(mock_projects_dir, output_dir)

        project_a_index = (output_dir / "project-a" / "index.html").read_text()
        # Should contain links to session directories
        assert "abc123" in project_a_index
        assert "def456" in project_a_index

    def test_returns_statistics(self, mock_projects_dir, output_dir):
        """Test that batch generation returns statistics."""
        stats = generate_batch_html(mock_projects_dir, output_dir)

        assert stats["total_projects"] == 2
        assert stats["total_sessions"] == 3  # 2 + 1
        assert stats["failed_sessions"] == []
        assert "output_dir" in stats

    def test_progress_callback_called(self, mock_projects_dir, output_dir):
        """Test that progress callback is called for each session."""
        progress_calls = []

        def on_progress(project_name, session_name, current, total):
            progress_calls.append((project_name, session_name, current, total))

        generate_batch_html(
            mock_projects_dir, output_dir, progress_callback=on_progress
        )

        # Should be called for each session (3 total)
        assert len(progress_calls) == 3
        # Last call should have current == total
        assert progress_calls[-1][2] == progress_calls[-1][3]

    def test_handles_failed_session_gracefully(self, output_dir):
        """Test that failed session conversion doesn't crash the batch."""
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir)

            # Create a project with 2 sessions
            project = projects_dir / "-home-user-projects-test"
            project.mkdir(parents=True)

            # Session 1
            session1 = project / "session1.jsonl"
            session1.write_text(
                '{"type": "user", "timestamp": "2025-01-01T10:00:00.000Z", "message": {"role": "user", "content": "Hello from session 1"}}\n'
            )

            # Session 2
            session2 = project / "session2.jsonl"
            session2.write_text(
                '{"type": "user", "timestamp": "2025-01-02T10:00:00.000Z", "message": {"role": "user", "content": "Hello from session 2"}}\n'
            )

            # Patch generate_html to fail on one specific session
            original_generate_html = __import__("cad").generate_html

            def mock_generate_html(json_path, output_dir, github_repo=None):
                if "session1" in str(json_path):
                    raise RuntimeError("Simulated failure")
                return original_generate_html(json_path, output_dir, github_repo)

            # generate_batch_html lives in cad.features.html alongside
            # generate_html and references it via its module namespace,
            # so patch there (the cad-level re-export is a separate
            # binding that doesn't affect the call site).
            with patch(
                "cad.features.html.generate_html", side_effect=mock_generate_html
            ):
                stats = generate_batch_html(projects_dir, output_dir)

            # Should have processed session2 successfully
            assert stats["total_sessions"] == 1
            # Should have recorded session1 as failed
            assert len(stats["failed_sessions"]) == 1
            assert "session1" in stats["failed_sessions"][0]["session"]
            assert "Simulated failure" in stats["failed_sessions"][0]["error"]

class TestAllCommand:
    """Tests for the all CLI command."""

    def test_all_command_exists(self):
        """Test that all command is registered."""
        runner = CliRunner()
        result = runner.invoke(cli, ["all", "--help"])
        assert result.exit_code == 0
        assert "all" in result.output.lower() or "convert" in result.output.lower()

    def test_all_dry_run(self, mock_projects_dir, output_dir):
        """Test dry-run mode shows what would be converted."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "all",
                "--source",
                str(mock_projects_dir),
                "--output",
                str(output_dir),
                "--dry-run",
            ],
        )

        assert result.exit_code == 0
        assert "project-a" in result.output
        assert "project-b" in result.output
        # Dry run should not create files
        assert not (output_dir / "index.html").exists()

    def test_all_creates_archive(self, mock_projects_dir, output_dir):
        """Test all command creates full archive."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "all",
                "--source",
                str(mock_projects_dir),
                "--output",
                str(output_dir),
            ],
        )

        assert result.exit_code == 0
        assert (output_dir / "index.html").exists()

    def test_all_include_agents_flag(self, mock_projects_dir, output_dir):
        """Test --include-agents flag includes agent sessions."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "all",
                "--source",
                str(mock_projects_dir),
                "--output",
                str(output_dir),
                "--include-agents",
            ],
        )

        assert result.exit_code == 0
        # Should have agent directory in project-a
        project_a_dir = output_dir / "project-a"
        session_dirs = [d for d in project_a_dir.iterdir() if d.is_dir()]
        assert len(session_dirs) == 3  # 2 regular + 1 agent

    def test_all_quiet_flag(self, mock_projects_dir, output_dir):
        """Test --quiet flag suppresses non-error output."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "all",
                "--source",
                str(mock_projects_dir),
                "--output",
                str(output_dir),
                "--quiet",
            ],
        )

        assert result.exit_code == 0
        # Should create the archive
        assert (output_dir / "index.html").exists()
        # Output should be minimal (no progress messages)
        assert "Scanning" not in result.output
        assert "Processed" not in result.output
        assert "Generating" not in result.output

    def test_all_quiet_with_dry_run(self, mock_projects_dir, output_dir):
        """Test --quiet flag works with --dry-run."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "all",
                "--source",
                str(mock_projects_dir),
                "--output",
                str(output_dir),
                "--dry-run",
                "--quiet",
            ],
        )

        assert result.exit_code == 0
        # Dry run with quiet should produce no output
        assert "Dry run" not in result.output
        assert "project-a" not in result.output
        # Should not create any files
        assert not (output_dir / "index.html").exists()

class TestJsonCommandWithUrl:
    """Tests for the json command with URL support."""

    def test_json_command_accepts_url(self, output_dir):
        """Test that json command can accept a URL starting with http:// or https://."""
        from unittest.mock import patch, MagicMock

        # Sample JSONL content
        jsonl_content = (
            '{"type": "user", "timestamp": "2025-01-01T10:00:00.000Z", "message": {"role": "user", "content": "Hello from URL"}}\n'
            '{"type": "assistant", "timestamp": "2025-01-01T10:00:05.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi there!"}]}}\n'
        )

        # Mock the httpx.get response
        mock_response = MagicMock()
        mock_response.text = jsonl_content
        mock_response.raise_for_status = MagicMock()

        runner = CliRunner()
        with patch("cad.httpx.get", return_value=mock_response) as mock_get:
            result = runner.invoke(
                cli,
                [
                    "json",
                    "https://example.com/session.jsonl",
                    "-o",
                    str(output_dir),
                ],
            )

        # Check that the URL was fetched
        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        assert call_url == "https://example.com/session.jsonl"

        # Check that HTML was generated
        assert result.exit_code == 0
        assert (output_dir / "index.html").exists()

    def test_json_command_accepts_http_url(self, output_dir):
        """Test that json command can accept http:// URLs."""
        from unittest.mock import patch, MagicMock

        jsonl_content = '{"type": "user", "timestamp": "2025-01-01T10:00:00.000Z", "message": {"role": "user", "content": "Hello"}}\n'

        mock_response = MagicMock()
        mock_response.text = jsonl_content
        mock_response.raise_for_status = MagicMock()

        runner = CliRunner()
        with patch("cad.httpx.get", return_value=mock_response) as mock_get:
            result = runner.invoke(
                cli,
                [
                    "json",
                    "http://example.com/session.jsonl",
                    "-o",
                    str(output_dir),
                ],
            )

        mock_get.assert_called_once()
        assert result.exit_code == 0

    def test_json_command_url_fetch_error(self, output_dir):
        """Test that json command handles URL fetch errors gracefully."""
        from unittest.mock import patch
        import httpx

        runner = CliRunner()
        with patch(
            "cad.httpx.get",
            side_effect=httpx.RequestError("Network error"),
        ):
            result = runner.invoke(
                cli,
                [
                    "json",
                    "https://example.com/session.jsonl",
                    "-o",
                    str(output_dir),
                ],
            )

        assert result.exit_code != 0
        assert "error" in result.output.lower() or "Error" in result.output

    def test_json_command_still_works_with_local_file(self, output_dir):
        """Test that json command still works with local file paths."""
        # Create a temp JSONL file
        jsonl_file = output_dir / "test.jsonl"
        jsonl_file.write_text(
            '{"type": "user", "timestamp": "2025-01-01T10:00:00.000Z", "message": {"role": "user", "content": "Hello local"}}\n'
            '{"type": "assistant", "timestamp": "2025-01-01T10:00:05.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}]}}\n'
        )

        html_output = output_dir / "html_output"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "json",
                str(jsonl_file),
                "-o",
                str(html_output),
            ],
        )

        assert result.exit_code == 0
        assert (html_output / "index.html").exists()
