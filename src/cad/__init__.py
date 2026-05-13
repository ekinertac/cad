"""cad — Coding Agent Driver.

A single CLI for managing and resuming sessions across multiple local
coding agents (claude, codex, pi, opencode, forge). Originally grew out
of Simon Willison's claude-code-transcripts HTML renderer, which still
lives inside as the `json` / `all` / `web` subcommands.
"""

import json
import html
import os
import platform
import re
import contextlib
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
import webbrowser
from datetime import datetime
from pathlib import Path

import click
from click_default_group import DefaultGroup
import httpx
from jinja2 import Environment, PackageLoader
import markdown
import questionary

# Session parsing / transcript extraction lives in core/session_model.py.
# Re-exported for test compatibility and any callers reaching in.
from .core.session_model import (  # noqa: E402,F401
    _extract_role_text,
    _extract_summarizable_text,
    _flatten_content_blocks,
    _get_jsonl_summary,
    _parse_jsonl_file,
    _read_session_excerpt_for_summary,
    extract_text_from_content,
    get_claude_session_metadata,
    get_session_cwd,
    get_session_summary,
    get_session_transcript,
    parse_session_file,
)

# API constants
API_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# Picker + prompts moved to core/picker.py.
from .core.picker import (  # noqa: E402,F401
    prompt_confirm,
    prompt_for_cwd,
    prompt_for_title,
    select_entry,
    select_session_action,
)

# HTML render pipeline moved to features/html/. Re-exported so the
# extensive test imports (`from cad import generate_html, render_*, …`)
# keep resolving.
from .features.html import (  # noqa: E402,F401
    CSS,
    GIST_PREVIEW_JS,
    GITHUB_REPO_PATTERN,
    JS,
    LONG_TEXT_THRESHOLD,
    PROMPTS_PER_PAGE,
    analyze_conversation,
    create_gist,
    detect_github_repo,
    fetch_url_to_tempfile,
    format_json,
    format_tool_stats,
    generate_batch_html,
    generate_html,
    generate_html_from_session_data,
    generate_index_pagination_html,
    generate_pagination_html,
    get_template,
    inject_gist_preview_js,
    is_json_like,
    is_tool_result_message,
    is_url,
    make_msg_id,
    render_assistant_message,
    render_bash_tool,
    render_content_block,
    render_edit_tool,
    render_markdown_text,
    render_message,
    render_todo_write,
    render_user_message_content,
    render_write_tool,
)

# Provider abstraction lives in core/providers.py — re-exported so legacy
# imports (`from cad import resume_session` etc.) still resolve.
from .core.providers import (  # noqa: E402,F401
    PROVIDER_BADGES,
    PROVIDER_NEW_COMMANDS,
    PROVIDER_RESUME_COMMANDS,
    new_session,
    resume_session,
)

# `command cad` in the wrapper skips this function so the binary runs once.
# zsh and bash get separate snippets only because the conditional syntax
# differs slightly; the mechanism is identical.

