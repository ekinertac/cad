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


class TestLiveCommand:
    """`cad live` is an interactive picker showing only live sessions,
    auto-refreshing every 2 seconds."""

    def _setup_live_fixture(self, tmp_path, monkeypatch):
        """Common scaffold: two real claude projects on disk with two
        sessions each, plus monkeypatch home and the live-detection
        helpers so we can pin which sessions look 'live'."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        for proj_name in ("alpha", "beta"):
            d = fake_home / ".claude" / "projects" / f"-Users-x-Code-{proj_name}"
            d.mkdir(parents=True)
            for sid in (f"{proj_name}-1", f"{proj_name}-2"):
                (d / f"{sid}.jsonl").write_text(
                    '{"type":"summary","summary":"x"}\n'
                    f'{{"type":"user","cwd":"/Users/x/Code/{proj_name}",'
                    f'"message":{{"content":"hi from {sid}"}}}}\n'
                )
        return fake_home

    def test_build_live_entries_filters_and_groups(self, tmp_path, monkeypatch):
        """_build_live_entries returns one entry per live session,
        sorted within each project by state priority."""
        import cad as ct

        self._setup_live_fixture(tmp_path, monkeypatch)

        monkeypatch.setattr(
            ct,
            "find_live_claude_state",
            lambda: {
                "bound_uuids": {
                    "alpha-1": {"pid": 1, "cwd": "/Users/x/Code/alpha"},
                    "alpha-2": {"pid": 2, "cwd": "/Users/x/Code/alpha"},
                    "beta-1": {"pid": 3, "cwd": "/Users/x/Code/beta"},
                },
                "unbound_cwds": {},
            },
        )

        def fake_annotate(sessions, _state, now=None):
            states = {
                "alpha-1": "working",
                "alpha-2": "idle",
                "beta-1": "input",
            }
            for s in sessions:
                s["state"] = states.get(s["session_id"], "idle")
                s["live"] = s["session_id"] in states

        monkeypatch.setattr(ct, "_annotate_sessions_with_live_state", fake_annotate)

        entries = ct._build_live_entries()
        # 3 live sessions + 2 project headers = 5 rows; beta-2 (not in
        # the states map) is excluded.
        session_entries = [e for e in entries if e.get("session_id")]
        assert len(session_entries) == 3
        sids = [e["session_id"] for e in session_entries]
        assert "beta-2" not in sids
        # Within the alpha group (sessions sitting after the alpha
        # header until the next header), working comes before idle.
        alpha_header_idx = next(
            i
            for i, e in enumerate(entries)
            if e.get("header") and e["display"].strip() == "alpha"
        )
        alpha_run = []
        for e in entries[alpha_header_idx + 1 :]:
            if e.get("header"):
                break
            alpha_run.append(e)
        assert alpha_run[0]["state"] == "working"
        assert alpha_run[1]["state"] == "idle"

    def test_build_live_entries_emits_project_headers(self, tmp_path, monkeypatch):
        """Visual grouping: each project group is led by a non-selectable
        header row (``header: True``) carrying the project name, followed
        by its indented session rows. Projects without live sessions emit
        no header."""
        import cad as ct

        self._setup_live_fixture(tmp_path, monkeypatch)

        monkeypatch.setattr(
            ct,
            "find_live_claude_state",
            lambda: {
                "bound_uuids": {
                    "alpha-1": {"pid": 1, "cwd": "/Users/x/Code/alpha"},
                    "alpha-2": {"pid": 2, "cwd": "/Users/x/Code/alpha"},
                    "beta-1": {"pid": 3, "cwd": "/Users/x/Code/beta"},
                },
                "unbound_cwds": {},
            },
        )

        def fake_annotate(sessions, _state, now=None):
            states = {"alpha-1": "working", "alpha-2": "idle", "beta-1": "input"}
            for s in sessions:
                s["state"] = states.get(s["session_id"], "idle")
                s["live"] = s["session_id"] in states

        monkeypatch.setattr(ct, "_annotate_sessions_with_live_state", fake_annotate)

        entries = ct._build_live_entries()
        # 2 project headers + 1 inter-group spacer + 3 sessions = 6 rows.
        assert len(entries) == 6
        headers = [e for e in entries if e.get("header") and e["display"].strip()]
        assert sorted(h["display"].strip() for h in headers) == ["alpha", "beta"]
        # The header sitting above alpha-1 should be the alpha header.
        alpha_one_idx = next(
            i for i, e in enumerate(entries) if e.get("session_id") == "alpha-1"
        )
        assert entries[alpha_one_idx - 1].get("header")
        assert entries[alpha_one_idx - 1]["display"].strip() == "alpha"
        # Session displays no longer carry the project name column —
        # the header handles that.
        for e in entries:
            if e.get("session_id"):
                assert "alpha " not in e["display"]
                assert "beta " not in e["display"]

    def test_resume_session_refuses_when_live(self, monkeypatch):
        """Bottom-of-stack guardrail: even if a caller manages to invoke
        resume_session on a live session, we refuse before exec'ing.
        Two agents on one JSONL = scrambled conversation. The check
        covers cad local, cad live, and any future callers."""
        import cad as ct

        called = {"execvp": False, "chdir": False}
        monkeypatch.setattr(
            ct.os,
            "execvp",
            lambda *a, **kw: called.__setitem__("execvp", True),
        )
        monkeypatch.setattr(
            ct.os,
            "chdir",
            lambda *a, **kw: called.__setitem__("chdir", True),
        )

        session = {
            "provider": "claude",
            "session_id": "abc-123",
            "cwd": "/tmp",
            "live": True,
        }
        ct.resume_session(session)
        assert called["execvp"] is False
        assert called["chdir"] is False

    def _wire_one_live_session(self, tmp_path, monkeypatch):
        """Common scaffolding for the Enter-behavior tests: one live
        session named alpha-1, picker stubbed to return its first
        non-header entry. Returns the captured-actions dict and the
        peek/resume call logs."""
        import cad as ct

        self._setup_live_fixture(tmp_path, monkeypatch)
        monkeypatch.setattr(
            ct,
            "find_live_claude_state",
            lambda: {
                "bound_uuids": {"alpha-1": {"pid": 1, "cwd": "/Users/x/Code/alpha"}},
                "unbound_cwds": {},
            },
        )

        def fake_annotate(sessions, _state, now=None):
            for s in sessions:
                s["live"] = s["session_id"] == "alpha-1"
                s["state"] = "working" if s["live"] else "idle"
                if s["live"]:
                    s["pid"] = 4242

        monkeypatch.setattr(ct, "_annotate_sessions_with_live_state", fake_annotate)

        captured = {"calls": 0}

        def fake_select(entries, **kwargs):
            # `cad live` now loops the picker — it stays open after
            # each Enter so the user can keep using it as a dashboard.
            # Simulate: pick on the first invocation, then quit (None)
            # on the second so the command terminates and the test
            # doesn't hang.
            captured["actions"] = kwargs.get("actions")
            captured["calls"] += 1
            if captured["calls"] > 1:
                return None
            first = next(e for e in entries if not e.get("header"))
            return (first, list(captured["actions"].values())[0])

        peek_calls = []
        resume_calls = []
        monkeypatch.setattr(ct, "select_entry", fake_select)
        monkeypatch.setattr(
            ct, "peek_session", lambda s: peek_calls.append(s["session_id"])
        )
        monkeypatch.setattr(
            ct, "resume_session", lambda s: resume_calls.append(s["session_id"])
        )
        return captured, peek_calls, resume_calls

    def test_live_command_enter_focuses_then_peeks(self, tmp_path, monkeypatch):
        """`cad live`'s Enter tries to bring the terminal tab running
        the session to the front. When focus succeeds we DON'T also
        peek — the user is now in the actual session. Resume is never
        called (two agents on one JSONL would corrupt it)."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        captured, peek_calls, resume_calls = self._wire_one_live_session(
            tmp_path, monkeypatch
        )
        focus_log = []

        def fake_focus(s):
            focus_log.append(s.get("session_id"))
            return True  # iTerm2 matched and switched

        monkeypatch.setattr(ct, "focus_live_session", fake_focus)

        result = CliRunner().invoke(cli, ["live"])
        assert result.exit_code == 0, result.output
        assert focus_log == ["alpha-1"]
        assert peek_calls == []  # didn't fall back
        assert resume_calls == []

    def test_live_command_stays_open_after_enter(self, tmp_path, monkeypatch):
        """`cad live` is a monitoring view, not a launcher: after
        Enter (whether focus succeeds or peek runs as fallback) the
        picker must re-open so the user can switch to another session
        without re-running the command. Verified by the picker being
        invoked twice — first pick returns a session, second returns
        None to terminate. If the loop were missing, the second call
        would never happen."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        captured, peek_calls, _ = self._wire_one_live_session(tmp_path, monkeypatch)
        monkeypatch.setattr(ct, "focus_live_session", lambda s: True)

        result = CliRunner().invoke(cli, ["live"])
        assert result.exit_code == 0, result.output
        # First call returned the session; second returned None and
        # broke the loop. Anything less than 2 means we exited after
        # the first Enter, which is the bug we're guarding against.
        assert captured["calls"] == 2
        # Focus handled it both times — peek shouldn't have run.
        assert peek_calls == []

    def test_live_command_enter_falls_back_to_peek_when_focus_fails(
        self, tmp_path, monkeypatch
    ):
        """When focus can't find the tab (unsupported terminal, no PID,
        no match), peek the session so Enter is never a silent no-op."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        captured, peek_calls, resume_calls = self._wire_one_live_session(
            tmp_path, monkeypatch
        )
        monkeypatch.setattr(ct, "focus_live_session", lambda s: False)

        result = CliRunner().invoke(cli, ["live"])
        assert result.exit_code == 0, result.output
        assert peek_calls == ["alpha-1"]
        assert resume_calls == []

    def test_live_command_passes_refresh_callback_to_picker(
        self, tmp_path, monkeypatch
    ):
        """The picker is wired with a refresh callback so state changes
        in the underlying processes show up without re-running."""
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        self._setup_live_fixture(tmp_path, monkeypatch)

        monkeypatch.setattr(
            ct,
            "find_live_claude_state",
            lambda: {
                "bound_uuids": {"alpha-1": {"pid": 1, "cwd": "/Users/x/Code/alpha"}},
                "unbound_cwds": {},
            },
        )

        def fake_annotate(sessions, _state, now=None):
            for s in sessions:
                s["live"] = s["session_id"] == "alpha-1"
                s["state"] = "working" if s["live"] else "idle"

        monkeypatch.setattr(ct, "_annotate_sessions_with_live_state", fake_annotate)

        captured = {}

        def fake_select(entries, **kwargs):
            captured["kwargs"] = kwargs
            captured["entries"] = list(entries)
            return None  # simulate user cancel

        monkeypatch.setattr(ct, "select_entry", fake_select)

        result = CliRunner().invoke(cli, ["live"])
        assert result.exit_code == 0, result.output
        assert "refresh_callback" in captured["kwargs"]
        assert captured["kwargs"]["refresh_callback"] is ct._build_live_entries
        # 1 header + 1 session.
        session_entries = [e for e in captured["entries"] if e.get("session_id")]
        assert len(session_entries) == 1
        assert session_entries[0]["session_id"] == "alpha-1"

    def test_live_command_says_so_when_nothing_is_live(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from cad import cli
        import cad as ct

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        proj = fake_home / ".claude" / "projects" / "-Users-x-Code-foo"
        proj.mkdir(parents=True)
        (proj / "abc.jsonl").write_text(
            '{"type":"user","cwd":"/Users/x/Code/foo",' '"message":{"content":"hi"}}\n'
        )
        monkeypatch.setattr(
            ct,
            "find_live_claude_state",
            lambda: {"bound_uuids": {}, "unbound_cwds": {}},
        )

        result = CliRunner().invoke(cli, ["live"])
        assert result.exit_code == 0
        assert "No live agent sessions" in result.output


class TestFocusLiveSession:
    """`focus_live_session` switches the terminal to whichever tab is
    running the live claude process. Right now only iTerm2 is supported;
    other terminals return False so the caller can fall back to peek."""

    def test_returns_false_when_session_has_no_pid(self, monkeypatch):
        """Unbound (cwd-matched) sessions have no specific PID, so we
        can't identify which tty they're on. Caller must fall back."""
        import cad as ct

        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
        assert ct.focus_live_session({"pid": None}) is False

    def test_returns_false_outside_iterm2(self, monkeypatch):
        """No supported terminal integration → caller falls back."""
        import cad as ct

        monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
        assert ct.focus_live_session({"pid": 1234}) is False

    def test_returns_false_when_term_program_unset(self, monkeypatch):
        """Headless / ssh / cron contexts have no TERM_PROGRAM."""
        import cad as ct

        monkeypatch.delenv("TERM_PROGRAM", raising=False)
        assert ct.focus_live_session({"pid": 1234}) is False

    def test_iterm2_runs_osascript_with_resolved_tty(self, monkeypatch):
        """iTerm2 path: resolve PID→tty via ps, then run an osascript
        that selects the iTerm2 session whose tty property matches.
        The actual AppleScript text isn't asserted (it can evolve); we
        just verify the tty is in the script and osascript was invoked."""
        import cad as ct

        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
        ran = {}

        def fake_run(cmd, *args, **kwargs):
            class R:
                returncode = 0
                stdout = ""

            if cmd[:2] == ["ps", "-p"]:
                R.stdout = "ttys004\n"
                return R()
            if cmd and cmd[0] == "osascript":
                ran["cmd"] = cmd
                # AppleScript returns "ok" when it found and focused
                # the matching session.
                R.stdout = "ok\n"
                return R()
            return R()

        monkeypatch.setattr(ct.subprocess, "run", fake_run)
        assert ct.focus_live_session({"pid": 4242}) is True
        assert ran["cmd"][0] == "osascript"
        # The script is passed via -e; whatever shape it takes, the
        # tty must appear so iTerm2 has something to match against.
        script_text = " ".join(ran["cmd"])
        assert "/dev/ttys004" in script_text

    def test_iterm2_returns_false_when_tty_lookup_fails(self, monkeypatch):
        """If ps can't tell us the tty (process gone, weird system),
        skip the AppleScript dance — there's nothing to match on."""
        import cad as ct

        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")

        def fake_run(cmd, *args, **kwargs):
            class R:
                returncode = 1
                stdout = ""

            return R()

        monkeypatch.setattr(ct.subprocess, "run", fake_run)
        assert ct.focus_live_session({"pid": 4242}) is False


class TestLiveClaudeDetection:
    """Live-session detection: process inspection via pgrep+lsof+ps,
    plus the pure annotation step that maps detected processes onto
    discovered sessions."""

    def test_annotate_bound_session_via_resume_uuid(self):
        """When a claude process was started with --resume <uuid>, the
        matching session is marked live with state derived from mtime."""
        import cad as ct

        sessions = [
            {
                "provider": "claude",
                "session_id": "abc-123",
                "cwd": "/x",
                "mtime": 1_000.0,
            },
            {
                "provider": "claude",
                "session_id": "other",
                "cwd": "/x",
                "mtime": 999.0,
            },
        ]
        state = {
            "bound_uuids": {"abc-123": {"pid": 100, "cwd": "/x"}},
            "unbound_cwds": {},
        }
        # 5 seconds after mtime → "working" (within 10s window).
        ct._annotate_sessions_with_live_state(sessions, state, now=1_005.0)
        assert sessions[0]["live"] is True
        assert sessions[0]["state"] == "working"
        # The PID of the bound claude process must travel with the
        # session so downstream features (terminal-tab focus, kill,
        # etc.) can act on it without re-shelling out to pgrep.
        assert sessions[0]["pid"] == 100
        assert sessions[1]["live"] is False
        assert sessions[1]["state"] == "idle"
        assert sessions[1].get("pid") is None

    def test_input_state_when_mtime_is_stale(self):
        """Between 10s and 5min since last JSONL write = "input": the
        process is alive but the conversation has gone quiet, so claude
        is most likely sitting at the prompt waiting on the user."""
        import cad as ct

        sessions = [
            {
                "provider": "claude",
                "session_id": "abc-123",
                "cwd": "/x",
                "mtime": 1_000.0,
            }
        ]
        state = {
            "bound_uuids": {"abc-123": {"pid": 100, "cwd": "/x"}},
            "unbound_cwds": {},
        }
        ct._annotate_sessions_with_live_state(sessions, state, now=1_030.0)
        assert sessions[0]["live"] is True
        assert sessions[0]["state"] == "input"

    def test_idle_state_when_mtime_is_very_stale(self):
        """A live process that's been quiet for 5+ minutes is probably
        abandoned — classify as idle so the dashboard doesn't pretend
        anything's happening there."""
        import cad as ct

        sessions = [
            {
                "provider": "claude",
                "session_id": "abc-123",
                "cwd": "/x",
                "mtime": 1_000.0,
            }
        ]
        state = {
            "bound_uuids": {"abc-123": {"pid": 100, "cwd": "/x"}},
            "unbound_cwds": {},
        }
        # 10 minutes after last write → idle
        ct._annotate_sessions_with_live_state(sessions, state, now=1_600.0)
        assert sessions[0]["live"] is True
        assert sessions[0]["state"] == "idle"

    def test_unbound_cwd_binds_to_most_recent_jsonls(self):
        """A fresh `claude` (no --resume) gives us a live cwd but no
        UUID; the annotator binds to the N most-recent JSONLs in that
        cwd to fill in the gap."""
        import cad as ct

        # 3 sessions in the same cwd; the cwd has 2 fresh claudes.
        sessions = [
            {"provider": "claude", "session_id": "oldest", "cwd": "/x", "mtime": 100.0},
            {"provider": "claude", "session_id": "middle", "cwd": "/x", "mtime": 200.0},
            {"provider": "claude", "session_id": "newest", "cwd": "/x", "mtime": 300.0},
        ]
        state = {"bound_uuids": {}, "unbound_cwds": {"/x": 2}}
        ct._annotate_sessions_with_live_state(sessions, state, now=400.0)
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id["newest"]["live"] is True
        assert by_id["middle"]["live"] is True
        assert by_id["oldest"]["live"] is False

    def test_unbound_sessions_carry_pid(self):
        """Unbound sessions (fresh ``claude`` with no ``--resume`` arg)
        must still propagate their PID onto the session dict so
        downstream features (terminal-tab focus) can find the tab.
        Without this, hitting Enter on a freshly-started claude in
        ``cad live`` silently falls back to peek because focus has no
        pid to ``ps`` against."""
        import cad as ct

        sessions = [
            {"provider": "claude", "session_id": "newest", "cwd": "/x", "mtime": 300.0},
            {"provider": "claude", "session_id": "older", "cwd": "/x", "mtime": 200.0},
        ]
        state = {
            "bound_uuids": {},
            # Two live claudes in /x, with their actual pids.
            "unbound_cwds": {"/x": [4321, 9999]},
        }
        ct._annotate_sessions_with_live_state(sessions, state, now=400.0)
        by_id = {s["session_id"]: s for s in sessions}
        # PIDs assigned in iteration order to most-recent-first sessions.
        assert by_id["newest"]["live"] is True
        assert by_id["newest"]["pid"] in (4321, 9999)
        assert by_id["older"]["live"] is True
        assert by_id["older"]["pid"] in (4321, 9999)
        assert by_id["newest"]["pid"] != by_id["older"]["pid"]

    def test_non_claude_sessions_never_marked_live(self):
        """Codex/pi/opencode/forge process detection isn't wired yet —
        they stay idle regardless of state input."""
        import cad as ct

        sessions = [
            {"provider": "codex", "session_id": "abc", "cwd": "/x", "mtime": 1_000.0},
            {"provider": "pi", "session_id": "abc", "cwd": "/x", "mtime": 1_000.0},
        ]
        # Even if the state dict claimed these are live, the annotator
        # only acts on claude provider sessions.
        state = {
            "bound_uuids": {"abc": {"pid": 1, "cwd": "/x"}},
            "unbound_cwds": {"/x": 5},
        }
        ct._annotate_sessions_with_live_state(sessions, state, now=1_005.0)
        for s in sessions:
            assert s["live"] is False
            assert s["state"] == "idle"

    def test_find_live_claude_state_handles_missing_pgrep(self, monkeypatch):
        """If pgrep isn't on PATH (Windows etc.), helper returns the
        empty state without raising so the picker still works."""
        import cad as ct
        import subprocess

        def boom(*a, **kw):
            raise FileNotFoundError("pgrep not on PATH")

        monkeypatch.setattr(subprocess, "run", boom)
        assert ct.find_live_claude_state() == {
            "bound_uuids": {},
            "unbound_cwds": {},
        }

    def test_find_live_claude_state_parses_resume_uuid(self, monkeypatch):
        """Mock the three subprocess calls so we can verify argv parsing
        without a real claude process to inspect."""
        import cad as ct
        import subprocess

        # Override the test-suite default that skips live detection.
        monkeypatch.delenv("CAD_NO_LIVE", raising=False)

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            r = R()
            if cmd[:2] == ["pgrep", "-x"]:
                r.stdout = "42\n"
            elif cmd[0] == "lsof":
                r.stdout = (
                    "COMMAND PID USER FD TYPE DEVICE SIZE NODE NAME\n"
                    "claude  42  x    cwd  DIR    1,2   64  3   /Users/x/Code/foo\n"
                )
            elif cmd[:2] == ["ps", "-p"]:
                r.stdout = (
                    "claude --dangerously-skip-permissions "
                    "--resume deadbeef-1234-5678-9abc-deadbeef1234\n"
                )
            return r

        monkeypatch.setattr(subprocess, "run", fake_run)
        state = ct.find_live_claude_state()
        assert state["bound_uuids"] == {
            "deadbeef-1234-5678-9abc-deadbeef1234": {
                "pid": 42,
                "cwd": "/Users/x/Code/foo",
            }
        }
        assert state["unbound_cwds"] == {}
