"""Tests for HTML generation from Claude Code session JSON."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from syrupy.extensions.single_file import SingleFileSnapshotExtension, WriteMode

from claude_code_transcripts import (
    generate_html,
    detect_github_repo,
    render_markdown_text,
    format_json,
    is_json_like,
    render_todo_write,
    render_write_tool,
    render_edit_tool,
    render_bash_tool,
    render_content_block,
    analyze_conversation,
    format_tool_stats,
    is_tool_result_message,
    inject_gist_preview_js,
    create_gist,
    GIST_PREVIEW_JS,
    parse_session_file,
    get_session_summary,
    find_local_sessions,
    find_local_projects,
    _prune_temp_outputs,
    get_session_cwd,
)


class HTMLSnapshotExtension(SingleFileSnapshotExtension):
    """Snapshot extension that saves HTML files."""

    _write_mode = WriteMode.TEXT
    file_extension = "html"


@pytest.fixture
def snapshot_html(snapshot):
    """Fixture for HTML file snapshots."""
    return snapshot.use_extension(HTMLSnapshotExtension)


@pytest.fixture
def sample_session():
    """Load the sample session fixture."""
    fixture_path = Path(__file__).parent / "sample_session.json"
    with open(fixture_path) as f:
        return json.load(f)


@pytest.fixture
def output_dir():
    """Create a temporary output directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestGenerateHtml:
    """Tests for the main generate_html function."""

    def test_generates_index_html(self, output_dir, snapshot_html):
        """Test index.html generation."""
        fixture_path = Path(__file__).parent / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert index_html == snapshot_html

    def test_generates_page_001_html(self, output_dir, snapshot_html):
        """Test page-001.html generation."""
        fixture_path = Path(__file__).parent / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        page_html = (output_dir / "page-001.html").read_text(encoding="utf-8")
        assert page_html == snapshot_html

    def test_generates_page_002_html(self, output_dir, snapshot_html):
        """Test page-002.html generation (continuation page)."""
        fixture_path = Path(__file__).parent / "sample_session.json"
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
        # Need to set the global _github_repo for commit link rendering
        import claude_code_transcripts

        old_repo = claude_code_transcripts._github_repo
        claude_code_transcripts._github_repo = "example/repo"
        try:
            block = {
                "type": "tool_result",
                "content": "[main abc1234] Add new feature\n 2 files changed, 10 insertions(+)",
                "is_error": False,
            }
            result = render_content_block(block)
            assert result == snapshot_html
        finally:
            claude_code_transcripts._github_repo = old_repo

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
        from claude_code_transcripts import cli
        import subprocess

        # Create sample session file
        fixture_path = Path(__file__).parent / "sample_session.json"

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
        monkeypatch.setattr(
            "claude_code_transcripts.tempfile.gettempdir", lambda: str(tmp_path)
        )

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
        from claude_code_transcripts import cli
        import subprocess

        fixture_path = Path(__file__).parent / "sample_session.json"

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
        from claude_code_transcripts import cli

        fixture_path = Path(__file__).parent / "sample_session.json"

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
        from claude_code_transcripts import cli

        fixture_path = Path(__file__).parent / "sample_session.json"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-o", str(output_dir), "--json"],
        )

        assert result.exit_code == 0
        # Should use original filename, not "session.json"
        assert (output_dir / "sample_session.json").exists()
        assert not (output_dir / "session.json").exists()


