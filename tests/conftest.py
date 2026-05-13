"""Pytest configuration and fixtures for cad tests."""

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def sample_session():
    """Load the canonical sample session fixture used by HTML render tests.
    Originally lived as a module-level fixture in tests/test_generate_html.py;
    promoted here so the html test files (now under tests/features/html/)
    can still reach it without relative-path gymnastics."""
    fixture_path = Path(__file__).parent / "sample_session.json"
    with open(fixture_path) as f:
        return json.load(f)


@pytest.fixture
def mock_projects_dir():
    """Create a mock ~/.claude/projects structure with test sessions.

    Two projects, one with 2 sessions + one agent file (skipped by
    default) + one warmup file (skipped), and a second project with
    1 session. Originally lived in the now-removed tests/test_all.py.
    Promoted to a top-level fixture so tests under tests/core/ and
    tests/features/html/ can both consume it.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        projects_dir = Path(tmpdir)

        # Create project-a with 2 sessions
        project_a = projects_dir / "-home-user-projects-project-a"
        project_a.mkdir(parents=True)

        session_a1 = project_a / "abc123.jsonl"
        session_a1.write_text(
            '{"type": "user", "timestamp": "2025-01-01T10:00:00.000Z", "message": {"role": "user", "content": "Hello from project A"}}\n'
            '{"type": "assistant", "timestamp": "2025-01-01T10:00:05.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi there!"}]}}\n'
        )

        session_a2 = project_a / "def456.jsonl"
        session_a2.write_text(
            '{"type": "user", "timestamp": "2025-01-02T10:00:00.000Z", "message": {"role": "user", "content": "Second session in project A"}}\n'
            '{"type": "assistant", "timestamp": "2025-01-02T10:00:05.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Got it!"}]}}\n'
        )

        # Create an agent file (should be skipped by default)
        agent_a = project_a / "agent-xyz789.jsonl"
        agent_a.write_text(
            '{"type": "user", "timestamp": "2025-01-03T10:00:00.000Z", "message": {"role": "user", "content": "Agent session"}}\n'
        )

        # Create project-b with 1 session
        project_b = projects_dir / "-home-user-projects-project-b"
        project_b.mkdir(parents=True)

        session_b1 = project_b / "ghi789.jsonl"
        session_b1.write_text(
            '{"type": "user", "timestamp": "2025-01-04T10:00:00.000Z", "message": {"role": "user", "content": "Hello from project B"}}\n'
            '{"type": "assistant", "timestamp": "2025-01-04T10:00:05.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Welcome!"}]}}\n'
        )

        # Create empty/warmup session (should be skipped)
        warmup = project_b / "warmup123.jsonl"
        warmup.write_text(
            '{"type": "user", "timestamp": "2025-01-05T10:00:00.000Z", "message": {"role": "user", "content": "warmup"}}\n'
        )

        yield projects_dir


@pytest.fixture
def output_dir():
    """Create a temporary output directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture(autouse=True)
def mock_webbrowser_open(monkeypatch):
    """Automatically mock webbrowser.open to prevent browsers opening during tests."""
    opened_urls = []

    def mock_open(url):
        opened_urls.append(url)
        return True

    monkeypatch.setattr("cad.webbrowser.open", mock_open)
    return opened_urls


@pytest.fixture(autouse=True)
def disable_live_detection(monkeypatch):
    """Skip the real pgrep/lsof/ps shellouts during tests. Each CLI test
    that calls into find_local_projects would otherwise launch ~3
    subprocesses per running claude on the developer's machine, which
    blows up the suite runtime and produces flaky results that depend on
    what's actually running locally. Tests that specifically exercise the
    live-detection helpers monkeypatch subprocess.run themselves and
    don't care about this env var.
    """
    monkeypatch.setenv("CAD_NO_LIVE", "1")
