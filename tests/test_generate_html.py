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
    find_pi_sessions,
    find_opencode_sessions,
    find_forge_sessions,
    _prune_temp_outputs,
    get_session_cwd,
    get_claude_session_metadata,
    load_session_summary,
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


def _write_session(folder, name, summary="Session", cwd=None):
    """Helper: write a minimal valid claude session JSONL into folder.

    The JSONL includes a ``cwd`` field on the user line so the new
    cwd-based grouping picks the file up. Tests that need a specific
    project directory pass it via ``cwd``; default is ``str(folder)``
    (so each session group lives under its own synthetic project).
    """
    if cwd is None:
        cwd = str(folder)
    f = folder / name
    f.write_text(
        f'{{"type":"summary","summary":"{summary}"}}\n'
        f'{{"type":"user","cwd":"{cwd}","timestamp":"2025-01-01T00:00:00Z",'
        '"message":{"role":"user","content":"test"}}\n'
    )
    return f


class TestFindLocalProjects:
    """Tests for find_local_projects: cwd-based grouping across providers.

    The picker doesn't load summaries until a project is chosen, so these
    tests exercise the discover+group layer only.
    """

    def test_returns_empty_for_missing_folder(self, tmp_path, monkeypatch):
        # Ensure codex root also doesn't pull anything from the real machine
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        assert find_local_projects(tmp_path / "does-not-exist") == []

    def test_returns_empty_for_empty_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        projects_dir.mkdir(parents=True)
        assert find_local_projects(projects_dir) == []

    def test_skips_sessions_without_cwd(self, tmp_path, monkeypatch):
        """Sessions whose JSONL has no cwd (warmups / broken files) can't be
        grouped meaningfully and are dropped."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        # No cwd field — just a summary line.
        (proj / "s.jsonl").write_text(
            '{"type":"summary","summary":"x"}\n'
            '{"type":"user","message":{"content":"hi"}}\n'
        )
        assert find_local_projects(projects_dir) == []

    def test_skips_agent_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "agent-123.jsonl", cwd="/Users/x/Code/foo")
        assert find_local_projects(projects_dir) == []

    def test_groups_sessions_by_cwd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "a.jsonl", cwd="/Users/x/Code/foo")
        _write_session(proj, "b.jsonl", cwd="/Users/x/Code/foo")
        _write_session(proj, "c.jsonl", cwd="/Users/x/Code/foo")
        _write_session(proj, "agent-skip.jsonl", cwd="/Users/x/Code/foo")

        results = find_local_projects(projects_dir)
        assert len(results) == 1
        assert results[0]["session_count"] == 3
        assert results[0]["cwd"] == "/Users/x/Code/foo"
        assert results[0]["name"] == "foo"
        assert results[0]["provider_counts"] == {"claude": 3}

    def test_latest_mtime_is_max_session_mtime(self, tmp_path, monkeypatch):
        import os as _os

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        old = _write_session(proj, "old.jsonl", cwd="/Users/x/Code/foo")
        new = _write_session(proj, "new.jsonl", cwd="/Users/x/Code/foo")
        _os.utime(old, (1_000_000, 1_000_000))
        _os.utime(new, (2_000_000, 2_000_000))

        results = find_local_projects(projects_dir)
        assert len(results) == 1
        assert results[0]["latest_mtime"] == 2_000_000

    def test_sorted_by_latest_mtime_desc(self, tmp_path, monkeypatch):
        import os as _os

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        for raw, dirname in [
            ("-Users-x-Code-aaa", "/Users/x/Code/aaa"),
            ("-Users-x-Code-bbb", "/Users/x/Code/bbb"),
            ("-Users-x-Code-ccc", "/Users/x/Code/ccc"),
        ]:
            (projects_dir / raw).mkdir(parents=True)
        _os.utime(
            _write_session(
                projects_dir / "-Users-x-Code-aaa", "s.jsonl", cwd="/Users/x/Code/aaa"
            ),
            (1_000_000, 1_000_000),
        )
        _os.utime(
            _write_session(
                projects_dir / "-Users-x-Code-bbb", "s.jsonl", cwd="/Users/x/Code/bbb"
            ),
            (3_000_000, 3_000_000),
        )
        _os.utime(
            _write_session(
                projects_dir / "-Users-x-Code-ccc", "s.jsonl", cwd="/Users/x/Code/ccc"
            ),
            (2_000_000, 2_000_000),
        )

        results = find_local_projects(projects_dir)
        assert [r["name"] for r in results] == ["bbb", "ccc", "aaa"]

    def test_display_name_is_cwd_basename(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "s.jsonl", cwd="/Users/x/Code/foo")
        assert find_local_projects(projects_dir)[0]["name"] == "foo"

    def test_collision_appends_cwd(self, tmp_path, monkeypatch):
        """Two real projects with the same basename but different cwds get
        the full cwd appended to their display row only."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        a = projects_dir / "raw-a"
        b = projects_dir / "raw-b"
        c = projects_dir / "raw-c"
        for p in (a, b, c):
            p.mkdir(parents=True)
        _write_session(a, "s.jsonl", cwd="/Users/x/Code/foo")
        _write_session(b, "s.jsonl", cwd="/Users/x/projects/foo")
        _write_session(c, "s.jsonl", cwd="/Users/x/Code/unique")

        results = find_local_projects(projects_dir)
        by_cwd = {r["cwd"]: r for r in results}

        assert "/Users/x/Code/foo" in by_cwd["/Users/x/Code/foo"]["display"]
        assert "/Users/x/projects/foo" in by_cwd["/Users/x/projects/foo"]["display"]
        assert "/Users/x/Code/unique" not in by_cwd["/Users/x/Code/unique"]["display"]

    def test_does_not_load_summaries(self, tmp_path, monkeypatch):
        """The project picker hydration must not pull summaries up front."""
        import claude_code_transcripts as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "s.jsonl", cwd="/Users/x/Code/foo")

        def boom(*a, **kw):
            raise AssertionError("summary loader should not be called")

        monkeypatch.setattr(ct, "get_session_summary", boom)
        monkeypatch.setattr(ct, "get_codex_summary", boom)
        monkeypatch.setattr(ct, "load_session_summary", boom)
        results = find_local_projects(projects_dir)
        assert len(results) == 1

    def test_real_project_exposes_session_filepaths(self, tmp_path, monkeypatch):
        """Every project entry exposes its sessions list with filepaths so
        the second-step picker / dispatch has what it needs."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        f = _write_session(proj, "s.jsonl", cwd="/Users/x/Code/foo")
        results = find_local_projects(projects_dir)
        assert [s["filepath"] for s in results[0]["sessions"]] == [f]

    def test_merges_home_and_home_code_into_global_sessions(
        self, tmp_path, monkeypatch
    ):
        """Sessions whose cwd is ~/ or ~/Code merge into one virtual entry."""
        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))
        projects_dir = tmp_path / ".claude" / "projects"
        home_folder = projects_dir / "-Users-x"
        code_folder = projects_dir / "-Users-x-Code"
        for p in (home_folder, code_folder):
            p.mkdir(parents=True)
        _write_session(home_folder, "a.jsonl", cwd="/Users/x")
        _write_session(home_folder, "b.jsonl", cwd="/Users/x")
        _write_session(code_folder, "c.jsonl", cwd="/Users/x/Code")

        results = find_local_projects(projects_dir)
        assert len(results) == 1
        assert results[0]["name"] == "Global Sessions"
        assert results[0]["session_count"] == 3
        assert results[0]["cwd"] is None

    def test_only_home_present_still_produces_global_sessions(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))
        projects_dir = tmp_path / ".claude" / "projects"
        home_folder = projects_dir / "-Users-x"
        home_folder.mkdir(parents=True)
        _write_session(home_folder, "a.jsonl", cwd="/Users/x")

        results = find_local_projects(projects_dir)
        assert len(results) == 1
        assert results[0]["name"] == "Global Sessions"

    def test_global_sessions_does_not_swallow_real_projects(
        self, tmp_path, monkeypatch
    ):
        """Real projects under ~/Code (e.g. ~/Code/foo) keep their own entry."""
        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))
        projects_dir = tmp_path / ".claude" / "projects"
        home_folder = projects_dir / "-Users-x"
        real_proj = projects_dir / "-Users-x-Code-foo"
        for p in (home_folder, real_proj):
            p.mkdir(parents=True)
        _write_session(home_folder, "h.jsonl", cwd="/Users/x")
        _write_session(real_proj, "r.jsonl", cwd="/Users/x/Code/foo")

        results = find_local_projects(projects_dir)
        names = [r["name"] for r in results]
        assert "Global Sessions" in names
        assert "foo" in names

    def test_codex_sessions_merge_with_claude_by_cwd(self, tmp_path, monkeypatch):
        """A claude session and a codex session sharing the same cwd land in
        a single project entry; the badge counts each provider."""
        fake_home = tmp_path / "fake-home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        projects_dir = tmp_path / ".claude" / "projects" / "-Users-x-Code-foo"
        projects_dir.mkdir(parents=True)
        _write_session(projects_dir, "claude-1.jsonl", cwd="/Users/x/Code/foo")

        # find_codex_sessions reads from Path.home()/.codex/sessions/, so the
        # codex fixture has to live under our fake home.
        codex_dir = fake_home / ".codex" / "sessions" / "2026" / "01" / "01"
        codex_dir.mkdir(parents=True)
        codex_file = codex_dir / "rollout-2026-01-01T00-00-00-codex-uuid.jsonl"
        codex_file.write_text(
            '{"type":"session_meta","payload":{"id":"codex-uuid","cwd":"/Users/x/Code/foo"}}\n'
        )

        results = find_local_projects(tmp_path / ".claude" / "projects")
        assert len(results) == 1
        assert results[0]["session_count"] == 2
        assert results[0]["provider_counts"] == {"claude": 1, "codex": 1}
        assert "1c+1x" in results[0]["display"]

    def test_no_global_entry_when_no_global_folders(self, tmp_path, monkeypatch):
        """If neither ~/ nor ~/Code has sessions, no virtual entry is added."""
        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))

        projects_dir = tmp_path / ".claude" / "projects"
        proj = projects_dir / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        _write_session(proj, "s.jsonl")

        results = find_local_projects(projects_dir)
        assert all(r["name"] != "Global Sessions" for r in results)


def _make_session(provider, session_id, cwd, filepath=None):
    """Helper: build a session dict with the minimum keys resume_session
    inspects. Tests don't need the lazy-loaded summary/display fields."""
    return {
        "provider": provider,
        "session_id": session_id,
        "cwd": str(cwd),
        "filepath": filepath or Path(f"/fake/{session_id}.jsonl"),
        "mtime": 0.0,
        "size": 0,
        "summary": None,
        "display": None,
    }


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