class TestImportJsonOption:
    """Tests for the import command --json option."""

    def test_import_json_saves_session_data(self, httpx_mock, output_dir):
        """Test that import --json saves the session JSON."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        # Load sample session to mock API response
        fixture_path = Path(__file__).parent / "sample_session.json"
        with open(fixture_path) as f:
            session_data = json.load(f)

        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/session_ingress/session/test-session-id",
            json=session_data,
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "web",
                "test-session-id",
                "--token",
                "test-token",
                "--org-uuid",
                "test-org",
                "-o",
                str(output_dir),
                "--json",
            ],
        )

        assert result.exit_code == 0
        json_file = output_dir / "test-session-id.json"
        assert json_file.exists()
        assert "JSON:" in result.output
        assert "KB" in result.output

        # Verify JSON content is valid
        with open(json_file) as f:
            saved_data = json.load(f)
        assert saved_data == session_data


class TestImportGistOption:
    """Tests for the import command --gist option."""

    def test_import_gist_creates_gist(self, httpx_mock, monkeypatch, tmp_path):
        """Test that import --gist creates a gist."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import subprocess

        # Load sample session to mock API response
        fixture_path = Path(__file__).parent / "sample_session.json"
        with open(fixture_path) as f:
            session_data = json.load(f)

        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/session_ingress/session/test-session-id",
            json=session_data,
        )

        # Mock subprocess.run for gh gist create
        mock_result = subprocess.CompletedProcess(
            args=["gh", "gist", "create"],
            returncode=0,
            stdout="https://gist.github.com/testuser/def456\n",
            stderr="",
        )

        def mock_run(*args, **kwargs):
            return mock_result

        monkeypatch.setattr(subprocess, "run", mock_run)

        # Mock tempfile.gettempdir
        monkeypatch.setattr(
            "claude_code_transcripts.tempfile.gettempdir", lambda: str(tmp_path)
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "web",
                "test-session-id",
                "--token",
                "test-token",
                "--org-uuid",
                "test-org",
                "--gist",
            ],
        )

        assert result.exit_code == 0
        assert "Creating GitHub gist" in result.output
        assert "gist.github.com" in result.output
        assert "gisthost.github.io" in result.output


class TestVersionOption:
    """Tests for the --version option."""

    def test_version_long_flag(self):
        """Test that --version shows version info."""
        import importlib.metadata
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])

        expected_version = importlib.metadata.version("claude-code-transcripts")
        assert result.exit_code == 0
        assert expected_version in result.output

    def test_version_short_flag(self):
        """Test that -v shows version info."""
        import importlib.metadata
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["-v"])

        expected_version = importlib.metadata.version("claude-code-transcripts")
        assert result.exit_code == 0
        assert expected_version in result.output


class TestOpenOption:
    """Tests for the --open option."""

    def test_session_open_calls_webbrowser(self, output_dir, monkeypatch):
        """Test that session --open opens the browser."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        fixture_path = Path(__file__).parent / "sample_session.json"

        # Track webbrowser.open calls
        opened_urls = []

        def mock_open(url):
            opened_urls.append(url)
            return True

        monkeypatch.setattr("claude_code_transcripts.webbrowser.open", mock_open)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-o", str(output_dir), "--open"],
        )

        assert result.exit_code == 0
        assert len(opened_urls) == 1
        assert "index.html" in opened_urls[0]
        assert opened_urls[0].startswith("file://")

    def test_import_open_calls_webbrowser(self, httpx_mock, output_dir, monkeypatch):
        """Test that import --open opens the browser."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        # Load sample session to mock API response
        fixture_path = Path(__file__).parent / "sample_session.json"
        with open(fixture_path) as f:
            session_data = json.load(f)

        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/session_ingress/session/test-session-id",
            json=session_data,
        )

        # Track webbrowser.open calls
        opened_urls = []

        def mock_open(url):
            opened_urls.append(url)
            return True

        monkeypatch.setattr("claude_code_transcripts.webbrowser.open", mock_open)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "web",
                "test-session-id",
                "--token",
                "test-token",
                "--org-uuid",
                "test-org",
                "-o",
                str(output_dir),
                "--open",
            ],
        )

        assert result.exit_code == 0
        assert len(opened_urls) == 1
        assert "index.html" in opened_urls[0]
        assert opened_urls[0].startswith("file://")


class TestParseSessionFile:
    """Tests for parse_session_file which abstracts both JSON and JSONL formats."""

    def test_parses_json_format(self):
        """Test that standard JSON format is parsed correctly."""
        fixture_path = Path(__file__).parent / "sample_session.json"
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
        fixture_path = Path(__file__).parent / "sample_session.jsonl"
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
        fixture_path = Path(__file__).parent / "sample_session.jsonl"
        result = parse_session_file(fixture_path)

        # None of the loglines should be summary or file-history-snapshot
        for entry in result["loglines"]:
            assert entry["type"] in ("user", "assistant")

    def test_jsonl_preserves_message_content(self):
        """Test that message content is preserved correctly."""
        fixture_path = Path(__file__).parent / "sample_session.jsonl"
        result = parse_session_file(fixture_path)

        # Find the first user message
        user_msg = next(e for e in result["loglines"] if e["type"] == "user")
        assert user_msg["message"]["content"] == "Create a hello world function"

    def test_jsonl_generates_html(self, output_dir, snapshot_html):
        """Test that JSONL files can be converted to HTML."""
        fixture_path = Path(__file__).parent / "sample_session.jsonl"
        generate_html(fixture_path, output_dir)

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")
        assert "hello world" in index_html.lower()
        assert index_html == snapshot_html


