"""Pytest configuration and fixtures for cad tests."""

import pytest


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