class TestCwdOverrideSidecar:
    """Cwd overrides live at ~/.cct/cwd-overrides.json. Discovery swaps
    them in before grouping, so a moved session lands in the new project.
    Agent files are never touched."""

    def test_save_and_read_override(self, tmp_path, monkeypatch):
        import claude_code_transcripts as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        target = tmp_path / "new-project"
        target.mkdir()
        ct.save_cwd_override("claude", "abc-123", str(target))
        # Stored as resolved absolute path
        assert ct.get_cwd_override("claude", "abc-123") == str(target.resolve())

    def test_empty_cwd_clears_override(self, tmp_path, monkeypatch):
        import claude_code_transcripts as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        target = tmp_path / "x"
        target.mkdir()
        ct.save_cwd_override("claude", "abc", str(target))
        ct.save_cwd_override("claude", "abc", "")
        assert ct.get_cwd_override("claude", "abc") is None

    def test_override_moves_session_to_new_project(self, tmp_path, monkeypatch):
        """A claude session whose JSONL records cwd=A but has a sidecar
        override pointing at B groups under B."""
        import claude_code_transcripts as ct

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
    """Sidecar lives at ~/.cct/titles.json, keyed by '<provider>:<id>'."""

    def test_save_and_read_override(self, tmp_path, monkeypatch):
        import claude_code_transcripts as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ct.save_title_override("claude", "abc-123", "My title")
        sess = {"provider": "claude", "session_id": "abc-123"}
        assert ct.get_title_override(sess) == "My title"

    def test_empty_title_removes_override(self, tmp_path, monkeypatch):
        import claude_code_transcripts as ct

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        ct.save_title_override("claude", "abc", "First")
        ct.save_title_override("claude", "abc", "")
        sess = {"provider": "claude", "session_id": "abc"}
        assert ct.get_title_override(sess) is None

    def test_override_wins_over_native_summary(self, tmp_path, monkeypatch):
        """load_session_summary picks the override even when a provider
        summary is already set (opencode/forge case)."""
        import claude_code_transcripts as ct

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