class TestGetSessionSummary:
    """Tests for get_session_summary which extracts summary from session files."""

    def test_gets_summary_from_jsonl(self):
        """Test extracting summary from JSONL file."""
        fixture_path = Path(__file__).parent / "sample_session.jsonl"
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


def _write_session(folder, name, summary="Session"):
    """Helper: write a minimal valid session JSONL into folder."""
    f = folder / name
    f.write_text(
        f'{{"type":"summary","summary":"{summary}"}}\n'
        '{"type":"user","timestamp":"2025-01-01T00:00:00Z",'
        '"message":{"role":"user","content":"test"}}\n'
    )
    return f


class TestFindLocalProjects:
    """Tests for find_local_projects which discovers project folders without
    reading session summaries (the speed win behind the two-step picker)."""

    def test_returns_empty_for_missing_folder(self, tmp_path):
        assert find_local_projects(tmp_path / "does-not-exist") == []

    def test_returns_empty_for_empty_folder(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)
        assert find_local_projects(projects_dir) == []

    def test_skips_folders_with_no_jsonl(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        (proj / "readme.txt").write_text("nope")
        assert find_local_projects(projects_dir) == []

    def test_skips_folders_with_only_agent_files(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "agent-123.jsonl")
        assert find_local_projects(projects_dir) == []

    def test_counts_sessions_correctly(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "a.jsonl")
        _write_session(proj, "b.jsonl")
        _write_session(proj, "c.jsonl")
        _write_session(proj, "agent-skip.jsonl")  # excluded
        results = find_local_projects(projects_dir)
        assert len(results) == 1
        assert results[0]["session_count"] == 3

    def test_latest_mtime_is_max_session_mtime(self, tmp_path):
        import os

        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        old = _write_session(proj, "old.jsonl")
        new = _write_session(proj, "new.jsonl")
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))

        results = find_local_projects(projects_dir)
        assert len(results) == 1
        assert results[0]["latest_mtime"] == 2_000_000

    def test_sorted_by_latest_mtime_desc(self, tmp_path):
        import os

        projects_dir = tmp_path / ".claude" / "projects"
        a = projects_dir / "-Users-x-Code-aaa"
        b = projects_dir / "-Users-x-Code-bbb"
        c = projects_dir / "-Users-x-Code-ccc"
        for p in (a, b, c):
            p.mkdir(parents=True)
        os.utime(_write_session(a, "s.jsonl"), (1_000_000, 1_000_000))
        os.utime(_write_session(b, "s.jsonl"), (3_000_000, 3_000_000))
        os.utime(_write_session(c, "s.jsonl"), (2_000_000, 2_000_000))

        results = find_local_projects(projects_dir)
        assert [r["raw_name"] for r in results] == [
            "-Users-x-Code-bbb",
            "-Users-x-Code-ccc",
            "-Users-x-Code-aaa",
        ]

    def test_display_name_uses_helper(self, tmp_path):
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "s.jsonl")
        results = find_local_projects(projects_dir)
        assert results[0]["name"] == "foo"

    def test_collision_appends_disambiguator(self, tmp_path):
        """Two folders that collapse to the same display name get the raw
        folder appended; a third, unique folder stays clean."""
        projects_dir = tmp_path / ".claude" / "projects"
        # Both of these reduce to the same display name via
        # get_project_display_name (intermediate dirs stripped).
        a = projects_dir / "-Users-x-Code-foo"
        b = projects_dir / "-Users-x-projects-foo"
        c = projects_dir / "-Users-x-Code-unique"
        for p in (a, b, c):
            p.mkdir(parents=True)
            _write_session(p, "s.jsonl")

        results = find_local_projects(projects_dir)
        by_raw = {r["raw_name"]: r for r in results}

        # Both colliders carry the disambiguator suffix in their display string
        assert "(-Users-x-Code-foo)" in by_raw["-Users-x-Code-foo"]["display"]
        assert "(-Users-x-projects-foo)" in by_raw["-Users-x-projects-foo"]["display"]
        # Non-colliding row stays clean
        assert "(" not in by_raw["-Users-x-Code-unique"]["display"]

    def test_does_not_read_session_summaries(self, tmp_path, monkeypatch):
        """find_local_projects must not call get_session_summary — the whole
        point is to keep the project picker cheap."""
        import claude_code_transcripts

        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "s.jsonl")

        def boom(*a, **kw):
            raise AssertionError("get_session_summary should not be called")

        monkeypatch.setattr(claude_code_transcripts, "get_session_summary", boom)
        # Should not raise
        results = find_local_projects(projects_dir)
        assert len(results) == 1

    def test_real_projects_carry_paths_list(self, tmp_path):
        """Every project entry exposes a paths list (single-element for real
        projects). The session picker uses this uniformly across real and
        virtual projects."""
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "s.jsonl")

        results = find_local_projects(projects_dir)
        assert results[0]["paths"] == [proj]

    def test_merges_home_and_home_code_into_global_sessions(
        self, tmp_path, monkeypatch
    ):
        """The two folders the user uses for quick one-off questions
        (~/ and ~/Code) merge into a single virtual 'Global Sessions' entry."""
        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))

        projects_dir = tmp_path / ".claude" / "projects"
        home_folder = projects_dir / "-Users-x"
        code_folder = projects_dir / "-Users-x-Code"
        for p in (home_folder, code_folder):
            p.mkdir(parents=True)
        _write_session(home_folder, "a.jsonl")
        _write_session(home_folder, "b.jsonl")
        _write_session(code_folder, "c.jsonl")

        results = find_local_projects(projects_dir)
        # Exactly one virtual entry — neither raw folder appears separately.
        assert len(results) == 1
        global_entry = results[0]
        assert global_entry["name"] == "Global Sessions"
        assert global_entry["session_count"] == 3
        assert sorted(global_entry["paths"]) == sorted([home_folder, code_folder])

    def test_only_home_present_still_produces_global_sessions(
        self, tmp_path, monkeypatch
    ):
        """If only one of the two global folders exists, the virtual entry
        still represents it (no need to require both)."""
        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))

        projects_dir = tmp_path / ".claude" / "projects"
        home_folder = projects_dir / "-Users-x"
        home_folder.mkdir(parents=True)
        _write_session(home_folder, "a.jsonl")

        results = find_local_projects(projects_dir)
        assert len(results) == 1
        assert results[0]["name"] == "Global Sessions"
        assert results[0]["paths"] == [home_folder]

    def test_global_sessions_does_not_swallow_real_projects(
        self, tmp_path, monkeypatch
    ):
        """A real project under ~/Code (e.g. ~/Code/foo) keeps its own entry —
        only the bare ~/ and ~/Code folders are merged."""
        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))

        projects_dir = tmp_path / ".claude" / "projects"
        home_folder = projects_dir / "-Users-x"
        code_folder = projects_dir / "-Users-x-Code"
        real_proj = projects_dir / "-Users-x-Code-claude-code-transcripts"
        for p in (home_folder, code_folder, real_proj):
            p.mkdir(parents=True)
            _write_session(p, "s.jsonl")

        results = find_local_projects(projects_dir)
        names = [r["name"] for r in results]
        assert "Global Sessions" in names
        # The real project survives as a separate, non-virtual entry.
        assert any(
            r["paths"] == [real_proj] and r["name"] != "Global Sessions"
            for r in results
        )

    def test_no_global_entry_when_no_global_folders(self, tmp_path, monkeypatch):
        """If neither ~/ nor ~/Code has sessions, no virtual entry is added."""
        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))

        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "s.jsonl")

        results = find_local_projects(projects_dir)
        assert all(r["name"] != "Global Sessions" for r in results)