def peek_session(session):
    """Render the session's prompts/replies to a temp markdown file and
    open it in ``$PAGER`` (fallback ``less``). Blocks until the user
    quits the pager, then unlinks the temp file. less uses the alternate
    screen so cad's terminal state is restored on exit — Quick Look-style.

    Opens at the *bottom* of the file (most recent turns) since that's
    what's relevant for an in-progress live session. Scroll up to see
    earlier history.
    """
    transcript = get_session_transcript(session)
    if not transcript:
        click.echo(f"Peek not yet supported for {session['provider']} sessions.")
        return

    lines = [
        f"# {session['provider']} session {session['session_id']}",
        f"# {session['cwd']}",
        "",
    ]
    for role, text in transcript:
        lines.append(f"## {role.capitalize()}")
        lines.append("")
        lines.append(text)
        lines.append("")

    fd, path = tempfile.mkstemp(prefix="cad-peek-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        pager = os.environ.get("PAGER") or "less"
        args = [pager]
        # less-specific flags: `-R` passes ANSI colours, `+G` opens at
        # end of file so the user sees the most recent turns first.
        # Skip if $PAGER is something else (we can't assume its flags).
        if Path(pager).name == "less":
            args += ["-R", "+G"]
        args.append(path)
        try:
            subprocess.run(args)
        except FileNotFoundError:
            click.echo(f"Pager not found: {pager}", err=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

def summarize_session(session):
    """Generate a short title for the session by piping an excerpt to the
    locally-installed ``codex`` CLI in non-interactive mode.

    Codex over a ChatGPT account is the cheapest path here: ``claude -p``
    insists on the Anthropic API even when the Claude Code subscription
    is active, so users with depleted API credits can't use it. Codex
    exec uses the same auth as their interactive codex sessions. The
    final stdout line is the agent's reply (codex prints framing info on
    stderr that we discard).

    Raises :class:`click.ClickException` on timeout, missing binary,
    non-zero exit, or empty output.
    """
    excerpt = _read_session_excerpt_for_summary(session)
    if not excerpt:
        raise click.ClickException("Could not read session content to summarize.")

    prompt = (
        "Generate a concise 3-7 word title that captures what this session "
        "was about. Respond with the title only — no quotes, no trailing "
        "punctuation, no preamble.\n\n"
        f"<session>\n{excerpt}\n</session>"
    )

    try:
        result = subprocess.run(
            # --ephemeral keeps the throwaway summarization call from
            # leaving a session file under ~/.codex/sessions, which would
            # otherwise pollute cct's own session list.
            ["codex", "exec", "--ephemeral", prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise click.ClickException(
            "`codex` not found on PATH — summarize uses it as the LLM."
        )
    except subprocess.TimeoutExpired:
        raise click.ClickException("Summarize timed out after 60s.")

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise click.ClickException(
            f"codex exited with code {result.returncode}: {stderr[-200:]}"
        )

    # Take the last non-empty line — codex's reply is the final stdout chunk,
    # any preceding lines are typically status output. Strip wrapping quotes
    # the model sometimes adds despite the instruction.
    lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
    title = (lines[-1] if lines else "").strip("\"'").strip()
    if not title:
        raise click.ClickException("codex returned an empty title.")
    return title

# Shell wrapper snippets moved to features/shell_init/.
from .features.shell_init import SHELL_WRAPPERS  # noqa: E402,F401

# Temp output constants + helpers live in core/util.py now. Re-export
# the module-level names for any legacy callers (and tests) that import
# them from the top-level cad namespace.
from .core.util import (  # noqa: E402,F401
    TEMP_OUTPUT_KEEP,
    TEMP_OUTPUT_PARENT,
    _atomic_write_json,
    _loading_message,
    _prune_temp_outputs,
    _temp_output_dir,
)

# Per-provider discovery lives in core/discovery.py — re-exported for
# legacy import paths and tests.
from .core.discovery import (  # noqa: E402,F401
    _FORGE_CWD_RE,
    _is_claude_queue_operation_session,
    find_claude_sessions,
    find_codex_sessions,
    find_forge_sessions,
    find_local_sessions,
    find_opencode_sessions,
    find_pi_sessions,
    get_codex_summary,
    get_pi_summary,
)

# Overrides + project grouping moved to core/. The `find_local_projects`
# shim here injects the live annotator so existing callers (which expect
# live indicators in the project list) keep working — core/projects.py
# is deliberately ignorant of pgrep/lsof.
from .core.overrides import (  # noqa: E402,F401
    _apply_cwd_override,
    _cwd_overrides_file,
    _load_cwd_overrides,
    _load_titles,
    _migrate_legacy_sidecar_dir,
    _titles_file,
    get_cwd_override,
    get_title_override,
    save_cwd_override,
    save_title_override,
)
from .core.projects import (  # noqa: E402,F401
    _find_project_for_cwd,
    _global_session_cwds,
    _group_sessions_into_projects,
    find_all_sessions,
    get_project_display_name,
    load_session_summary,
)
from .core.projects import find_local_projects as _find_local_projects_core

# Live-mode helpers live in features/live/. Re-exported here so the
# existing test imports (`from cad import find_live_claude_state` etc.)
# keep resolving. The find_local_projects shim below injects the
# live annotator at call time so core/ stays oblivious to pgrep/lsof.
from .features.live import (  # noqa: E402,F401
    _annotate_sessions_with_live_state,
    _build_live_entries,
    default_annotator as _live_default_annotator,
    find_live_claude_state,
    focus_live_session,
)

def find_local_projects(folder=None):
    """Shim: call the core grouping function with the live annotator
    wired in. core/projects.py knows nothing about pgrep/lsof; this
    shim lives in __init__.py for backwards compatibility with every
    caller doing ``from cad import find_local_projects``."""
    return _find_local_projects_core(folder=folder, annotate_live=_live_default_annotator)

# Project-rename machinery moved to features/project_rename/.
from .features.project_rename import (  # noqa: E402,F401
    _CLAUDE_STATE_DIRS,
    _claude_encode_path,
    migrate_claude_project,
)

# claude-for-web API client + credentials + repo helpers moved to
# features/web/. Re-exported so the existing test imports continue
# to resolve at `from cad import resolve_credentials` etc.
from .features.web import (  # noqa: E402,F401
    ANTHROPIC_VERSION,
    API_BASE_URL,
    CredentialsError,
    enrich_sessions_with_repos,
    extract_repo_from_session,
    fetch_session,
    fetch_sessions,
    filter_sessions_by_repo,
    format_session_for_display,
    get_access_token_from_keychain,
    get_api_headers,
    get_org_uuid_from_config,
    resolve_credentials,
)

# detect_github_repo (used by the HTML render path on local sessions)
# is conceptually web/HTML but currently still consumed by code
# remaining in this file. Stays here until features/html is extracted.

@click.group(cls=DefaultGroup, default="local", default_if_no_args=True)
@click.version_option(None, "-v", "--version", package_name="cad")
def cli():
    """cad — Coding Agent Driver. Manage sessions across claude, codex,
    pi, opencode, and forge from one picker, or render Claude Code
    sessions to HTML."""
    pass

# Register feature commands. Each features/<name>/__init__.py exports a
# register(cli) hook so subcommands plug in here without __init__.py
# having to know the internals. To remove a feature: delete its
# directory and remove the corresponding register() call.
from .features import html as _html_feature  # noqa: E402
from .features import live as _live_feature  # noqa: E402
from .features import shell_init as _shell_init_feature  # noqa: E402
from .features import web as _web_feature  # noqa: E402

_live_feature.register(cli)
_shell_init_feature.register(cli)
_web_feature.register(cli)
_html_feature.register(cli)

@cli.command("local")
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Output directory. If not specified, writes to temp dir and opens in browser.",
)
@click.option(
    "-a",
    "--output-auto",
    is_flag=True,
    help="Auto-name output subdirectory based on session filename (uses -o as parent, or current dir).",
)
@click.option(
    "--repo",
    help="GitHub repo (owner/name) for commit links. Auto-detected from git push output if not specified.",
)
@click.option(
    "--gist",
    is_flag=True,
    help="Upload to GitHub Gist and output a gisthost.github.io URL.",
)
@click.option(
    "--json",
    "include_json",
    is_flag=True,
    help="Include the original JSONL session file in the output directory.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated index.html in your default browser (default if no -o specified).",
)
@click.option(
    "--all",
    "show_all_projects",
    is_flag=True,
    help="Skip the auto-pick: show the project picker even when launched inside a known project.",
)
def local_cmd(
    output, output_auto, repo, gist, include_json, open_browser, show_all_projects
):
    """Select a local agent session and either resume it or render to HTML.

    Sessions from claude (``~/.claude/projects/``) and codex
    (``~/.codex/sessions/``) are merged by their recorded ``cwd`` so a
    project entry shows the combined session count regardless of which
    agent CLI you used. Provider is shown via a small badge per row
    (``[c]`` / ``[x]``).

    Two-step picker. First choose a project (questionary, with
    type-to-filter). Then choose a session (custom picker):

    - Enter resumes — chdir to the recorded cwd and exec the right agent
      CLI (``claude --dangerously-skip-permissions --resume`` for claude,
      ``codex resume`` for codex).
    - h renders the session to HTML. Currently only claude sessions can
      be rendered; pressing h on a codex session prints a 'not yet
      supported' message and returns.

    Session summaries are loaded only after a project is picked, so the
    first picker stays cheap even with hundreds of sessions on disk.
    """
    with _loading_message("Loading projects..."):
        projects = find_local_projects()

    if not projects:
        click.echo("No local sessions found.")
        return

    # If launched from inside a known project, skip the project picker
    # and drop straight into that project's session list — the common
    # case. The user can press Esc/Bksp to back out to the full list, or
    # pass --all to bypass the auto-pick entirely.
    auto_pick = None
    if not show_all_projects:
        auto_pick = _find_project_for_cwd(projects, str(Path.cwd()))

    # Outer loop covers project → session → (back) → project navigation.
    # Esc/Bksp on the session picker returns the user here instead of
    # quitting; q on either picker still hard-quits.
    project_idx = 0
    while True:
        # Project picker uses the same custom picker as the session step
        # so the search UX is consistent (`/` opens search in both). No
        # back_action: Esc here means quit. `r` bulk-moves every session
        # in a project to a new cwd — for when you've renamed the folder
        # on disk (`mv ~/Code/foo ~/Code/bar`) and want every session to
        # point at the new location in one go.
        if auto_pick is not None:
            # First iteration only: cad was launched inside a known
            # project. Skip the picker and drop straight into its
            # sessions. Clear the auto-pick so back-navigation goes to
            # the full project picker as expected.
            selected_project = auto_pick
            project_action = "open"
            try:
                project_idx = projects.index(auto_pick)
            except ValueError:
                project_idx = 0
            auto_pick = None
            click.echo(
                f"Auto-opening project at {selected_project['cwd']} "
                f"(Esc to see all, --all to skip auto-pick)"
            )
        else:
            picked = select_entry(
                projects,
                actions={"enter": "open", "n": "new", "r": "rename"},
                initial_selected=project_idx,
            )
            if picked is None:
                click.echo("No project selected.")
                return
            selected_project, project_action = picked
            try:
                project_idx = projects.index(selected_project)
            except ValueError:
                project_idx = 0

        if project_action == "new":
            # Start a fresh claude session in this project's cwd. Doesn't
            # pick up the virtual Global Sessions entry — there's no
            # canonical cwd for it.
            cwd = selected_project["cwd"]
            if not cwd:
                click.echo(
                    "Can't start a new session in the virtual Global "
                    "Sessions entry — no canonical cwd.",
                    err=True,
                )
                continue
            # Replaces the current process — does not return on success.
            new_session(cwd)
            return

        if project_action == "rename":
            # Full project rename. cad handles every step so the user
            # never has to do a manual `mv` and then track it across
            # claude state dirs. Sequence: prompt → confirm → backup →
            # mv user folder → migrate claude state dirs → rewrite cwd
            # in JSONLs → clear sidecar overrides (no longer needed).
            project_sessions = selected_project["sessions"]
            n = len(project_sessions)
            old_cwd = selected_project["cwd"]

            if not old_cwd:
                click.echo(
                    "Rename not supported for the virtual Global Sessions entry.",
                    err=True,
                )
                continue

            new_cwd = prompt_for_cwd(
                default=old_cwd, must_exist=False, label="Rename to"
            )
            if new_cwd is None:
                continue  # Ctrl-C / EOF
            if not new_cwd:
                click.echo("Empty path — cancelled (no override changes made).")
                continue
            if new_cwd == old_cwd:
                click.echo("Same path — nothing to do.")
                continue

            # Spell out exactly what's about to happen so the user can
            # bail on the last yes/no rather than discovering surprises.
            providers_in_project = sorted({s["provider"] for s in project_sessions})
            non_claude = [p for p in providers_in_project if p != "claude"]
            click.echo()
            click.echo("About to rename project:")
            click.echo(f"  fs mv:    {old_cwd}  →  {new_cwd}")
            click.echo(f"  claude:   migrate state dirs, rewrite cwd in {n} JSONL(s)")
            if non_claude:
                click.echo(
                    f"  others:   {', '.join(non_claude)} sessions stay where they are"
                )
                click.echo(
                    "            (cad's sidecar override will point them at the new cwd)"
                )
            click.echo(f"  backup:   ~/.cad/agent-backups/claude-migrate-<ts>/")
            if not prompt_confirm("Proceed?"):
                click.echo("Cancelled.")
                continue

            try:
                # Phase 1: backup + migrate claude on-disk state. Do this
                # before the user-side mv so backups live in ~/.cad/ even
                # if the user-side mv fails.
                migration = migrate_claude_project(
                    old_cwd,
                    new_cwd,
                    backup_root=Path.home() / ".cad" / "agent-backups",
                )

                # Phase 2: mv the user's actual project directory.
                old_path = Path(old_cwd)
                new_path = Path(new_cwd)
                if old_path.exists():
                    shutil.move(str(old_path), str(new_path))
                elif not new_path.exists():
                    # User-side directory was already gone (e.g., they
                    # nuked it during testing). Warn but don't fail —
                    # the claude state migration may still be useful.
                    click.echo(
                        f"Note: {old_path} didn't exist; skipped fs mv.",
                        err=True,
                    )

                # Phase 3: for non-claude providers, fall back to the
                # sidecar override (their storage isn't path-encoded so
                # there's nothing to move on disk; cad just needs to
                # know the new cwd).
                for s in project_sessions:
                    if s["provider"] == "claude":
                        # Claude no longer needs the sidecar — its JSONLs
                        # now record the new cwd. Clear any stale
                        # override so source-of-truth is the JSONL.
                        save_cwd_override(s["provider"], s["session_id"], "")
                    else:
                        save_cwd_override(s["provider"], s["session_id"], new_cwd)

                click.echo()
                click.echo(f"Renamed {selected_project['name']} → {Path(new_cwd).name}")
                click.echo(f"  claude state dirs moved: {len(migration['moved_dirs'])}")
                click.echo(
                    f"  JSONL cwds rewritten:    {len(migration['rewritten_files'])}"
                )
                if migration["backup_dir"]:
                    click.echo(f"  backup:                  {migration['backup_dir']}")
                if migration["skipped"]:
                    click.echo("  skipped (collisions):")
                    for s in migration["skipped"]:
                        click.echo(f"    {s}")
            except (OSError, click.ClickException) as e:
                click.echo(f"Migration failed: {e}", err=True)
                click.echo(
                    "Backup (if any) is at ~/.cad/agent-backups/. "
                    "Inspect ~/.claude/projects/ and the new path before retrying.",
                    err=True,
                )
                continue

            # Re-discover so the picker reflects the new state, then jump
            # cursor to the new project.
            projects = find_local_projects()
            project_idx = next(
                (i for i, p in enumerate(projects) if p["cwd"] == new_cwd),
                0,
            )
            continue

        sessions = selected_project["sessions"]
        if not sessions:
            click.echo(f"No sessions in {selected_project['name']}.")
            # Bounce back to project picker — empty project is recoverable.
            continue

        # Hydrate summaries only now (after the project pick), so opening
        # the project picker stays cheap regardless of total session count.
        for s in sessions:
            load_session_summary(s)

        # Inner loop: r/s/m/p actions update a title/cwd / open the pager
        # and stay on the session picker; back returns to the outer loop.
        # selected_idx is preserved across iterations so re-rendered
        # pickers come back to the same row (Quick Look style).
        went_back = False
        selected_idx = 0
        while True:
            picked = select_entry(
                sessions,
                actions={
                    "enter": "resume",
                    "n": "new",
                    "h": "html",
                    "r": "rename",
                    "s": "summarize",
                    "m": "move",
                    "p": "peek",
                },
                back_action="back",
                initial_selected=selected_idx,
            )
            if picked is None:
                # q or Ctrl-C — hard quit.
                click.echo("No session selected.")
                return

            session, action = picked

            if action == "back":
                went_back = True
                break

            # Remember which row was active so the next re-entry of the
            # picker starts on the same session.
            try:
                selected_idx = sessions.index(session)
            except ValueError:
                selected_idx = 0

            if action == "peek":
                peek_session(session)
                continue

            if action == "new":
                # Start a fresh claude session in this project's cwd.
                # Replaces the process — does not return on success.
                new_session(selected_project["cwd"])
                return

            if action == "rename":
                new_title = prompt_for_title(default=session.get("summary") or "")
                if new_title is None:  # Ctrl-C / EOF
                    continue
                save_title_override(
                    session["provider"], session["session_id"], new_title
                )
                session["summary"] = new_title or None
                session["display"] = None
                session["_recently_updated"] = True
                load_session_summary(session)
                continue

            if action == "summarize":
                click.echo(f"Summarizing {session['session_id']}...")
                try:
                    title = summarize_session(session)
                except click.ClickException as e:
                    click.echo(f"Summarize failed: {e.message}", err=True)
                    continue
                save_title_override(session["provider"], session["session_id"], title)
                session["summary"] = title
                session["display"] = None
                session["_recently_updated"] = True
                load_session_summary(session)
                click.echo(f"Saved title: {title}")
                continue

            if action == "move":
                new_cwd = prompt_for_cwd(default=session.get("cwd") or "")
                if new_cwd is None:  # cancel
                    continue
                save_cwd_override(session["provider"], session["session_id"], new_cwd)
                session["cwd"] = new_cwd or session["cwd"]
                session["display"] = None
                session["_recently_updated"] = True
                load_session_summary(session)
                verb = "Moved" if new_cwd else "Cleared override for"
                click.echo(f"{verb} session to {new_cwd or session['cwd']}")
                continue

            if action == "resume":
                # Replaces the current process — does not return.
                resume_session(session)
                return

            # action == "html" — break out, fall through to render.
            break

        if went_back:
            # Re-enter outer loop = project picker.
            continue
        # Fell through with action == "html". Exit outer loop too.
        break

    # action == "html"
    if session["provider"] != "claude":
        click.echo(f"HTML render not supported for {session['provider']} sessions yet.")
        return

    session_file = session["filepath"]
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        parent_dir = Path(output) if output else Path(".")
        output = parent_dir / session_file.stem
    elif output is None:
        output = _temp_output_dir(f"claude-session-{session_file.stem}")

    output = Path(output)
    generate_html(session_file, output, github_repo=repo)

    # Show output directory
    click.echo(f"Output: {output.resolve()}")

    # Copy JSONL file to output directory if requested
    if include_json:
        output.mkdir(exist_ok=True)
        json_dest = output / session_file.name
        shutil.copy(session_file, json_dest)
        json_size_kb = json_dest.stat().st_size / 1024
        click.echo(f"JSONL: {json_dest} ({json_size_kb:.1f} KB)")

    if gist:
        # Inject gist preview JS and create gist
        inject_gist_preview_js(output)
        click.echo("Creating GitHub gist...")
        gist_id, gist_url = create_gist(output)
        preview_url = f"https://gisthost.github.io/?{gist_id}/index.html"
        click.echo(f"Gist: {gist_url}")
        click.echo(f"Preview: {preview_url}")

    if open_browser or auto_open:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)

def main():
    cli()