class TestResumeWritesCwdFile:
    """When CCT_CWD_FILE is set in the environment, resume_session writes
    the target cwd to that file before exec'ing claude. The shell wrapper
    installed via `cct shell-init` reads it after the agent exits to chdir
    the parent shell."""

    def test_writes_cwd_when_env_set(self, tmp_path, monkeypatch):
        import claude_code_transcripts as ct

        real_cwd = tmp_path / "proj"
        real_cwd.mkdir()
        sess = _make_session("claude", "abc", real_cwd)

        cwd_file = tmp_path / "cct-cwd"
        monkeypatch.setenv("CCT_CWD_FILE", str(cwd_file))
        monkeypatch.setattr(ct.os, "execvp", lambda *a, **kw: None)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        ct.resume_session(sess)
        assert cwd_file.read_text() == str(real_cwd)

    def test_no_file_written_when_env_unset(self, tmp_path, monkeypatch):
        """Plain `cct` (no wrapper) should not leave files behind."""
        import claude_code_transcripts as ct

        real_cwd = tmp_path / "proj"
        real_cwd.mkdir()
        sess = _make_session("claude", "abc", real_cwd)

        monkeypatch.delenv("CCT_CWD_FILE", raising=False)
        monkeypatch.setattr(ct.os, "execvp", lambda *a, **kw: None)
        monkeypatch.setattr(ct.os, "chdir", lambda *a, **kw: None)

        ct.resume_session(sess)
        # No file was specified so nothing should be written; just confirming
        # the call didn't crash.

    def test_codex_provider_invokes_codex_resume(self, tmp_path, monkeypatch):
        """resume_session dispatches to ``codex resume <id>`` for codex
        sessions instead of claude --resume."""
        import claude_code_transcripts as ct

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
    `returns` on each .ask() call. Used to script the project picker.

    Special sentinel ``"__first__"`` picks the first choice's ``value`` —
    handy for tests that want the discovery layer to run for real and just
    auto-select what it found, instead of hand-constructing project dicts.

    If `calls` (a list) is passed, each invocation appends its kwargs to it
    so tests can assert what was passed to questionary.select.
    """
    queue = list(returns)

    class MockSelect:
        def __init__(self, *args, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            self._kwargs = kwargs
            self._args = args

        def ask(self):
            value = queue.pop(0)
            if value == "__first__":
                choices = self._kwargs.get("choices") or (
                    self._args[1] if len(self._args) > 1 else []
                )
                return choices[0].value
            return value

    return MockSelect


def _make_mock_select_entry(returns):
    """Stand-in for select_entry — pops from a queue on each call.

    ``"__first__"`` returns ``(entries[0], "select")``.
    ``("__first__", "html")`` returns ``(entries[0], "html")``.
    ``None`` returns ``None`` (cancellation).
    Plain tuples are returned as-is.
    """
    queue = list(returns)

    def fake(entries, actions=None, back_action=None):
        if not queue:
            raise AssertionError("select_entry called more times than scripted")
        v = queue.pop(0)
        if v is None:
            return None
        if v == "__first__":
            return (entries[0], "select")
        if isinstance(v, tuple) and len(v) == 2 and v[0] == "__first__":
            return (entries[0], v[1])
        return v

    return fake


def _set_up_fake_home_with_session(tmp_path, monkeypatch, cwd_dir=None):
    """Build a fake ~/.claude/projects/<x>/<id>.jsonl pointing at cwd_dir.

    Returns (fake_home, project_cwd, session_file). The session file's cwd
    is `cwd_dir` (must exist for resume tests; created if not given).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    if cwd_dir is None:
        cwd_dir = tmp_path / "real-project"
        cwd_dir.mkdir()

    project_dir = fake_home / ".claude" / "projects" / "test-project"
    project_dir.mkdir(parents=True)
    session_file = project_dir / "abc-123.jsonl"
    session_file.write_text(
        '{"type":"summary","summary":"Test"}\n'
        f'{{"type":"user","cwd":"{cwd_dir}","message":{{"content":"hi"}}}}\n'
    )
    return fake_home, cwd_dir, session_file