class TestResumeWritesCwdFile:
    """When CCT_CWD_FILE is set in the environment, resume_session writes
    the target cwd to that file before exec'ing claude. The shell wrapper
    installed via `cct shell-init` reads it after claude exits to chdir
    the parent shell."""

    def test_writes_cwd_when_env_set(self, tmp_path, monkeypatch):
        import claude_code_transcripts as ct

        real_cwd = tmp_path / "proj"
        real_cwd.mkdir()
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text(f'{{"type":"user","cwd":"{real_cwd}"}}\n')

        cwd_file = tmp_path / "cct-cwd"
        monkeypatch.setenv("CCT_CWD_FILE", str(cwd_file))

        # Stub execvp so the test doesn't actually replace the process.
        monkeypatch.setattr(ct.os, "execvp", lambda *a, **kw: None)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        ct.resume_session(jsonl)
        assert cwd_file.read_text() == str(real_cwd)

    def test_no_file_written_when_env_unset(self, tmp_path, monkeypatch):
        """Plain `cct` (no wrapper) should not leave files behind."""
        import claude_code_transcripts as ct

        real_cwd = tmp_path / "proj"
        real_cwd.mkdir()
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text(f'{{"type":"user","cwd":"{real_cwd}"}}\n')

        monkeypatch.delenv("CCT_CWD_FILE", raising=False)
        monkeypatch.setattr(ct.os, "execvp", lambda *a, **kw: None)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        ct.resume_session(jsonl)
        # No file was specified so nothing should be written; just confirming
        # the call didn't crash.


