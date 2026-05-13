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




class TestFindProjectForCwd:
    """The auto-pick helper that decides whether to skip the project
    picker when cad is launched inside a known project."""

    def _project(self, cwd):
        return {"name": Path(cwd).name, "cwd": cwd}

    def test_exact_match(self):
        import cad as ct

        projects = [self._project("/Users/x/Code/foo")]
        assert ct._find_project_for_cwd(projects, "/Users/x/Code/foo") is projects[0]

    def test_subdir_match_picks_deepest(self):
        import cad as ct

        a = self._project("/Users/x/Code")
        b = self._project("/Users/x/Code/foo")
        # Subdir of foo should resolve to foo (deepest), not Code.
        assert ct._find_project_for_cwd([a, b], "/Users/x/Code/foo/sub") is b

    def test_global_cwd_returns_none(self, monkeypatch):
        """Launching cad from ~/ or ~/Code shouldn't auto-pick the
        catch-all bucket — show the full project list instead."""
        import cad as ct

        monkeypatch.setattr(Path, "home", lambda: Path("/Users/x"))
        projects = [self._project("/Users/x"), self._project("/Users/x/Code/foo")]
        assert ct._find_project_for_cwd(projects, "/Users/x") is None
        assert ct._find_project_for_cwd(projects, "/Users/x/Code") is None

    def test_unknown_cwd_returns_none(self):
        import cad as ct

        projects = [self._project("/Users/x/Code/foo")]
        assert ct._find_project_for_cwd(projects, "/somewhere/else") is None

    def test_virtual_project_with_none_cwd_is_not_matched(self):
        """Global Sessions has cwd=None — it must never be auto-picked."""
        import cad as ct

        projects = [
            {"name": "Global Sessions", "cwd": None},
            self._project("/Users/x/Code/foo"),
        ]
        assert ct._find_project_for_cwd(projects, "/Users/x/Code/foo") is projects[1]




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
        import cad as ct

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





class TestGetProjectDisplayName:
    """Tests for get_project_display_name function."""

    def test_extracts_project_name_from_path(self):
        """Test extracting readable project name from encoded path."""
        assert get_project_display_name("-home-user-projects-myproject") == "myproject"

    def test_handles_nested_paths(self):
        """Test handling nested project paths."""
        assert get_project_display_name("-home-user-code-apps-webapp") == "apps-webapp"

    def test_handles_windows_style_paths(self):
        """Test handling Windows-style encoded paths."""
        assert get_project_display_name("-mnt-c-Users-name-Projects-app") == "app"

    def test_handles_simple_name(self):
        """Test handling already simple names."""
        assert get_project_display_name("simple-project") == "simple-project"




class TestFindAllSessions:
    """Tests for find_all_sessions function."""

    def test_finds_sessions_grouped_by_project(self, mock_projects_dir):
        """Test that sessions are found and grouped by project."""
        result = find_all_sessions(mock_projects_dir)

        # Should have 2 projects
        assert len(result) == 2

        # Check project names are extracted
        project_names = [p["name"] for p in result]
        assert "project-a" in project_names
        assert "project-b" in project_names

    def test_excludes_agent_files_by_default(self, mock_projects_dir):
        """Test that agent-* files are excluded by default."""
        result = find_all_sessions(mock_projects_dir)

        # Find project-a
        project_a = next(p for p in result if p["name"] == "project-a")

        # Should have 2 sessions (not 3, agent excluded)
        assert len(project_a["sessions"]) == 2

        # No session should be an agent file
        for session in project_a["sessions"]:
            assert not session["path"].name.startswith("agent-")

    def test_includes_agent_files_when_requested(self, mock_projects_dir):
        """Test that agent-* files can be included."""
        result = find_all_sessions(mock_projects_dir, include_agents=True)

        # Find project-a
        project_a = next(p for p in result if p["name"] == "project-a")

        # Should have 3 sessions (including agent)
        assert len(project_a["sessions"]) == 3

    def test_excludes_warmup_sessions(self, mock_projects_dir):
        """Test that warmup sessions are excluded."""
        result = find_all_sessions(mock_projects_dir)

        # Find project-b
        project_b = next(p for p in result if p["name"] == "project-b")

        # Should have 1 session (warmup excluded)
        assert len(project_b["sessions"]) == 1

    def test_sessions_sorted_by_date(self, mock_projects_dir):
        """Test that sessions within a project are sorted by modification time."""
        result = find_all_sessions(mock_projects_dir)

        for project in result:
            sessions = project["sessions"]
            if len(sessions) > 1:
                # Check descending order (most recent first)
                for i in range(len(sessions) - 1):
                    assert sessions[i]["mtime"] >= sessions[i + 1]["mtime"]

    def test_returns_empty_for_nonexistent_folder(self):
        """Test handling of non-existent folder."""
        result = find_all_sessions(Path("/nonexistent/path"))
        assert result == []

    def test_session_includes_summary(self, mock_projects_dir):
        """Test that sessions include summary text."""
        result = find_all_sessions(mock_projects_dir)

        project_a = next(p for p in result if p["name"] == "project-a")

        for session in project_a["sessions"]:
            assert "summary" in session
            assert session["summary"] != "(no summary)"