class TestLocalSessionCLI:
    """End-to-end CLI tests. Discovery runs for real against tmp fixtures;
    only ``select_entry`` (the picker) is mocked. Both pickers (project
    and session) go through the same function now."""

    def test_local_html_action_generates_transcript(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

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
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

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
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

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
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

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
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)
        monkeypatch.setattr(ct, "select_entry", _make_mock_select_entry([None]))

        result = CliRunner().invoke(cli, ["local"])
        assert result.exit_code == 0
        assert "No project selected" in result.output

    def test_local_handles_cancelled_session_selection(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

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
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

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
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)

        call_count = {"n": 0}
        captured = {}

        def fake_select_entry(entries, actions=None, back_action=None):
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

    def test_back_action_returns_to_project_picker(self, tmp_path, monkeypatch):
        """Esc/Bksp on the session picker routes back to the project
        picker (outer loop) instead of quitting cct."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)

        call_count = {"n": 0}

        def fake_select_entry(entries, actions=None, back_action=None):
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

    def test_move_action_saves_cwd_override(self, tmp_path, monkeypatch):
        """Pressing m, entering a valid path, saves the cwd override and
        marks the row as recently updated."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)
        target = tmp_path / "new-target"
        target.mkdir()

        call_count = {"n": 0}

        def fake_select_entry(entries, actions=None, back_action=None):
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
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

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
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

        _set_up_fake_home_with_session(tmp_path, monkeypatch)

        call_log = []

        def fake_select_entry(entries, actions=None, back_action=None):
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
        """`local -a` creates an output subdirectory named after the
        chosen session's stem."""
        from click.testing import CliRunner
        from claude_code_transcripts import cli
        import claude_code_transcripts as ct

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