class TestShellInit:
    """`cct shell-init <shell>` prints a wrapper function the user evals
    in their rc file. The function calls the underlying binary with
    CCT_CWD_FILE set, then cd's the parent shell after exit."""

    def test_zsh_output_contains_wrapper(self):
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        result = CliRunner().invoke(cli, ["shell-init", "zsh"])
        assert result.exit_code == 0
        assert "CCT_CWD_FILE" in result.output
        assert "cct()" in result.output
        # Must call the underlying binary, not recurse
        assert "command cct" in result.output

    def test_bash_output_contains_wrapper(self):
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        result = CliRunner().invoke(cli, ["shell-init", "bash"])
        assert result.exit_code == 0
        assert "CCT_CWD_FILE" in result.output
        assert "cct()" in result.output

    def test_unknown_shell_errors(self):
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        result = CliRunner().invoke(cli, ["shell-init", "tcsh"])
        assert result.exit_code != 0


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


class TestPruneTempOutputs:
    """Tests for the temp-output prune helper that bounds disk usage in
    $TMPDIR/claude-code-transcripts/."""

    def test_does_nothing_when_under_threshold(self, tmp_path):
        for i in range(3):
            (tmp_path / f"sess-{i}").mkdir()
        _prune_temp_outputs(tmp_path, keep=5)
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "sess-0",
            "sess-1",
            "sess-2",
        ]

    def test_keeps_newest_n(self, tmp_path):
        import os

        # Create 5 dirs with staggered mtimes
        for i in range(5):
            d = tmp_path / f"sess-{i}"
            d.mkdir()
            os.utime(d, (1_000_000 + i * 100, 1_000_000 + i * 100))

        _prune_temp_outputs(tmp_path, keep=2)
        # Only the two newest survive (sess-3 and sess-4)
        survivors = sorted(p.name for p in tmp_path.iterdir())
        assert survivors == ["sess-3", "sess-4"]

    def test_ignores_files(self, tmp_path):
        """Non-directory entries shouldn't be considered when pruning."""
        (tmp_path / "stray.txt").write_text("not a session output")
        for i in range(3):
            (tmp_path / f"sess-{i}").mkdir()
        _prune_temp_outputs(tmp_path, keep=10)
        assert (tmp_path / "stray.txt").exists()

    def test_missing_parent_is_safe(self, tmp_path):
        # Should not raise even if the parent doesn't exist yet
        _prune_temp_outputs(tmp_path / "does-not-exist", keep=5)


def _make_mock_select(returns, calls=None):
    """Build a questionary.select stand-in that returns the next value from
    `returns` on each .ask() call. Used to script the two-step picker.

    If `calls` (a list) is passed, each invocation appends its kwargs to it
    so tests can assert what was passed to questionary.select.
    """
    queue = list(returns)

    class MockSelect:
        def __init__(self, *args, **kwargs):
            if calls is not None:
                calls.append(kwargs)

        def ask(self):
            return queue.pop(0)

    return MockSelect


