"""
features/local/command.py — the ``cad local`` click subcommand.

The everyday two-step picker: project → session → action. Driven by
:func:`cad.core.picker.select_entry` with per-key action dicts.

Reaches into many other modules via the top-level ``cad`` namespace at
call time so that existing test ``monkeypatch.setattr(cad, "X", …)``
patches keep working. The functions involved:

- ``select_entry``, ``prompt_for_title``, ``prompt_for_cwd``,
  ``prompt_confirm`` (core/picker)
- ``find_local_projects``, ``load_session_summary``,
  ``_find_project_for_cwd`` (core/projects)
- ``save_title_override``, ``save_cwd_override`` (core/overrides)
- ``new_session``, ``resume_session`` (core/providers)
- ``migrate_claude_project`` (features/project_rename)
- ``peek_session``, ``summarize_session`` (local/actions)
- ``generate_html``, ``inject_gist_preview_js``, ``create_gist``
  (features/html)
- ``_temp_output_dir``, ``_loading_message`` (core/util)

Pulling them all through ``cad.__dict__`` keeps this file independent
of internal module reshuffles too — only the public names matter.
"""

import shutil
import webbrowser
from pathlib import Path

import click


def _lookup(name):
    """Resolve ``name`` on the top-level ``cad`` module at call time.

    Centralised so swapping the indirection (or eventually removing
    it when tests are rewritten to patch the canonical locations)
    only touches one helper.
    """
    from ... import __dict__ as cad_ns

    return cad_ns[name]


@click.command("local")
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
    # All shared helpers resolved through cad.* at call time — see the
    # module docstring for why.
    _loading_message = _lookup("_loading_message")
    find_local_projects = _lookup("find_local_projects")
    _find_project_for_cwd = _lookup("_find_project_for_cwd")
    select_entry = _lookup("select_entry")
    new_session = _lookup("new_session")
    prompt_for_cwd = _lookup("prompt_for_cwd")
    prompt_confirm = _lookup("prompt_confirm")
    migrate_claude_project = _lookup("migrate_claude_project")
    save_cwd_override = _lookup("save_cwd_override")
    load_session_summary = _lookup("load_session_summary")
    prompt_for_title = _lookup("prompt_for_title")
    save_title_override = _lookup("save_title_override")
    peek_session = _lookup("peek_session")
    summarize_session = _lookup("summarize_session")
    resume_session = _lookup("resume_session")
    generate_html = _lookup("generate_html")
    inject_gist_preview_js = _lookup("inject_gist_preview_js")
    create_gist = _lookup("create_gist")
    _temp_output_dir = _lookup("_temp_output_dir")

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
                    "d": "archive",
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

            if action == "archive":
                # Soft-delete: move the JSONL to ~/.cad/archive/. cad
                # archive can list and restore it; a plain mv works
                # too. Refuse for live sessions / non-claude providers.
                archive_session_fn = _lookup("archive_session")
                ArchiveError = _lookup("ArchiveError")
                if not prompt_confirm(
                    f"Archive session {session['session_id']}? "
                    "(reversible via `cad archive`)"
                ):
                    continue
                try:
                    dest = archive_session_fn(session)
                except ArchiveError as e:
                    click.echo(f"Archive failed: {e}", err=True)
                    continue
                click.echo(f"Archived to {dest}")
                # Drop from the in-memory list so the picker re-renders
                # without it. If the project is now empty, bounce back
                # to the project picker.
                try:
                    sessions.remove(session)
                except ValueError:
                    pass
                if not sessions:
                    went_back = True
                    break
                # Keep cursor on roughly the same row.
                selected_idx = min(selected_idx, len(sessions) - 1)
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