class TestLocalSessionCLI:
    """Tests for CLI behavior with local sessions."""

    def test_local_html_action_generates_transcript(self, tmp_path, monkeypatch):
        """Picking 'html' (h) on a session triggers HTML generation."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct
        import questionary

        project_dir = tmp_path / ".claude" / "projects" / "test-project"
        project_dir.mkdir(parents=True)

        session_file = project_dir / "session-123.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Test local session"}\n'
            '{"type":"user","cwd":"/Users/x","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"Hello"}}\n'
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            questionary,
            "select",
            _make_mock_select([{"name": "test-project", "paths": [project_dir]}]),
        )
        # The session-step picker returns (filepath, action). Force the
        # html action so the rest of the existing render flow runs.
        monkeypatch.setattr(
            ct, "select_session_action", lambda entries: (session_file, "html")
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["local"])

        assert result.exit_code == 0
        assert "Loading projects" in result.output
        assert "Generated" in result.output

    def test_local_resume_action_invokes_claude(self, tmp_path, monkeypatch):
        """Picking the default Enter (resume) action chdir's to the cwd
        recorded in the JSONL and exec's claude with skip-permissions."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct
        import questionary

        project_dir = tmp_path / ".claude" / "projects" / "test-project"
        project_dir.mkdir(parents=True)
        # The cwd the JSONL claims must actually exist for resume to proceed.
        real_cwd = tmp_path / "real-project"
        real_cwd.mkdir()

        session_file = project_dir / "abc-123.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Test session"}\n'
            f'{{"type":"user","cwd":"{real_cwd}","message":{{"content":"Hi"}}}}\n'
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            questionary,
            "select",
            _make_mock_select([{"name": "test-project", "paths": [project_dir]}]),
        )
        monkeypatch.setattr(
            ct, "select_session_action", lambda entries: (session_file, "resume")
        )

        # Capture the exec call instead of actually replacing the process.
        exec_calls = []

        def fake_execvp(file, args):
            exec_calls.append((file, args, os.getcwd()))

        monkeypatch.setattr(ct.os, "execvp", fake_execvp)

        runner = CliRunner()
        result = runner.invoke(cli, ["local"])

        assert result.exit_code == 0, result.output
        assert len(exec_calls) == 1
        file, args, cwd_at_exec = exec_calls[0]
        assert file == "claude"
        assert args[0] == "claude"
        assert "--dangerously-skip-permissions" in args
        assert "--resume" in args
        assert "abc-123" in args
        assert cwd_at_exec == str(real_cwd)

    def test_local_handles_cancelled_project_selection(self, tmp_path, monkeypatch):
        """Cancelling at the project picker exits with a friendly message."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import questionary

        project_dir = tmp_path / ".claude" / "projects" / "test-project"
        project_dir.mkdir(parents=True)

        session_file = project_dir / "session-123.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Test session"}\n'
            '{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"Hello"}}\n'
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(questionary, "select", _make_mock_select([None]))

        runner = CliRunner()
        result = runner.invoke(cli, ["local"])

        assert result.exit_code == 0
        assert "No project selected" in result.output

    def test_local_handles_cancelled_session_selection(self, tmp_path, monkeypatch):
        """Cancelling at the session picker (Esc) exits cleanly."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct
        import questionary

        project_dir = tmp_path / ".claude" / "projects" / "test-project"
        project_dir.mkdir(parents=True)

        session_file = project_dir / "session-123.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Test session"}\n'
            '{"type":"user","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"Hello"}}\n'
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            questionary,
            "select",
            _make_mock_select([{"name": "test-project", "paths": [project_dir]}]),
        )
        monkeypatch.setattr(ct, "select_session_action", lambda entries: None)

        runner = CliRunner()
        result = runner.invoke(cli, ["local"])

        assert result.exit_code == 0
        assert "No session selected" in result.output

    def test_project_picker_has_search_filter_enabled(self, tmp_path, monkeypatch):
        """The project picker (still on questionary) keeps type-to-filter so
        users with many projects can narrow the list quickly. Pinned to
        catch accidental regressions during refactors."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct
        import questionary

        project_dir = tmp_path / ".claude" / "projects" / "test-project"
        project_dir.mkdir(parents=True)

        session_file = project_dir / "session-123.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Test session"}\n'
            '{"type":"user","cwd":"/Users/x","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"Hello"}}\n'
        )

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        calls = []
        monkeypatch.setattr(
            questionary,
            "select",
            _make_mock_select(
                [{"name": "test-project", "paths": [project_dir]}],
                calls=calls,
            ),
        )
        monkeypatch.setattr(
            ct, "select_session_action", lambda entries: (session_file, "html")
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["local"])

        assert result.exit_code == 0
        assert len(calls) == 1, "expected one questionary.select call (project)"
        assert calls[0].get("use_search_filter") is True


class TestOutputAutoOption:
    """Tests for the -a/--output-auto flag."""

    def test_json_output_auto_creates_subdirectory(self, tmp_path):
        """Test that json -a creates output subdirectory named after file stem."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        fixture_path = Path(__file__).parent / "sample_session.json"

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
        from claude_code_transcripts import cli
        import os

        fixture_path = Path(__file__).parent / "sample_session.json"

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
        from claude_code_transcripts import cli

        fixture_path = Path(__file__).parent / "sample_session.json"

        # Track webbrowser.open calls
        opened_urls = []

        def mock_open(url):
            opened_urls.append(url)
            return True

        monkeypatch.setattr("claude_code_transcripts.webbrowser.open", mock_open)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["json", str(fixture_path), "-a", "-o", str(tmp_path)],
        )

        assert result.exit_code == 0
        assert len(opened_urls) == 0  # No browser opened

    def test_local_output_auto_creates_subdirectory(self, tmp_path, monkeypatch):
        """Test that local -a creates output subdirectory named after file stem."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct
        import questionary

        # Create mock .claude/projects structure
        project_dir = tmp_path / ".claude" / "projects" / "test-project"
        project_dir.mkdir(parents=True)

        session_file = project_dir / "my-session-file.jsonl"
        session_file.write_text(
            '{"type":"summary","summary":"Test local session"}\n'
            '{"type":"user","cwd":"/Users/x","timestamp":"2025-01-01T00:00:00Z","message":{"role":"user","content":"Hello"}}\n'
        )

        output_parent = tmp_path / "output"
        output_parent.mkdir()

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            questionary,
            "select",
            _make_mock_select([{"name": "test-project", "paths": [project_dir]}]),
        )
        monkeypatch.setattr(
            ct, "select_session_action", lambda entries: (session_file, "html")
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["local", "-a", "-o", str(output_parent)])

        assert result.exit_code == 0
        # Output should be in output_parent/my-session-file/
        expected_dir = output_parent / "my-session-file"
        assert expected_dir.exists()
        assert (expected_dir / "index.html").exists()

    def test_web_output_auto_creates_subdirectory(self, httpx_mock, tmp_path):
        """Test that web -a creates output subdirectory named after session ID."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        # Load sample session to mock API response
        fixture_path = Path(__file__).parent / "sample_session.json"
        with open(fixture_path) as f:
            session_data = json.load(f)

        httpx_mock.add_response(
            url="https://api.anthropic.com/v1/session_ingress/session/my-web-session-id",
            json=session_data,
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "web",
                "my-web-session-id",
                "--token",
                "test-token",
                "--org-uuid",
                "test-org",
                "-a",
                "-o",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0
        # Output should be in tmp_path/my-web-session-id/
        expected_dir = tmp_path / "my-web-session-id"
        assert expected_dir.exists()
        assert (expected_dir / "index.html").exists()

    def test_output_auto_with_jsonl_uses_stem(self, tmp_path, monkeypatch):
        """Test that -a with JSONL file uses file stem (without .jsonl extension)."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli

        # Create a JSONL file
        fixture_path = Path(__file__).parent / "sample_session.jsonl"

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
        fixture_path = Path(__file__).parent / "sample_session.json"
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
        fixture_path = Path(__file__).parent / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # Search modal should be present
        assert 'id="search-modal"' in index_html
        # Results container should be present
        assert 'id="search-results"' in index_html

    def test_search_javascript_present(self, output_dir):
        """Test that search JavaScript functionality is present."""
        fixture_path = Path(__file__).parent / "sample_session.json"
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
        fixture_path = Path(__file__).parent / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # CSS should style the search box
        assert "#search-box" in index_html or ".search-box" in index_html
        # CSS should style the search modal
        assert "#search-modal" in index_html or ".search-modal" in index_html

    def test_search_box_hidden_by_default_in_css(self, output_dir):
        """Test that search box is hidden by default (for progressive enhancement)."""
        fixture_path = Path(__file__).parent / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # Search box should be hidden by default in CSS
        # JavaScript will show it when loaded
        assert "search-box" in index_html
        # The JS should show the search box
        assert "style.display" in index_html or "classList" in index_html

    def test_search_total_pages_available(self, output_dir):
        """Test that total_pages is available to JavaScript for fetching."""
        fixture_path = Path(__file__).parent / "sample_session.json"
        generate_html(fixture_path, output_dir, github_repo="example/project")

        index_html = (output_dir / "index.html").read_text(encoding="utf-8")

        # Total pages should be embedded for JS to know how many pages to fetch
        assert "totalPages" in index_html or "total_pages" in index_html
