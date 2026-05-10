"""Convert Claude Code session JSON to a clean mobile-friendly HTML page with pagination."""

import json
import html
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path

import click
from click_default_group import DefaultGroup
import httpx
from jinja2 import Environment, PackageLoader
import markdown
import questionary

# Set up Jinja2 environment
_jinja_env = Environment(
    loader=PackageLoader("claude_code_transcripts", "templates"),
    autoescape=True,
)

# Load macros template and expose macros
_macros_template = _jinja_env.get_template("macros.html")
_macros = _macros_template.module


def get_template(name):
    """Get a Jinja2 template by name."""
    return _jinja_env.get_template(name)


# Regex to match git commit output: [branch hash] message
COMMIT_PATTERN = re.compile(r"\[[\w\-/]+ ([a-f0-9]{7,})\] (.+?)(?:\n|$)")

# Regex to detect GitHub repo from git push output (e.g., github.com/owner/repo/pull/new/branch)
GITHUB_REPO_PATTERN = re.compile(
    r"github\.com/([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)/pull/new/"
)

PROMPTS_PER_PAGE = 5
LONG_TEXT_THRESHOLD = (
    300  # Characters - text blocks longer than this are shown in index
)


def extract_text_from_content(content):
    """Extract plain text from message content.

    Handles both string content (older format) and array content (newer format).

    Args:
        content: Either a string or a list of content blocks like
                 [{"type": "text", "text": "..."}, {"type": "image", ...}]

    Returns:
        The extracted text as a string, or empty string if no text found.
    """
    if isinstance(content, str):
        return content.strip()
    elif isinstance(content, list):
        # Extract text from content blocks of type "text"
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    texts.append(text)
        return " ".join(texts).strip()
    return ""


# Module-level variable for GitHub repo (set by generate_html)
_github_repo = None

# API constants
API_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


def get_session_summary(filepath, max_length=200):
    """Extract a human-readable summary from a session file.

    Supports both JSON and JSONL formats.
    Returns a summary string or "(no summary)" if none found.
    """
    filepath = Path(filepath)
    try:
        if filepath.suffix == ".jsonl":
            return _get_jsonl_summary(filepath, max_length)
        else:
            # For JSON files, try to get first user message
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            loglines = data.get("loglines", [])
            for entry in loglines:
                if entry.get("type") == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    text = extract_text_from_content(content)
                    if text:
                        if len(text) > max_length:
                            return text[: max_length - 3] + "..."
                        return text
            return "(no summary)"
    except Exception:
        return "(no summary)"


def _get_jsonl_summary(filepath, max_length=200):
    """Extract summary from JSONL file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    # First priority: summary type entries
                    if obj.get("type") == "summary" and obj.get("summary"):
                        summary = obj["summary"]
                        if len(summary) > max_length:
                            return summary[: max_length - 3] + "..."
                        return summary
                except json.JSONDecodeError:
                    continue

        # Second pass: find first non-meta user message
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if (
                        obj.get("type") == "user"
                        and not obj.get("isMeta")
                        and obj.get("message", {}).get("content")
                    ):
                        content = obj["message"]["content"]
                        text = extract_text_from_content(content)
                        if text and not text.startswith("<"):
                            if len(text) > max_length:
                                return text[: max_length - 3] + "..."
                            return text
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass

    return "(no summary)"


def get_session_cwd(jsonl_path):
    """Return the working directory the session was started in, by scanning
    the JSONL for the first event line that carries a ``cwd`` field. Used
    by the resume action so we ``chdir`` to the right project before exec'ing
    claude — far more reliable than decoding the lossy folder-name encoding
    (where a folder like ``-Users-x-Code-claude-code-transcripts`` could
    decode to several different real paths).

    Returns ``None`` if no event in the file has a ``cwd``. Malformed JSON
    lines are skipped so a partially-corrupted session still yields a path.
    """
    try:
        with open(jsonl_path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and d.get("cwd"):
                    return d["cwd"]
    except OSError:
        return None
    return None


def find_local_sessions(folder, limit=10):
    """Find recent JSONL session files in the given folder.

    Returns a list of (Path, summary) tuples sorted by modification time.
    Excludes agent files and warmup/empty sessions.

    Pass ``limit=None`` to return every matching session uncapped — used by
    the two-step ``local`` picker once a project has been chosen.
    """
    folder = Path(folder)
    if not folder.exists():
        return []

    results = []
    for f in folder.glob("**/*.jsonl"):
        if f.name.startswith("agent-"):
            continue
        summary = get_session_summary(f)
        # Skip boring/empty sessions
        if summary.lower() == "warmup" or summary == "(no summary)":
            continue
        results.append((f, summary))

    # Sort by modification time, most recent first
    results.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    return results[:limit]


def select_session_action(sessions):
    """Interactive session picker with two actions: ``resume`` (Enter) and
    ``html`` (h). Modal search is opened with ``/`` so plain typing can't
    coexist with ``h`` as a hotkey — the same pattern fzf/htop use.

    Each entry in ``sessions`` must be a dict with ``display`` (string) and
    ``filepath`` (Path). Returns ``(filepath, action)`` or ``None`` if the
    user cancelled. Built directly on prompt_toolkit because questionary's
    select doesn't expose per-key action binding.
    """
    from prompt_toolkit import Application
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.styles import Style

    state = {"selected": 0, "filter": "", "search_mode": False, "result": None}
    VISIBLE = 18

    def filtered_indices():
        if not state["filter"]:
            return list(range(len(sessions)))
        needle = state["filter"].lower()
        return [i for i, s in enumerate(sessions) if needle in s["display"].lower()]

    def viewport(indices):
        """Slice the filtered list to a window around the cursor and return
        (visible_indices, above_count, below_count) for scroll markers."""
        if not indices:
            return [], 0, 0
        # Clamp the cursor against the filtered list.
        if state["selected"] >= len(indices):
            state["selected"] = len(indices) - 1
        if state["selected"] < 0:
            state["selected"] = 0
        top = max(0, state["selected"] - VISIBLE // 2)
        top = min(top, max(0, len(indices) - VISIBLE))
        visible = indices[top : top + VISIBLE]
        return visible, top, max(0, len(indices) - top - len(visible))

    def get_list_text():
        indices = filtered_indices()
        if not indices:
            return [("class:hint", "  (no matches)\n")]
        visible, above, below = viewport(indices)
        out = []
        if above > 0:
            out.append(("class:hint", f"  ▲ {above} more above\n"))
        for src_i in visible:
            pos = indices.index(src_i)
            sel = pos == state["selected"]
            arrow = "» " if sel else "  "
            style = "class:selected" if sel else ""
            out.append((style, f"{arrow}{sessions[src_i]['display']}\n"))
        if below > 0:
            out.append(("class:hint", f"  ▼ {below} more below\n"))
        return out

    def get_status_text():
        if state["search_mode"]:
            return [
                (
                    "class:status",
                    f" /{state['filter']}▁  Enter=select · Esc=exit search\n",
                )
            ]
        return [
            (
                "class:status",
                " Enter=resume · h=transcript · /=search · Esc=cancel\n",
            )
        ]

    kb = KeyBindings()
    not_searching = Condition(lambda: not state["search_mode"])
    is_searching = Condition(lambda: state["search_mode"])

    @kb.add("up")
    def _(event):
        if state["selected"] > 0:
            state["selected"] -= 1

    @kb.add("down")
    def _(event):
        if state["selected"] < len(filtered_indices()) - 1:
            state["selected"] += 1

    @kb.add("pageup")
    def _(event):
        state["selected"] = max(0, state["selected"] - VISIBLE)

    @kb.add("pagedown")
    def _(event):
        state["selected"] = min(
            len(filtered_indices()) - 1, state["selected"] + VISIBLE
        )

    @kb.add("enter")
    def _(event):
        indices = filtered_indices()
        if not indices:
            return
        # Enter in search mode does both: confirm filter AND pick (resume).
        state["search_mode"] = False
        state["result"] = (
            sessions[indices[state["selected"]]]["filepath"],
            "resume",
        )
        event.app.exit()

    @kb.add("h", filter=not_searching)
    def _(event):
        indices = filtered_indices()
        if not indices:
            return
        state["result"] = (
            sessions[indices[state["selected"]]]["filepath"],
            "html",
        )
        event.app.exit()

    @kb.add("/", filter=not_searching)
    def _(event):
        state["search_mode"] = True
        state["filter"] = ""
        state["selected"] = 0

    @kb.add("escape", eager=True)
    def _(event):
        if state["search_mode"]:
            state["search_mode"] = False
            state["filter"] = ""
            state["selected"] = 0
        else:
            event.app.exit()

    @kb.add("c-c", eager=True)
    def _(event):
        event.app.exit()

    @kb.add("backspace", filter=is_searching)
    def _(event):
        if state["filter"]:
            state["filter"] = state["filter"][:-1]
            state["selected"] = 0

    @kb.add("<any>", filter=is_searching)
    def _(event):
        char = event.key_sequence[0].data
        if len(char) == 1 and char.isprintable():
            state["filter"] += char
            state["selected"] = 0

    layout = Layout(
        HSplit(
            [
                Window(
                    content=FormattedTextControl(text=get_list_text),
                    height=Dimension(min=1, preferred=VISIBLE + 2, max=VISIBLE + 2),
                ),
                Window(content=FormattedTextControl(text=get_status_text), height=1),
            ]
        )
    )
    style = Style.from_dict(
        {
            "selected": "reverse",
            "hint": "fg:ansibrightblack",
            "status": "fg:ansicyan",
        }
    )
    Application(layout=layout, key_bindings=kb, style=style, full_screen=False).run()
    return state["result"]


def resume_session(jsonl_path):
    """Replace this process with ``claude --dangerously-skip-permissions
    --resume <session-id>`` after chdir'ing to the cwd recorded in the JSONL.
    Skip-permissions is intentional: permission prompts in long sessions
    devolve into rubber-stamping (alarm fatigue). Never returns on success.

    A child process can't change its parent shell's working directory.
    To make the shell end up in the project folder after claude exits,
    the user installs the wrapper printed by ``cct shell-init``: it sets
    ``CCT_CWD_FILE`` to a temp path; we write the target cwd there before
    exec'ing claude, and the shell function reads it after exit and cd's.
    """
    cwd = get_session_cwd(jsonl_path)
    if cwd is None:
        click.echo(
            f"Could not recover working directory from {jsonl_path.name}.",
            err=True,
        )
        sys.exit(1)
    if not Path(cwd).is_dir():
        click.echo(f"Original project directory no longer exists: {cwd}", err=True)
        sys.exit(1)
    session_id = jsonl_path.stem
    click.echo(f"Resuming {session_id} in {cwd}...")

    # Pass cwd back to the parent shell via the wrapper's temp file (if any).
    # Don't fail the resume if writing the breadcrumb fails — claude still
    # runs; the user just won't get the post-exit cd.
    cwd_file = os.environ.get("CCT_CWD_FILE")
    if cwd_file:
        try:
            Path(cwd_file).write_text(cwd)
        except OSError:
            pass

    os.chdir(cwd)
    os.execvp(
        "claude",
        ["claude", "--dangerously-skip-permissions", "--resume", session_id],
    )


# `command cct` in the wrapper skips this function so the binary runs once.
# zsh and bash get separate snippets only because the conditional syntax
# differs slightly; the mechanism is identical.
SHELL_WRAPPERS = {
    "zsh": """\
cct() {
  local cwd_file
  cwd_file=$(mktemp -t cct-cwd.XXXXXX)
  CCT_CWD_FILE="$cwd_file" command cct "$@"
  local rc=$?
  if [[ -s "$cwd_file" ]]; then
    cd "$(< "$cwd_file")" || true
  fi
  rm -f "$cwd_file"
  return $rc
}
""",
    "bash": """\
cct() {
  local cwd_file
  cwd_file=$(mktemp -t cct-cwd.XXXXXX)
  CCT_CWD_FILE="$cwd_file" command cct "$@"
  local rc=$?
  if [ -s "$cwd_file" ]; then
    cd "$(cat "$cwd_file")" || true
  fi
  rm -f "$cwd_file"
  return $rc
}
""",
}


TEMP_OUTPUT_PARENT = "claude-code-transcripts"
TEMP_OUTPUT_KEEP = 20


def _temp_output_dir(stem):
    """Return a per-session temp output dir under a single shared parent
    (``$TMPDIR/claude-code-transcripts/``). Old sibling dirs beyond
    :data:`TEMP_OUTPUT_KEEP` are pruned so the user's tmp folder doesn't
    accumulate transcripts indefinitely between system-level temp cleanups.
    """
    parent = Path(tempfile.gettempdir()) / TEMP_OUTPUT_PARENT
    parent.mkdir(parents=True, exist_ok=True)
    _prune_temp_outputs(parent, keep=TEMP_OUTPUT_KEEP)
    return parent / stem


def _prune_temp_outputs(parent, keep):
    """Delete all but the ``keep`` most-recently-modified subdirectories of
    ``parent``. Best-effort: tolerates missing parents and unreadable
    entries so a cleanup hiccup never blocks the user's render.
    """
    try:
        children = [c for c in parent.iterdir() if c.is_dir()]
    except (OSError, FileNotFoundError):
        return
    if len(children) <= keep:
        return
    children.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in children[keep:]:
        try:
            shutil.rmtree(old)
        except OSError:
            pass


def _global_session_folder_names():
    """Raw folder names that should merge into the virtual 'Global Sessions'
    project. Currently the user's home dir and ~/Code — these are catch-all
    locations for one-off questions, not real project folders. Computed from
    Path.home() so the rule generalises across machines.
    """
    home_encoded = str(Path.home()).replace("/", "-")
    return {home_encoded, f"{home_encoded}-Code"}


def find_local_projects(folder):
    """Discover top-level project folders under ``folder`` for the two-step
    picker. Reads only filesystem metadata — never opens session files — so
    the project picker stays cheap regardless of how many sessions exist.

    Returns a list of dicts sorted by ``latest_mtime`` descending::

        {
            "name":         "foo",                       # display name via get_project_display_name
            "raw_name":     "-Users-x-Code-foo",         # unmodified folder name (None for virtual)
            "path":         Path(...),                   # the project folder (None for virtual)
            "paths":        [Path(...), ...],            # always populated; multi-element for virtual
            "session_count": 3,                          # JSONLs excluding agent-*
            "latest_mtime": 1700000000.0,
            "display":      "foo                  2026-05-10 14:02   3 sessions",
        }

    Folders matching :func:`_global_session_folder_names` (home and ~/Code)
    do not appear as separate entries — their sessions are aggregated under
    a single virtual project named "Global Sessions". When two real projects
    collapse to the same display name, both rows get a trailing
    ``  (raw_name)`` disambiguator so the user can tell them apart.
    """
    folder = Path(folder)
    if not folder.exists():
        return []

    global_names = _global_session_folder_names()
    projects = []
    global_paths = []
    global_total_sessions = 0
    global_latest_mtime = 0.0

    for child in folder.iterdir():
        if not child.is_dir():
            continue
        try:
            jsonls = [
                f for f in child.glob("*.jsonl") if not f.name.startswith("agent-")
            ]
        except OSError:
            # Unreadable folder — skip silently, matching find_local_sessions
            continue
        if not jsonls:
            continue
        latest_mtime = max(f.stat().st_mtime for f in jsonls)

        if child.name in global_names:
            global_paths.append(child)
            global_total_sessions += len(jsonls)
            global_latest_mtime = max(global_latest_mtime, latest_mtime)
            continue

        projects.append(
            {
                "name": get_project_display_name(child.name),
                "raw_name": child.name,
                "path": child,
                "paths": [child],
                "session_count": len(jsonls),
                "latest_mtime": latest_mtime,
            }
        )

    if global_paths:
        projects.append(
            {
                "name": "Global Sessions",
                "raw_name": None,
                "path": None,
                "paths": global_paths,
                "session_count": global_total_sessions,
                "latest_mtime": global_latest_mtime,
            }
        )

    projects.sort(key=lambda p: p["latest_mtime"], reverse=True)

    # Detect display-name collisions so we can disambiguate just those rows
    name_counts = {}
    for p in projects:
        name_counts[p["name"]] = name_counts.get(p["name"], 0) + 1

    for p in projects:
        date_str = datetime.fromtimestamp(p["latest_mtime"]).strftime("%Y-%m-%d %H:%M")
        plural = "session" if p["session_count"] == 1 else "sessions"
        line = f"{p['name']:<30} {date_str}   {p['session_count']} {plural}"
        # Virtual project never collides (unique name) and has no raw_name to show.
        if name_counts[p["name"]] > 1 and p["raw_name"] is not None:
            line = f"{line}   ({p['raw_name']})"
        p["display"] = line

    return projects


def get_project_display_name(folder_name):
    """Convert encoded folder name to readable project name.

    Claude Code stores projects in folders like:
    - -home-user-projects-myproject -> myproject
    - -mnt-c-Users-name-Projects-app -> app

    For nested paths under common roots (home, projects, code, Users, etc.),
    extracts the meaningful project portion.
    """
    # Common path prefixes to strip
    prefixes_to_strip = [
        "-home-",
        "-mnt-c-Users-",
        "-mnt-c-users-",
        "-Users-",
    ]

    name = folder_name
    for prefix in prefixes_to_strip:
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix) :]
            break

    # Split on dashes and find meaningful parts
    parts = name.split("-")

    # Common intermediate directories to skip
    skip_dirs = {"projects", "code", "repos", "src", "dev", "work", "documents"}

    # Find the first meaningful part (after skipping username and common dirs)
    meaningful_parts = []
    found_project = False

    for i, part in enumerate(parts):
        if not part:
            continue
        # Skip the first part if it looks like a username (before common dirs)
        if i == 0 and not found_project:
            # Check if next parts contain common dirs
            remaining = [p.lower() for p in parts[i + 1 :]]
            if any(d in remaining for d in skip_dirs):
                continue
        if part.lower() in skip_dirs:
            found_project = True
            continue
        meaningful_parts.append(part)
        found_project = True

    if meaningful_parts:
        return "-".join(meaningful_parts)

    # Fallback: return last non-empty part or original
    for part in reversed(parts):
        if part:
            return part
    return folder_name


def find_all_sessions(folder, include_agents=False):
    """Find all sessions in a Claude projects folder, grouped by project.

    Returns a list of project dicts, each containing:
    - name: display name for the project
    - path: Path to the project folder
    - sessions: list of session dicts with path, summary, mtime, size

    Sessions are sorted by modification time (most recent first) within each project.
    Projects are sorted by their most recent session.
    """
    folder = Path(folder)
    if not folder.exists():
        return []

    projects = {}

    for session_file in folder.glob("**/*.jsonl"):
        # Skip agent files unless requested
        if not include_agents and session_file.name.startswith("agent-"):
            continue

        # Get summary and skip boring sessions
        summary = get_session_summary(session_file)
        if summary.lower() == "warmup" or summary == "(no summary)":
            continue

        # Get project folder
        project_folder = session_file.parent
        project_key = project_folder.name

        if project_key not in projects:
            projects[project_key] = {
                "name": get_project_display_name(project_key),
                "path": project_folder,
                "sessions": [],
            }

        stat = session_file.stat()
        projects[project_key]["sessions"].append(
            {
                "path": session_file,
                "summary": summary,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )

    # Sort sessions within each project by mtime (most recent first)
    for project in projects.values():
        project["sessions"].sort(key=lambda s: s["mtime"], reverse=True)

    # Convert to list and sort projects by most recent session
    result = list(projects.values())
    result.sort(
        key=lambda p: p["sessions"][0]["mtime"] if p["sessions"] else 0, reverse=True
    )

    return result


def generate_batch_html(
    source_folder, output_dir, include_agents=False, progress_callback=None
):
    """Generate HTML archive for all sessions in a Claude projects folder.

    Creates:
    - Master index.html listing all projects
    - Per-project directories with index.html listing sessions
    - Per-session directories with transcript pages

    Args:
        source_folder: Path to the Claude projects folder
        output_dir: Path for output archive
        include_agents: Whether to include agent-* session files
        progress_callback: Optional callback(project_name, session_name, current, total)
            called after each session is processed

    Returns statistics dict with total_projects, total_sessions, failed_sessions, output_dir.
    """
    source_folder = Path(source_folder)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find all sessions
    projects = find_all_sessions(source_folder, include_agents=include_agents)

    # Calculate total for progress tracking
    total_session_count = sum(len(p["sessions"]) for p in projects)
    processed_count = 0
    successful_sessions = 0
    failed_sessions = []

    # Process each project
    for project in projects:
        project_dir = output_dir / project["name"]
        project_dir.mkdir(exist_ok=True)

        # Process each session
        for session in project["sessions"]:
            session_name = session["path"].stem
            session_dir = project_dir / session_name

            # Generate transcript HTML with error handling
            try:
                generate_html(session["path"], session_dir)
                successful_sessions += 1
            except Exception as e:
                failed_sessions.append(
                    {
                        "project": project["name"],
                        "session": session_name,
                        "error": str(e),
                    }
                )

            processed_count += 1

            # Call progress callback if provided
            if progress_callback:
                progress_callback(
                    project["name"], session_name, processed_count, total_session_count
                )

        # Generate project index
        _generate_project_index(project, project_dir)

    # Generate master index
    _generate_master_index(projects, output_dir)

    return {
        "total_projects": len(projects),
        "total_sessions": successful_sessions,
        "failed_sessions": failed_sessions,
        "output_dir": output_dir,
    }


def _generate_project_index(project, output_dir):
    """Generate index.html for a single project."""
    template = get_template("project_index.html")

    # Format sessions for template
    sessions_data = []
    for session in project["sessions"]:
        mod_time = datetime.fromtimestamp(session["mtime"])
        sessions_data.append(
            {
                "name": session["path"].stem,
                "summary": session["summary"],
                "date": mod_time.strftime("%Y-%m-%d %H:%M"),
                "size_kb": session["size"] / 1024,
            }
        )

    html_content = template.render(
        project_name=project["name"],
        sessions=sessions_data,
        session_count=len(sessions_data),
        css=CSS,
        js=JS,
    )

    output_path = output_dir / "index.html"
    output_path.write_text(html_content, encoding="utf-8")


def _generate_master_index(projects, output_dir):
    """Generate master index.html listing all projects."""
    template = get_template("master_index.html")

    # Format projects for template
    projects_data = []
    total_sessions = 0

    for project in projects:
        session_count = len(project["sessions"])
        total_sessions += session_count

        # Get most recent session date
        if project["sessions"]:
            most_recent = datetime.fromtimestamp(project["sessions"][0]["mtime"])
            recent_date = most_recent.strftime("%Y-%m-%d")
        else:
            recent_date = "N/A"

        projects_data.append(
            {
                "name": project["name"],
                "session_count": session_count,
                "recent_date": recent_date,
            }
        )

    html_content = template.render(
        projects=projects_data,
        total_projects=len(projects),
        total_sessions=total_sessions,
        css=CSS,
        js=JS,
    )

    output_path = output_dir / "index.html"
    output_path.write_text(html_content, encoding="utf-8")


def parse_session_file(filepath):
    """Parse a session file and return normalized data.

    Supports both JSON and JSONL formats.
    Returns a dict with 'loglines' key containing the normalized entries.
    """
    filepath = Path(filepath)

    if filepath.suffix == ".jsonl":
        return _parse_jsonl_file(filepath)
    else:
        # Standard JSON format
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)


def _parse_jsonl_file(filepath):
    """Parse JSONL file and convert to standard format."""
    loglines = []

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                entry_type = obj.get("type")

                # Skip non-message entries
                if entry_type not in ("user", "assistant"):
                    continue

                # Convert to standard format
                entry = {
                    "type": entry_type,
                    "timestamp": obj.get("timestamp", ""),
                    "message": obj.get("message", {}),
                }

                # Preserve isCompactSummary if present
                if obj.get("isCompactSummary"):
                    entry["isCompactSummary"] = True

                loglines.append(entry)
            except json.JSONDecodeError:
                continue

    return {"loglines": loglines}


class CredentialsError(Exception):
    """Raised when credentials cannot be obtained."""

    pass


def get_access_token_from_keychain():
    """Get access token from macOS keychain.

    Returns the access token or None if not found.
    Raises CredentialsError with helpful message on failure.
    """
    if platform.system() != "Darwin":
        return None

    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                os.environ.get("USER", ""),
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

        # Parse the JSON to get the access token
        creds = json.loads(result.stdout.strip())
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, subprocess.SubprocessError):
        return None


def get_org_uuid_from_config():
    """Get organization UUID from ~/.claude.json.

    Returns the organization UUID or None if not found.
    """
    config_path = Path.home() / ".claude.json"
    if not config_path.exists():
        return None

    try:
        with open(config_path) as f:
            config = json.load(f)
        return config.get("oauthAccount", {}).get("organizationUuid")
    except (json.JSONDecodeError, IOError):
        return None


def get_api_headers(token, org_uuid):
    """Build API request headers."""
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
        "x-organization-uuid": org_uuid,
    }


def fetch_sessions(token, org_uuid):
    """Fetch list of sessions from the API.

    Returns the sessions data as a dict.
    Raises httpx.HTTPError on network/API errors.
    """
    headers = get_api_headers(token, org_uuid)
    response = httpx.get(f"{API_BASE_URL}/sessions", headers=headers, timeout=30.0)
    response.raise_for_status()
    return response.json()


def fetch_session(token, org_uuid, session_id):
    """Fetch a specific session from the API.

    Returns the session data as a dict.
    Raises httpx.HTTPError on network/API errors.
    """
    headers = get_api_headers(token, org_uuid)
    response = httpx.get(
        f"{API_BASE_URL}/session_ingress/session/{session_id}",
        headers=headers,
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


def detect_github_repo(loglines):
    """
    Detect GitHub repo from git push output in tool results.

    Looks for patterns like:
    - github.com/owner/repo/pull/new/branch (from git push messages)

    Returns the first detected repo (owner/name) or None.
    """
    for entry in loglines:
        message = entry.get("message", {})
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    match = GITHUB_REPO_PATTERN.search(result_content)
                    if match:
                        return match.group(1)
    return None


def extract_repo_from_session(session):
    """Extract GitHub repo from session metadata.

    Looks in session_context.outcomes for git_info.repo,
    or parses from session_context.sources URL.

    Returns repo as "owner/name" or None.
    """
    context = session.get("session_context", {})

    # Try outcomes first (has clean repo format)
    outcomes = context.get("outcomes", [])
    for outcome in outcomes:
        if outcome.get("type") == "git_repository":
            git_info = outcome.get("git_info", {})
            repo = git_info.get("repo")
            if repo:
                return repo

    # Fall back to sources URL
    sources = context.get("sources", [])
    for source in sources:
        if source.get("type") == "git_repository":
            url = source.get("url", "")
            # Parse github.com/owner/repo from URL
            if "github.com/" in url:
                # Extract owner/repo from https://github.com/owner/repo
                match = re.search(r"github\.com/([^/]+/[^/]+?)(?:\.git)?$", url)
                if match:
                    return match.group(1)

    return None


def enrich_sessions_with_repos(sessions, token=None, org_uuid=None, fetch_fn=None):
    """Enrich sessions with repo information from session metadata.

    Args:
        sessions: List of session dicts from the API
        token: Unused (kept for backward compatibility)
        org_uuid: Unused (kept for backward compatibility)
        fetch_fn: Unused (kept for backward compatibility)

    Returns:
        List of session dicts with 'repo' key added
    """
    enriched = []
    for session in sessions:
        session_copy = dict(session)
        session_copy["repo"] = extract_repo_from_session(session)
        enriched.append(session_copy)
    return enriched


def filter_sessions_by_repo(sessions, repo):
    """Filter sessions by repo.

    Args:
        sessions: List of session dicts with 'repo' key
        repo: Repo to filter by (owner/name), or None to return all

    Returns:
        Filtered list of sessions
    """
    if repo is None:
        return sessions
    return [s for s in sessions if s.get("repo") == repo]


def format_json(obj):
    try:
        if isinstance(obj, str):
            obj = json.loads(obj)
        formatted = json.dumps(obj, indent=2, ensure_ascii=False)
        return f'<pre class="json">{html.escape(formatted)}</pre>'
    except (json.JSONDecodeError, TypeError):
        return f"<pre>{html.escape(str(obj))}</pre>"


def render_markdown_text(text):
    if not text:
        return ""
    return markdown.markdown(text, extensions=["fenced_code", "tables"])


def is_json_like(text):
    if not text or not isinstance(text, str):
        return False
    text = text.strip()
    return (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    )


def render_todo_write(tool_input, tool_id):
    todos = tool_input.get("todos", [])
    if not todos:
        return ""
    return _macros.todo_list(todos, tool_id)


def render_write_tool(tool_input, tool_id):
    """Render Write tool calls with file path header and content preview."""
    file_path = tool_input.get("file_path", "Unknown file")
    content = tool_input.get("content", "")
    return _macros.write_tool(file_path, content, tool_id)


def render_edit_tool(tool_input, tool_id):
    """Render Edit tool calls with diff-like old/new display."""
    file_path = tool_input.get("file_path", "Unknown file")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")
    replace_all = tool_input.get("replace_all", False)
    return _macros.edit_tool(file_path, old_string, new_string, replace_all, tool_id)


def render_bash_tool(tool_input, tool_id):
    """Render Bash tool calls with command as plain text."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    return _macros.bash_tool(command, description, tool_id)


def render_content_block(block):
    if not isinstance(block, dict):
        return f"<p>{html.escape(str(block))}</p>"
    block_type = block.get("type", "")
    if block_type == "image":
        source = block.get("source", {})
        media_type = source.get("media_type", "image/png")
        data = source.get("data", "")
        return _macros.image_block(media_type, data)
    elif block_type == "thinking":
        content_html = render_markdown_text(block.get("thinking", ""))
        return _macros.thinking(content_html)
    elif block_type == "text":
        content_html = render_markdown_text(block.get("text", ""))
        return _macros.assistant_text(content_html)
    elif block_type == "tool_use":
        tool_name = block.get("name", "Unknown tool")
        tool_input = block.get("input", {})
        tool_id = block.get("id", "")
        if tool_name == "TodoWrite":
            return render_todo_write(tool_input, tool_id)
        if tool_name == "Write":
            return render_write_tool(tool_input, tool_id)
        if tool_name == "Edit":
            return render_edit_tool(tool_input, tool_id)
        if tool_name == "Bash":
            return render_bash_tool(tool_input, tool_id)
        description = tool_input.get("description", "")
        display_input = {k: v for k, v in tool_input.items() if k != "description"}
        input_json = json.dumps(display_input, indent=2, ensure_ascii=False)
        return _macros.tool_use(tool_name, description, input_json, tool_id)
    elif block_type == "tool_result":
        content = block.get("content", "")
        is_error = block.get("is_error", False)
        has_images = False

        # Check for git commits and render with styled cards
        if isinstance(content, str):
            commits_found = list(COMMIT_PATTERN.finditer(content))
            if commits_found:
                # Build commit cards + remaining content
                parts = []
                last_end = 0
                for match in commits_found:
                    # Add any content before this commit
                    before = content[last_end : match.start()].strip()
                    if before:
                        parts.append(f"<pre>{html.escape(before)}</pre>")

                    commit_hash = match.group(1)
                    commit_msg = match.group(2)
                    parts.append(
                        _macros.commit_card(commit_hash, commit_msg, _github_repo)
                    )
                    last_end = match.end()

                # Add any remaining content after last commit
                after = content[last_end:].strip()
                if after:
                    parts.append(f"<pre>{html.escape(after)}</pre>")

                content_html = "".join(parts)
            else:
                content_html = f"<pre>{html.escape(content)}</pre>"
        elif isinstance(content, list):
            # Handle tool result content that contains multiple blocks (text, images, etc.)
            parts = []
            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type", "")
                    if item_type == "text":
                        text = item.get("text", "")
                        if text:
                            parts.append(f"<pre>{html.escape(text)}</pre>")
                    elif item_type == "image":
                        source = item.get("source", {})
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        if data:
                            parts.append(_macros.image_block(media_type, data))
                            has_images = True
                    else:
                        # Unknown type, render as JSON
                        parts.append(format_json(item))
                else:
                    # Non-dict item, escape as text
                    parts.append(f"<pre>{html.escape(str(item))}</pre>")
            content_html = "".join(parts) if parts else format_json(content)
        elif is_json_like(content):
            content_html = format_json(content)
        else:
            content_html = format_json(content)
        return _macros.tool_result(content_html, is_error, has_images)
    else:
        return format_json(block)


def render_user_message_content(message_data):
    content = message_data.get("content", "")
    if isinstance(content, str):
        if is_json_like(content):
            return _macros.user_content(format_json(content))
        return _macros.user_content(render_markdown_text(content))
    elif isinstance(content, list):
        return "".join(render_content_block(block) for block in content)
    return f"<p>{html.escape(str(content))}</p>"


def render_assistant_message(message_data):
    content = message_data.get("content", [])
    if not isinstance(content, list):
        return f"<p>{html.escape(str(content))}</p>"
    return "".join(render_content_block(block) for block in content)


def make_msg_id(timestamp):
    return f"msg-{timestamp.replace(':', '-').replace('.', '-')}"


def analyze_conversation(messages):
    """Analyze messages in a conversation to extract stats and long texts."""
    tool_counts = {}  # tool_name -> count
    long_texts = []
    commits = []  # list of (hash, message, timestamp)

    for log_type, message_json, timestamp in messages:
        if not message_json:
            continue
        try:
            message_data = json.loads(message_json)
        except json.JSONDecodeError:
            continue

        content = message_data.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")

            if block_type == "tool_use":
                tool_name = block.get("name", "Unknown")
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            elif block_type == "tool_result":
                # Check for git commit output
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    for match in COMMIT_PATTERN.finditer(result_content):
                        commits.append((match.group(1), match.group(2), timestamp))
            elif block_type == "text":
                text = block.get("text", "")
                if len(text) >= LONG_TEXT_THRESHOLD:
                    long_texts.append(text)

    return {
        "tool_counts": tool_counts,
        "long_texts": long_texts,
        "commits": commits,
    }


def format_tool_stats(tool_counts):
    """Format tool counts into a concise summary string."""
    if not tool_counts:
        return ""

    # Abbreviate common tool names
    abbrev = {
        "Bash": "bash",
        "Read": "read",
        "Write": "write",
        "Edit": "edit",
        "Glob": "glob",
        "Grep": "grep",
        "Task": "task",
        "TodoWrite": "todo",
        "WebFetch": "fetch",
        "WebSearch": "search",
    }

    parts = []
    for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        short_name = abbrev.get(name, name.lower())
        parts.append(f"{count} {short_name}")

    return " · ".join(parts)


def is_tool_result_message(message_data):
    """Check if a message contains only tool_result blocks."""
    content = message_data.get("content", [])
    if not isinstance(content, list):
        return False
    if not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def render_message(log_type, message_json, timestamp):
    if not message_json:
        return ""
    try:
        message_data = json.loads(message_json)
    except json.JSONDecodeError:
        return ""
    if log_type == "user":
        content_html = render_user_message_content(message_data)
        # Check if this is a tool result message
        if is_tool_result_message(message_data):
            role_class, role_label = "tool-reply", "Tool reply"
        else:
            role_class, role_label = "user", "User"
    elif log_type == "assistant":
        content_html = render_assistant_message(message_data)
        role_class, role_label = "assistant", "Assistant"
    else:
        return ""
    if not content_html.strip():
        return ""
    msg_id = make_msg_id(timestamp)
    return _macros.message(role_class, role_label, msg_id, timestamp, content_html)


CSS = """
/* iMessage-style dark theme. The markup is unchanged from the prior light
   theme — all the layout work is here. Each .message is a flex column whose
   children (header strip + content bubble) are reordered so the bubble sits
   on top and the timestamp/role caption sits underneath. User messages
   align to the right with a filled blue bubble; assistant and tool-reply
   align to the left in a dark grey bubble. Inner blocks (tool use, tool
   result, edits, code) use translucent accent colours that sit cleanly on
   the dark grey assistant bubble. */
:root {
    --bg-color: #000;
    --surface-1: #1c1c1e;
    --surface-2: #2c2c2e;
    --surface-3: #3a3a3c;
    --user-bg: #0b84ff;
    --user-text: #fff;
    --assistant-bg: #2c2c2e;
    --assistant-text: #f2f2f7;
    --tool-accent: #bf5af2;
    --tool-result-accent: #30d158;
    --tool-error-accent: #ff453a;
    --thinking-accent: #ff9f0a;
    --link-color: #64d2ff;
    --code-bg: #0a0a0c;
    --code-text: #c4f0a3;
    --text-color: #f2f2f7;
    --text-muted: #98989d;
    --border-subtle: rgba(255,255,255,0.08);
    --shadow: 0 1px 3px rgba(0,0,0,0.5);
    --user-border: var(--user-bg);
    --assistant-border: var(--surface-3);
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Roboto, sans-serif; background: var(--bg-color); color: var(--text-color); margin: 0; padding: 16px; line-height: 1.55; }
.container { max-width: 900px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin-bottom: 24px; padding-bottom: 8px; border-bottom: 1px solid var(--border-subtle); color: var(--text-color); }
.header-row { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; border-bottom: 1px solid var(--border-subtle); padding-bottom: 8px; margin-bottom: 24px; }
.header-row h1 { border-bottom: none; padding-bottom: 0; margin-bottom: 0; flex: 1; min-width: 200px; }
a { color: var(--link-color); }

/* Message row: flex column so header (caption) and content (bubble) can
   be re-ordered. align-items steers the whole row left vs right. */
.message { display: flex; flex-direction: column; margin-bottom: 4px; }
.message + .message { margin-top: 14px; }
.message.user { align-items: flex-end; }
.message.assistant, .message.tool-reply { align-items: flex-start; }

/* Caption strip below the bubble (time + role label). Kept tiny so it
   reads as metadata, not content. */
.message-header { order: 2; display: flex; gap: 10px; align-items: center; padding: 4px 8px 0; background: transparent; font-size: 0.72rem; color: var(--text-muted); }
.role-label { font-weight: 500; text-transform: lowercase; letter-spacing: 0; color: var(--text-muted); }
time { color: var(--text-muted); font-size: 0.72rem; }
.timestamp-link { color: inherit; text-decoration: none; }
.timestamp-link:hover { text-decoration: underline; }
.message:target .message-content { animation: highlight 1.5s ease-out; }
@keyframes highlight { 0% { box-shadow: 0 0 0 4px rgba(11,132,255,0.45); } 100% { box-shadow: 0 0 0 0 rgba(11,132,255,0); } }

/* The bubble itself. Asymmetric border-radius gives the chat-tail look:
   tail-side corner is small, the other three are big. */
.message-content { order: 1; padding: 10px 14px; border-radius: 18px; max-width: 90%; word-wrap: break-word; overflow-wrap: anywhere; }
.message.user .message-content { background: var(--user-bg); color: var(--user-text); border-bottom-right-radius: 6px; max-width: 75%; }
.message.assistant .message-content { background: var(--assistant-bg); color: var(--assistant-text); border-bottom-left-radius: 6px; }
.message.tool-reply .message-content { background: var(--surface-1); color: var(--text-color); border: 1px solid rgba(255,159,10,0.20); border-bottom-left-radius: 6px; }
.tool-reply .role-label { color: var(--thinking-accent); }
.tool-reply .tool-result { background: transparent; padding: 0; margin: 0; border: 0; }
.message-content p { margin: 0 0 8px 0; }
.message-content p:last-child { margin-bottom: 0; }
.message.user .message-content a { color: #fff; text-decoration: underline; }

.thinking { background: rgba(255,159,10,0.10); border: 1px solid rgba(255,159,10,0.25); border-radius: 12px; padding: 10px 12px; margin: 10px 0; font-size: 0.9rem; color: var(--text-muted); }
.thinking-label { font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--thinking-accent); margin-bottom: 6px; }
.thinking p { margin: 6px 0; }
.assistant-text { margin: 6px 0; }

.tool-use { background: rgba(191,90,242,0.10); border: 1px solid rgba(191,90,242,0.25); border-radius: 12px; padding: 10px 12px; margin: 10px 0; }
.tool-header { font-weight: 600; color: var(--tool-accent); margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
.tool-icon { font-size: 1.05rem; }
.tool-description { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 6px; font-style: italic; }
.tool-result { background: rgba(48,209,88,0.08); border: 1px solid rgba(48,209,88,0.20); border-radius: 12px; padding: 10px 12px; margin: 10px 0; }
.tool-result.tool-error { background: rgba(255,69,58,0.10); border-color: rgba(255,69,58,0.25); }

.file-tool { border-radius: 12px; padding: 10px 12px; margin: 10px 0; }
.write-tool { background: rgba(48,209,88,0.10); border: 1px solid rgba(48,209,88,0.25); }
.edit-tool { background: rgba(255,159,10,0.08); border: 1px solid rgba(255,159,10,0.25); }
.file-tool-header { font-weight: 600; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; font-size: 0.95rem; }
.write-header { color: var(--tool-result-accent); }
.edit-header { color: var(--thinking-accent); }
.file-tool-icon { font-size: 1rem; }
.file-tool-path { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: rgba(255,255,255,0.10); padding: 2px 8px; border-radius: 4px; }
.file-tool-fullpath { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.78rem; color: var(--text-muted); margin-bottom: 8px; word-break: break-all; }
.file-content { margin: 0; }
.edit-section { display: flex; margin: 4px 0; border-radius: 6px; overflow: hidden; }
.edit-label { padding: 8px 12px; font-weight: bold; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; display: flex; align-items: flex-start; }
.edit-old { background: rgba(255,69,58,0.10); }
.edit-old .edit-label { color: #ff8a80; background: rgba(255,69,58,0.22); }
.edit-old .edit-content { color: #ff9c8f; }
.edit-new { background: rgba(48,209,88,0.10); }
.edit-new .edit-label { color: #7be0a3; background: rgba(48,209,88,0.22); }
.edit-new .edit-content { color: #b2f0c8; }
.edit-content { margin: 0; flex: 1; background: transparent; font-size: 0.85rem; }
.edit-replace-all { font-size: 0.75rem; font-weight: normal; color: var(--text-muted); }

.todo-list { background: rgba(48,209,88,0.08); border: 1px solid rgba(48,209,88,0.25); border-radius: 12px; padding: 10px 12px; margin: 10px 0; }
.todo-header { font-weight: 600; color: var(--tool-result-accent); margin-bottom: 8px; display: flex; align-items: center; gap: 8px; font-size: 0.95rem; }
.todo-items { list-style: none; margin: 0; padding: 0; }
.todo-item { display: flex; align-items: flex-start; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--border-subtle); font-size: 0.9rem; }
.todo-item:last-child { border-bottom: none; }
.todo-icon { flex-shrink: 0; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; font-weight: bold; border-radius: 50%; }
.todo-completed .todo-icon { color: var(--tool-result-accent); background: rgba(48,209,88,0.18); }
.todo-completed .todo-content { color: var(--text-muted); text-decoration: line-through; }
.todo-in-progress .todo-icon { color: var(--thinking-accent); background: rgba(255,159,10,0.18); }
.todo-in-progress .todo-content { color: var(--thinking-accent); font-weight: 500; }
.todo-pending .todo-icon { color: var(--text-muted); background: rgba(255,255,255,0.06); }
.todo-pending .todo-content { color: var(--text-color); }

pre { background: var(--code-bg); color: var(--code-text); padding: 10px 12px; border-radius: 8px; overflow-x: auto; font-size: 0.82rem; line-height: 1.5; margin: 8px 0; white-space: pre-wrap; word-wrap: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre.json { color: #e0e0e0; }
code { background: rgba(255,255,255,0.10); padding: 1px 6px; border-radius: 4px; font-size: 0.88em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre code { background: none; padding: 0; }
/* Code inside the user (blue) bubble: bump contrast so it stays legible. */
.message.user code { background: rgba(255,255,255,0.20); color: #fff; }
.message.user pre { background: rgba(0,0,0,0.35); color: #e7f4ff; }

.user-content { margin: 0; }
.truncatable { position: relative; }
.truncatable.truncated .truncatable-content { max-height: 200px; overflow: hidden; }
.truncatable.truncated::after { content: ''; position: absolute; bottom: 32px; left: 0; right: 0; height: 60px; background: linear-gradient(to bottom, transparent, var(--assistant-bg)); pointer-events: none; }
.message.user .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--user-bg)); }
.message.tool-reply .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--surface-1)); }
.tool-use .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, rgba(28,28,30,0.95)); }
.tool-result .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, rgba(28,28,30,0.95)); }
.expand-btn { display: none; width: 100%; padding: 8px 12px; margin-top: 4px; background: rgba(255,255,255,0.06); border: 1px solid var(--border-subtle); border-radius: 8px; cursor: pointer; font-size: 0.8rem; color: var(--text-muted); }
.expand-btn:hover { background: rgba(255,255,255,0.12); color: var(--text-color); }
.message.user .expand-btn { background: rgba(255,255,255,0.18); border-color: rgba(255,255,255,0.25); color: rgba(255,255,255,0.92); }
.message.user .expand-btn:hover { background: rgba(255,255,255,0.28); color: #fff; }
.truncatable.truncated .expand-btn, .truncatable.expanded .expand-btn { display: block; }

.pagination { display: flex; justify-content: center; gap: 8px; margin: 24px 0; flex-wrap: wrap; }
.pagination a, .pagination span { padding: 5px 10px; border-radius: 8px; text-decoration: none; font-size: 0.85rem; }
.pagination a { background: var(--surface-1); color: var(--user-bg); border: 1px solid rgba(11,132,255,0.30); }
.pagination a:hover { background: rgba(11,132,255,0.12); }
.pagination .current { background: var(--user-bg); color: white; }
.pagination .disabled { color: var(--text-muted); border: 1px solid var(--border-subtle); }
.pagination .index-link { background: var(--user-bg); color: white; }

details.continuation { margin-bottom: 16px; }
details.continuation summary { cursor: pointer; padding: 10px 14px; background: var(--surface-2); border-radius: 14px; font-weight: 500; color: var(--text-muted); list-style: none; }
details.continuation summary:hover { background: var(--surface-3); }
details.continuation[open] summary { border-radius: 14px 14px 0 0; margin-bottom: 0; }

/* The index page keeps card layout — bubbles only make sense for the
   transcript pages where there's a back-and-forth. The cards just adopt
   the dark palette. */
.index-item { margin-bottom: 14px; border-radius: 14px; overflow: hidden; box-shadow: var(--shadow); background: var(--surface-1); border: 1px solid var(--border-subtle); }
.index-item a { display: block; text-decoration: none; color: inherit; }
.index-item a:hover { background: rgba(11,132,255,0.08); }
.index-item-header { display: flex; justify-content: space-between; align-items: center; padding: 8px 14px; background: rgba(255,255,255,0.03); font-size: 0.85rem; border-bottom: 1px solid var(--border-subtle); }
.index-item-number { font-weight: 600; color: var(--user-bg); }
.index-item-content { padding: 14px; }
.index-item-stats { padding: 8px 14px 12px; font-size: 0.85rem; color: var(--text-muted); border-top: 1px solid var(--border-subtle); }
.index-item-commit { margin-top: 6px; padding: 4px 8px; background: rgba(255,159,10,0.10); border-radius: 4px; font-size: 0.85rem; color: var(--thinking-accent); }
.index-item-commit code { background: rgba(0,0,0,0.30); padding: 1px 4px; border-radius: 3px; font-size: 0.8rem; margin-right: 6px; }
.commit-card { margin: 8px 0; padding: 10px 14px; background: rgba(255,159,10,0.08); border-left: 3px solid var(--thinking-accent); border-radius: 6px; }
.commit-card a { text-decoration: none; color: var(--text-color); display: block; }
.commit-card a:hover { color: var(--thinking-accent); }
.commit-card-hash { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--thinking-accent); font-weight: 600; margin-right: 8px; }
.index-commit { margin-bottom: 12px; padding: 10px 14px; background: rgba(255,159,10,0.08); border-left: 3px solid var(--thinking-accent); border-radius: 8px; box-shadow: var(--shadow); }
.index-commit a { display: block; text-decoration: none; color: inherit; }
.index-commit a:hover { background: rgba(255,159,10,0.12); margin: -10px -14px; padding: 10px 14px; border-radius: 8px; }
.index-commit-header { display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; margin-bottom: 4px; }
.index-commit-hash { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--thinking-accent); font-weight: 600; }
.index-commit-msg { color: var(--text-color); }
.index-item-long-text { margin-top: 8px; padding: 10px 12px; background: var(--surface-2); border-radius: 8px; border-left: 2px solid var(--border-subtle); }
.index-item-long-text .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--surface-2)); }
.index-item-long-text-content { color: var(--text-color); }

#search-box { display: none; align-items: center; gap: 8px; }
#search-box input { padding: 6px 10px; border: 1px solid var(--border-subtle); border-radius: 8px; font-size: 16px; width: 180px; background: var(--surface-1); color: var(--text-color); }
#search-box button, #modal-search-btn, #modal-close-btn { background: var(--user-bg); color: white; border: none; border-radius: 8px; padding: 6px 10px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
#search-box button:hover, #modal-search-btn:hover { background: #2a95ff; }
#modal-close-btn { background: var(--surface-3); margin-left: 8px; }
#modal-close-btn:hover { background: var(--text-muted); color: #000; }
#search-modal[open] { border: 1px solid var(--border-subtle); border-radius: 14px; box-shadow: 0 8px 32px rgba(0,0,0,0.6); padding: 0; width: 90vw; max-width: 900px; height: 80vh; max-height: 80vh; display: flex; flex-direction: column; background: var(--surface-1); color: var(--text-color); }
#search-modal::backdrop { background: rgba(0,0,0,0.7); }
.search-modal-header { display: flex; align-items: center; gap: 8px; padding: 14px; border-bottom: 1px solid var(--border-subtle); background: var(--surface-1); border-radius: 14px 14px 0 0; }
.search-modal-header input { flex: 1; padding: 8px 12px; border: 1px solid var(--border-subtle); border-radius: 8px; font-size: 16px; background: var(--bg-color); color: var(--text-color); }
#search-status { padding: 8px 14px; font-size: 0.85rem; color: var(--text-muted); border-bottom: 1px solid var(--border-subtle); }
#search-results { flex: 1; overflow-y: auto; padding: 14px; }
.search-result { margin-bottom: 12px; border-radius: 10px; overflow: hidden; box-shadow: var(--shadow); background: var(--surface-2); }
.search-result a { display: block; text-decoration: none; color: inherit; }
.search-result a:hover { background: rgba(11,132,255,0.10); }
.search-result-page { padding: 6px 12px; background: rgba(255,255,255,0.04); font-size: 0.8rem; color: var(--text-muted); border-bottom: 1px solid var(--border-subtle); }
.search-result-content { padding: 12px; }
.search-result mark { background: rgba(255,214,10,0.30); color: #ffd60a; padding: 1px 2px; border-radius: 2px; }

@media (max-width: 600px) {
    body { padding: 8px; }
    .message.user .message-content { max-width: 85%; }
    .message.assistant .message-content { max-width: 95%; }
    .index-item { border-radius: 10px; }
    .message-content, .index-item-content { padding: 10px 12px; }
    pre { font-size: 0.78rem; padding: 8px; }
    #search-box input { width: 120px; }
    #search-modal[open] { width: 95vw; height: 90vh; }
}
"""

JS = """
document.querySelectorAll('time[data-timestamp]').forEach(function(el) {
    const timestamp = el.getAttribute('data-timestamp');
    const date = new Date(timestamp);
    const now = new Date();
    const isToday = date.toDateString() === now.toDateString();
    const timeStr = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    if (isToday) { el.textContent = timeStr; }
    else { el.textContent = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + timeStr; }
});
document.querySelectorAll('pre.json').forEach(function(el) {
    let text = el.textContent;
    text = text.replace(/"([^"]+)":/g, '<span style="color: #ce93d8">"$1"</span>:');
    text = text.replace(/: "([^"]*)"/g, ': <span style="color: #81d4fa">"$1"</span>');
    text = text.replace(/: (\\d+)/g, ': <span style="color: #ffcc80">$1</span>');
    text = text.replace(/: (true|false|null)/g, ': <span style="color: #f48fb1">$1</span>');
    el.innerHTML = text;
});
document.querySelectorAll('.truncatable').forEach(function(wrapper) {
    const content = wrapper.querySelector('.truncatable-content');
    const btn = wrapper.querySelector('.expand-btn');
    if (content.scrollHeight > 250) {
        wrapper.classList.add('truncated');
        btn.addEventListener('click', function() {
            if (wrapper.classList.contains('truncated')) { wrapper.classList.remove('truncated'); wrapper.classList.add('expanded'); btn.textContent = 'Show less'; }
            else { wrapper.classList.remove('expanded'); wrapper.classList.add('truncated'); btn.textContent = 'Show more'; }
        });
    }
});
"""

# JavaScript to fix relative URLs when served via gisthost.github.io or gistpreview.github.io
# Fixes issue #26: Pagination links broken on gisthost.github.io
GIST_PREVIEW_JS = r"""
(function() {
    var hostname = window.location.hostname;
    if (hostname !== 'gisthost.github.io' && hostname !== 'gistpreview.github.io') return;
    // URL format: https://gisthost.github.io/?GIST_ID/filename.html
    var match = window.location.search.match(/^\?([^/]+)/);
    if (!match) return;
    var gistId = match[1];

    function rewriteLinks(root) {
        (root || document).querySelectorAll('a[href]').forEach(function(link) {
            var href = link.getAttribute('href');
            // Skip already-rewritten links (issue #26 fix)
            if (href.startsWith('?')) return;
            // Skip external links and anchors
            if (href.startsWith('http') || href.startsWith('#') || href.startsWith('//')) return;
            // Handle anchor in relative URL (e.g., page-001.html#msg-123)
            var parts = href.split('#');
            var filename = parts[0];
            var anchor = parts.length > 1 ? '#' + parts[1] : '';
            link.setAttribute('href', '?' + gistId + '/' + filename + anchor);
        });
    }

    // Run immediately
    rewriteLinks();

    // Also run on DOMContentLoaded in case DOM isn't ready yet
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() { rewriteLinks(); });
    }

    // Use MutationObserver to catch dynamically added content
    // gistpreview.github.io may add content after initial load
    var observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            mutation.addedNodes.forEach(function(node) {
                if (node.nodeType === 1) { // Element node
                    rewriteLinks(node);
                    // Also check if the node itself is a link
                    if (node.tagName === 'A' && node.getAttribute('href')) {
                        var href = node.getAttribute('href');
                        if (!href.startsWith('?') && !href.startsWith('http') &&
                            !href.startsWith('#') && !href.startsWith('//')) {
                            var parts = href.split('#');
                            var filename = parts[0];
                            var anchor = parts.length > 1 ? '#' + parts[1] : '';
                            node.setAttribute('href', '?' + gistId + '/' + filename + anchor);
                        }
                    }
                }
            });
        });
    });

    // Start observing once body exists
    function startObserving() {
        if (document.body) {
            observer.observe(document.body, { childList: true, subtree: true });
        } else {
            setTimeout(startObserving, 10);
        }
    }
    startObserving();

    // Handle fragment navigation after dynamic content loads
    // gisthost.github.io/gistpreview.github.io loads content dynamically, so the browser's
    // native fragment navigation fails because the element doesn't exist yet
    function scrollToFragment() {
        var hash = window.location.hash;
        if (!hash) return false;
        var targetId = hash.substring(1);
        var target = document.getElementById(targetId);
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return true;
        }
        return false;
    }

    // Try immediately in case content is already loaded
    if (!scrollToFragment()) {
        // Retry with increasing delays to handle dynamic content loading
        var delays = [100, 300, 500, 1000, 2000];
        delays.forEach(function(delay) {
            setTimeout(scrollToFragment, delay);
        });
    }
})();
"""


def inject_gist_preview_js(output_dir):
    """Inject gist preview JavaScript into all HTML files in the output directory."""
    output_dir = Path(output_dir)
    for html_file in output_dir.glob("*.html"):
        content = html_file.read_text(encoding="utf-8")
        # Insert the gist preview JS before the closing </body> tag
        if "</body>" in content:
            content = content.replace(
                "</body>", f"<script>{GIST_PREVIEW_JS}</script>\n</body>"
            )
            html_file.write_text(content, encoding="utf-8")


def create_gist(output_dir, public=False):
    """Create a GitHub gist from the HTML files in output_dir.

    Returns the gist ID on success, or raises click.ClickException on failure.
    """
    output_dir = Path(output_dir)
    html_files = list(output_dir.glob("*.html"))
    if not html_files:
        raise click.ClickException("No HTML files found to upload to gist.")

    # Build the gh gist create command
    # gh gist create file1 file2 ... --public/--private
    cmd = ["gh", "gist", "create"]
    cmd.extend(str(f) for f in sorted(html_files))
    if public:
        cmd.append("--public")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        # Output is the gist URL, e.g., https://gist.github.com/username/GIST_ID
        gist_url = result.stdout.strip()
        # Extract gist ID from URL
        gist_id = gist_url.rstrip("/").split("/")[-1]
        return gist_id, gist_url
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else str(e)
        raise click.ClickException(f"Failed to create gist: {error_msg}")
    except FileNotFoundError:
        raise click.ClickException(
            "gh CLI not found. Install it from https://cli.github.com/ and run 'gh auth login'."
        )


def generate_pagination_html(current_page, total_pages):
    return _macros.pagination(current_page, total_pages)


def generate_index_pagination_html(total_pages):
    """Generate pagination for index page where Index is current (first page)."""
    return _macros.index_pagination(total_pages)


def generate_html(json_path, output_dir, github_repo=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    # Load session file (supports both JSON and JSONL)
    data = parse_session_file(json_path)

    loglines = data.get("loglines", [])

    # Auto-detect GitHub repo if not provided
    if github_repo is None:
        github_repo = detect_github_repo(loglines)
        if github_repo:
            print(f"Auto-detected GitHub repo: {github_repo}")
        else:
            print(
                "Warning: Could not auto-detect GitHub repo. Commit links will be disabled."
            )

    # Set module-level variable for render functions
    global _github_repo
    _github_repo = github_repo

    conversations = []
    current_conv = None
    for entry in loglines:
        log_type = entry.get("type")
        timestamp = entry.get("timestamp", "")
        is_compact_summary = entry.get("isCompactSummary", False)
        message_data = entry.get("message", {})
        if not message_data:
            continue
        # Convert message dict to JSON string for compatibility with existing render functions
        message_json = json.dumps(message_data)
        is_user_prompt = False
        user_text = None
        if log_type == "user":
            content = message_data.get("content", "")
            text = extract_text_from_content(content)
            if text:
                is_user_prompt = True
                user_text = text
        if is_user_prompt:
            if current_conv:
                conversations.append(current_conv)
            current_conv = {
                "user_text": user_text,
                "timestamp": timestamp,
                "messages": [(log_type, message_json, timestamp)],
                "is_continuation": bool(is_compact_summary),
            }
        elif current_conv:
            current_conv["messages"].append((log_type, message_json, timestamp))
    if current_conv:
        conversations.append(current_conv)

    total_convs = len(conversations)
    total_pages = (total_convs + PROMPTS_PER_PAGE - 1) // PROMPTS_PER_PAGE

    for page_num in range(1, total_pages + 1):
        start_idx = (page_num - 1) * PROMPTS_PER_PAGE
        end_idx = min(start_idx + PROMPTS_PER_PAGE, total_convs)
        page_convs = conversations[start_idx:end_idx]
        messages_html = []
        for conv in page_convs:
            is_first = True
            for log_type, message_json, timestamp in conv["messages"]:
                msg_html = render_message(log_type, message_json, timestamp)
                if msg_html:
                    # Wrap continuation summaries in collapsed details
                    if is_first and conv.get("is_continuation"):
                        msg_html = f'<details class="continuation"><summary>Session continuation summary</summary>{msg_html}</details>'
                    messages_html.append(msg_html)
                is_first = False
        pagination_html = generate_pagination_html(page_num, total_pages)
        page_template = get_template("page.html")
        page_content = page_template.render(
            css=CSS,
            js=JS,
            page_num=page_num,
            total_pages=total_pages,
            pagination_html=pagination_html,
            messages_html="".join(messages_html),
        )
        (output_dir / f"page-{page_num:03d}.html").write_text(
            page_content, encoding="utf-8"
        )
        print(f"Generated page-{page_num:03d}.html")

    # Calculate overall stats and collect all commits for timeline
    total_tool_counts = {}
    total_messages = 0
    all_commits = []  # (timestamp, hash, message, page_num, conv_index)
    for i, conv in enumerate(conversations):
        total_messages += len(conv["messages"])
        stats = analyze_conversation(conv["messages"])
        for tool, count in stats["tool_counts"].items():
            total_tool_counts[tool] = total_tool_counts.get(tool, 0) + count
        page_num = (i // PROMPTS_PER_PAGE) + 1
        for commit_hash, commit_msg, commit_ts in stats["commits"]:
            all_commits.append((commit_ts, commit_hash, commit_msg, page_num, i))
    total_tool_calls = sum(total_tool_counts.values())
    total_commits = len(all_commits)

    # Build timeline items: prompts and commits merged by timestamp
    timeline_items = []

    # Add prompts
    prompt_num = 0
    for i, conv in enumerate(conversations):
        if conv.get("is_continuation"):
            continue
        if conv["user_text"].startswith("Stop hook feedback:"):
            continue
        prompt_num += 1
        page_num = (i // PROMPTS_PER_PAGE) + 1
        msg_id = make_msg_id(conv["timestamp"])
        link = f"page-{page_num:03d}.html#{msg_id}"
        rendered_content = render_markdown_text(conv["user_text"])

        # Collect all messages including from subsequent continuation conversations
        # This ensures long_texts from continuations appear with the original prompt
        all_messages = list(conv["messages"])
        for j in range(i + 1, len(conversations)):
            if not conversations[j].get("is_continuation"):
                break
            all_messages.extend(conversations[j]["messages"])

        # Analyze conversation for stats (excluding commits from inline display now)
        stats = analyze_conversation(all_messages)
        tool_stats_str = format_tool_stats(stats["tool_counts"])

        long_texts_html = ""
        for lt in stats["long_texts"]:
            rendered_lt = render_markdown_text(lt)
            long_texts_html += _macros.index_long_text(rendered_lt)

        stats_html = _macros.index_stats(tool_stats_str, long_texts_html)

        item_html = _macros.index_item(
            prompt_num, link, conv["timestamp"], rendered_content, stats_html
        )
        timeline_items.append((conv["timestamp"], "prompt", item_html))

    # Add commits as separate timeline items
    for commit_ts, commit_hash, commit_msg, page_num, conv_idx in all_commits:
        item_html = _macros.index_commit(
            commit_hash, commit_msg, commit_ts, _github_repo
        )
        timeline_items.append((commit_ts, "commit", item_html))

    # Sort by timestamp
    timeline_items.sort(key=lambda x: x[0])
    index_items = [item[2] for item in timeline_items]

    index_pagination = generate_index_pagination_html(total_pages)
    index_template = get_template("index.html")
    index_content = index_template.render(
        css=CSS,
        js=JS,
        pagination_html=index_pagination,
        prompt_num=prompt_num,
        total_messages=total_messages,
        total_tool_calls=total_tool_calls,
        total_commits=total_commits,
        total_pages=total_pages,
        index_items_html="".join(index_items),
    )
    index_path = output_dir / "index.html"
    index_path.write_text(index_content, encoding="utf-8")
    print(
        f"Generated {index_path.resolve()} ({total_convs} prompts, {total_pages} pages)"
    )


@click.group(cls=DefaultGroup, default="local", default_if_no_args=True)
@click.version_option(None, "-v", "--version", package_name="claude-code-transcripts")
def cli():
    """Convert Claude Code session JSON to mobile-friendly HTML pages."""
    pass


@cli.command("shell-init")
@click.argument("shell", type=click.Choice(sorted(SHELL_WRAPPERS.keys())))
def shell_init_cmd(shell):
    """Print a shell wrapper function for `cct`.

    Install once by adding this line to your rc file::

        eval "$(cct shell-init zsh)"   # or bash

    The wrapper makes Enter (resume) leave your shell inside the project
    directory after claude exits. Without it, you stay in whichever
    directory you ran `cct` from — a Unix child process can't cd its
    parent shell.
    """
    click.echo(SHELL_WRAPPERS[shell], nl=False)


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
def local_cmd(output, output_auto, repo, gist, include_json, open_browser):
    """Select a local Claude Code session and either resume it or render
    it to HTML.

    The picker is two-step. First choose a project folder under
    ~/.claude/projects (questionary, with type-to-filter). Then choose a
    session within it (custom picker). On the session step:

    - Enter resumes the session — chdir to its original cwd and exec
      ``claude --dangerously-skip-permissions --resume <id>``.
    - h renders the session to HTML using the existing flags
      (``-o``, ``--gist``, ``--repo``, ``--open``, ``--json``).

    Project metadata is gathered without reading session summaries so the
    first picker stays cheap even with hundreds of sessions on disk.
    """
    projects_folder = Path.home() / ".claude" / "projects"

    if not projects_folder.exists():
        click.echo(f"Projects folder not found: {projects_folder}")
        click.echo("No local Claude Code sessions available.")
        return

    click.echo("Loading projects...")
    projects = find_local_projects(projects_folder)

    if not projects:
        click.echo("No local projects found.")
        return

    project_choices = [
        questionary.Choice(title=p["display"], value=p) for p in projects
    ]
    selected_project = questionary.select(
        "Select a project:",
        choices=project_choices,
        use_search_filter=True,
        use_jk_keys=False,
        show_selected=True,
    ).ask()

    if selected_project is None:
        click.echo("No project selected.")
        return

    # Read summaries only for the chosen project — the speed win.
    # `paths` is always a list (single-element for real projects, multi-element
    # for the virtual "Global Sessions" entry that merges ~/ and ~/Code).
    results = []
    for path in selected_project["paths"]:
        results.extend(find_local_sessions(path, limit=None))
    results.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)

    if not results:
        click.echo(f"No sessions in {selected_project['name']}.")
        return

    session_entries = []
    for filepath, summary in results:
        stat = filepath.stat()
        mod_time = datetime.fromtimestamp(stat.st_mtime)
        size_kb = stat.st_size / 1024
        date_str = mod_time.strftime("%Y-%m-%d %H:%M")
        if len(summary) > 50:
            summary = summary[:47] + "..."
        display = f"{date_str}  {size_kb:5.0f} KB  {summary}"
        session_entries.append({"display": display, "filepath": filepath})

    picked = select_session_action(session_entries)
    if picked is None:
        click.echo("No session selected.")
        return

    session_file, action = picked

    if action == "resume":
        # Replaces the current process — does not return.
        resume_session(session_file)
        return

    # action == "html"
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        # Use -o as parent dir (or current dir), with auto-named subdirectory
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


def is_url(path):
    """Check if a path is a URL (starts with http:// or https://)."""
    return path.startswith("http://") or path.startswith("https://")


def fetch_url_to_tempfile(url):
    """Fetch a URL and save to a temporary file.

    Returns the Path to the temporary file.
    Raises click.ClickException on network errors.
    """
    try:
        response = httpx.get(url, timeout=60.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.RequestError as e:
        raise click.ClickException(f"Failed to fetch URL: {e}")
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"Failed to fetch URL: {e.response.status_code} {e.response.reason_phrase}"
        )

    # Determine file extension from URL
    url_path = url.split("?")[0]  # Remove query params
    if url_path.endswith(".jsonl"):
        suffix = ".jsonl"
    elif url_path.endswith(".json"):
        suffix = ".json"
    else:
        suffix = ".jsonl"  # Default to JSONL

    # Extract a name from the URL for the temp file
    url_name = Path(url_path).stem or "session"

    temp_dir = Path(tempfile.gettempdir())
    temp_file = temp_dir / f"claude-url-{url_name}{suffix}"
    temp_file.write_text(response.text, encoding="utf-8")
    return temp_file


@cli.command("json")
@click.argument("json_file", type=click.Path())
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
    help="Auto-name output subdirectory based on filename (uses -o as parent, or current dir).",
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
    help="Include the original JSON session file in the output directory.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated index.html in your default browser (default if no -o specified).",
)
def json_cmd(json_file, output, output_auto, repo, gist, include_json, open_browser):
    """Convert a Claude Code session JSON/JSONL file or URL to HTML."""
    # Handle URL input
    if is_url(json_file):
        click.echo(f"Fetching {json_file}...")
        temp_file = fetch_url_to_tempfile(json_file)
        json_file_path = temp_file
        # Use URL path for naming
        url_name = Path(json_file.split("?")[0]).stem or "session"
    else:
        # Validate that local file exists
        json_file_path = Path(json_file)
        if not json_file_path.exists():
            raise click.ClickException(f"File not found: {json_file}")
        url_name = None

    # Determine output directory and whether to open browser
    # If no -o specified, use temp dir and open browser by default
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        # Use -o as parent dir (or current dir), with auto-named subdirectory
        parent_dir = Path(output) if output else Path(".")
        output = parent_dir / (url_name or json_file_path.stem)
    elif output is None:
        output = _temp_output_dir(f"claude-session-{url_name or json_file_path.stem}")

    output = Path(output)
    generate_html(json_file_path, output, github_repo=repo)

    # Show output directory
    click.echo(f"Output: {output.resolve()}")

    # Copy JSON file to output directory if requested
    if include_json:
        output.mkdir(exist_ok=True)
        json_dest = output / json_file_path.name
        shutil.copy(json_file_path, json_dest)
        json_size_kb = json_dest.stat().st_size / 1024
        click.echo(f"JSON: {json_dest} ({json_size_kb:.1f} KB)")

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


def resolve_credentials(token, org_uuid):
    """Resolve token and org_uuid from arguments or auto-detect.

    Returns (token, org_uuid) tuple.
    Raises click.ClickException if credentials cannot be resolved.
    """
    # Get token
    if token is None:
        token = get_access_token_from_keychain()
        if token is None:
            if platform.system() == "Darwin":
                raise click.ClickException(
                    "Could not retrieve access token from macOS keychain. "
                    "Make sure you are logged into Claude Code, or provide --token."
                )
            else:
                raise click.ClickException(
                    "On non-macOS platforms, you must provide --token with your access token."
                )

    # Get org UUID
    if org_uuid is None:
        org_uuid = get_org_uuid_from_config()
        if org_uuid is None:
            raise click.ClickException(
                "Could not find organization UUID in ~/.claude.json. "
                "Provide --org-uuid with your organization UUID."
            )

    return token, org_uuid


def format_session_for_display(session_data):
    """Format a session for display in the list or picker.

    Shows repo first (if available), then date, then title.
    Returns a formatted string.
    """
    title = session_data.get("title", "Untitled")
    created_at = session_data.get("created_at", "")
    repo = session_data.get("repo")
    # Truncate title if too long
    if len(title) > 50:
        title = title[:47] + "..."
    # Format: repo (or placeholder)  date  title
    repo_display = repo if repo else "(no repo)"
    date_display = created_at[:19] if created_at else "N/A"
    return f"{repo_display:30}  {date_display:19}  {title}"


def generate_html_from_session_data(session_data, output_dir, github_repo=None):
    """Generate HTML from session data dict (instead of file path)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True, parents=True)

    loglines = session_data.get("loglines", [])

    # Auto-detect GitHub repo if not provided
    if github_repo is None:
        github_repo = detect_github_repo(loglines)
        if github_repo:
            click.echo(f"Auto-detected GitHub repo: {github_repo}")

    # Set module-level variable for render functions
    global _github_repo
    _github_repo = github_repo

    conversations = []
    current_conv = None
    for entry in loglines:
        log_type = entry.get("type")
        timestamp = entry.get("timestamp", "")
        is_compact_summary = entry.get("isCompactSummary", False)
        message_data = entry.get("message", {})
        if not message_data:
            continue
        # Convert message dict to JSON string for compatibility with existing render functions
        message_json = json.dumps(message_data)
        is_user_prompt = False
        user_text = None
        if log_type == "user":
            content = message_data.get("content", "")
            text = extract_text_from_content(content)
            if text:
                is_user_prompt = True
                user_text = text
        if is_user_prompt:
            if current_conv:
                conversations.append(current_conv)
            current_conv = {
                "user_text": user_text,
                "timestamp": timestamp,
                "messages": [(log_type, message_json, timestamp)],
                "is_continuation": bool(is_compact_summary),
            }
        elif current_conv:
            current_conv["messages"].append((log_type, message_json, timestamp))
    if current_conv:
        conversations.append(current_conv)

    total_convs = len(conversations)
    total_pages = (total_convs + PROMPTS_PER_PAGE - 1) // PROMPTS_PER_PAGE

    for page_num in range(1, total_pages + 1):
        start_idx = (page_num - 1) * PROMPTS_PER_PAGE
        end_idx = min(start_idx + PROMPTS_PER_PAGE, total_convs)
        page_convs = conversations[start_idx:end_idx]
        messages_html = []
        for conv in page_convs:
            is_first = True
            for log_type, message_json, timestamp in conv["messages"]:
                msg_html = render_message(log_type, message_json, timestamp)
                if msg_html:
                    # Wrap continuation summaries in collapsed details
                    if is_first and conv.get("is_continuation"):
                        msg_html = f'<details class="continuation"><summary>Session continuation summary</summary>{msg_html}</details>'
                    messages_html.append(msg_html)
                is_first = False
        pagination_html = generate_pagination_html(page_num, total_pages)
        page_template = get_template("page.html")
        page_content = page_template.render(
            css=CSS,
            js=JS,
            page_num=page_num,
            total_pages=total_pages,
            pagination_html=pagination_html,
            messages_html="".join(messages_html),
        )
        (output_dir / f"page-{page_num:03d}.html").write_text(
            page_content, encoding="utf-8"
        )
        click.echo(f"Generated page-{page_num:03d}.html")

    # Calculate overall stats and collect all commits for timeline
    total_tool_counts = {}
    total_messages = 0
    all_commits = []  # (timestamp, hash, message, page_num, conv_index)
    for i, conv in enumerate(conversations):
        total_messages += len(conv["messages"])
        stats = analyze_conversation(conv["messages"])
        for tool, count in stats["tool_counts"].items():
            total_tool_counts[tool] = total_tool_counts.get(tool, 0) + count
        page_num = (i // PROMPTS_PER_PAGE) + 1
        for commit_hash, commit_msg, commit_ts in stats["commits"]:
            all_commits.append((commit_ts, commit_hash, commit_msg, page_num, i))
    total_tool_calls = sum(total_tool_counts.values())
    total_commits = len(all_commits)

    # Build timeline items: prompts and commits merged by timestamp
    timeline_items = []

    # Add prompts
    prompt_num = 0
    for i, conv in enumerate(conversations):
        if conv.get("is_continuation"):
            continue
        if conv["user_text"].startswith("Stop hook feedback:"):
            continue
        prompt_num += 1
        page_num = (i // PROMPTS_PER_PAGE) + 1
        msg_id = make_msg_id(conv["timestamp"])
        link = f"page-{page_num:03d}.html#{msg_id}"
        rendered_content = render_markdown_text(conv["user_text"])

        # Collect all messages including from subsequent continuation conversations
        # This ensures long_texts from continuations appear with the original prompt
        all_messages = list(conv["messages"])
        for j in range(i + 1, len(conversations)):
            if not conversations[j].get("is_continuation"):
                break
            all_messages.extend(conversations[j]["messages"])

        # Analyze conversation for stats (excluding commits from inline display now)
        stats = analyze_conversation(all_messages)
        tool_stats_str = format_tool_stats(stats["tool_counts"])

        long_texts_html = ""
        for lt in stats["long_texts"]:
            rendered_lt = render_markdown_text(lt)
            long_texts_html += _macros.index_long_text(rendered_lt)

        stats_html = _macros.index_stats(tool_stats_str, long_texts_html)

        item_html = _macros.index_item(
            prompt_num, link, conv["timestamp"], rendered_content, stats_html
        )
        timeline_items.append((conv["timestamp"], "prompt", item_html))

    # Add commits as separate timeline items
    for commit_ts, commit_hash, commit_msg, page_num, conv_idx in all_commits:
        item_html = _macros.index_commit(
            commit_hash, commit_msg, commit_ts, _github_repo
        )
        timeline_items.append((commit_ts, "commit", item_html))

    # Sort by timestamp
    timeline_items.sort(key=lambda x: x[0])
    index_items = [item[2] for item in timeline_items]

    index_pagination = generate_index_pagination_html(total_pages)
    index_template = get_template("index.html")
    index_content = index_template.render(
        css=CSS,
        js=JS,
        pagination_html=index_pagination,
        prompt_num=prompt_num,
        total_messages=total_messages,
        total_tool_calls=total_tool_calls,
        total_commits=total_commits,
        total_pages=total_pages,
        index_items_html="".join(index_items),
    )
    index_path = output_dir / "index.html"
    index_path.write_text(index_content, encoding="utf-8")
    click.echo(
        f"Generated {index_path.resolve()} ({total_convs} prompts, {total_pages} pages)"
    )


@cli.command("web")
@click.argument("session_id", required=False)
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
    help="Auto-name output subdirectory based on session ID (uses -o as parent, or current dir).",
)
@click.option("--token", help="API access token (auto-detected from keychain on macOS)")
@click.option(
    "--org-uuid", help="Organization UUID (auto-detected from ~/.claude.json)"
)
@click.option(
    "--repo",
    help="GitHub repo (owner/name). Filters session list and sets default for commit links.",
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
    help="Include the JSON session data in the output directory.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated index.html in your default browser (default if no -o specified).",
)
def web_cmd(
    session_id,
    output,
    output_auto,
    token,
    org_uuid,
    repo,
    gist,
    include_json,
    open_browser,
):
    """Select and convert a web session from the Claude API to HTML.

    If SESSION_ID is not provided, displays an interactive picker to select a session.
    """
    try:
        token, org_uuid = resolve_credentials(token, org_uuid)
    except click.ClickException:
        raise

    # If no session ID provided, show interactive picker
    if session_id is None:
        try:
            sessions_data = fetch_sessions(token, org_uuid)
        except httpx.HTTPStatusError as e:
            raise click.ClickException(
                f"API request failed: {e.response.status_code} {e.response.text}"
            )
        except httpx.RequestError as e:
            raise click.ClickException(f"Network error: {e}")

        sessions = sessions_data.get("data", [])
        if not sessions:
            raise click.ClickException("No sessions found.")

        # Enrich sessions with repo information (extracted from session metadata)
        sessions = enrich_sessions_with_repos(sessions)

        # Filter by repo if specified
        if repo:
            sessions = filter_sessions_by_repo(sessions, repo)
            if not sessions:
                raise click.ClickException(f"No sessions found for repo: {repo}")

        # Build choices for questionary
        choices = []
        for s in sessions:
            sid = s.get("id", "unknown")
            display = format_session_for_display(s)
            choices.append(questionary.Choice(title=display, value=sid))

        selected = questionary.select(
            "Select a session to import:",
            choices=choices,
        ).ask()

        if selected is None:
            # User cancelled
            raise click.ClickException("No session selected.")

        session_id = selected

    # Fetch the session
    click.echo(f"Fetching session {session_id}...")
    try:
        session_data = fetch_session(token, org_uuid, session_id)
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"API request failed: {e.response.status_code} {e.response.text}"
        )
    except httpx.RequestError as e:
        raise click.ClickException(f"Network error: {e}")

    # Determine output directory and whether to open browser
    # If no -o specified, use temp dir and open browser by default
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        # Use -o as parent dir (or current dir), with auto-named subdirectory
        parent_dir = Path(output) if output else Path(".")
        output = parent_dir / session_id
    elif output is None:
        output = _temp_output_dir(f"claude-session-{session_id}")

    output = Path(output)
    click.echo(f"Generating HTML in {output}/...")
    generate_html_from_session_data(session_data, output, github_repo=repo)

    # Show output directory
    click.echo(f"Output: {output.resolve()}")

    # Save JSON session data if requested
    if include_json:
        output.mkdir(exist_ok=True)
        json_dest = output / f"{session_id}.json"
        with open(json_dest, "w") as f:
            json.dump(session_data, f, indent=2)
        json_size_kb = json_dest.stat().st_size / 1024
        click.echo(f"JSON: {json_dest} ({json_size_kb:.1f} KB)")

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


@cli.command("all")
@click.option(
    "-s",
    "--source",
    type=click.Path(exists=True),
    help="Source directory containing Claude projects (default: ~/.claude/projects).",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    default="./claude-archive",
    help="Output directory for the archive (default: ./claude-archive).",
)
@click.option(
    "--include-agents",
    is_flag=True,
    help="Include agent-* session files (excluded by default).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be converted without creating files.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated archive in your default browser.",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help="Suppress all output except errors.",
)
def all_cmd(source, output, include_agents, dry_run, open_browser, quiet):
    """Convert all local Claude Code sessions to a browsable HTML archive.

    Creates a directory structure with:
    - Master index listing all projects
    - Per-project pages listing sessions
    - Individual session transcripts
    """
    # Default source folder
    if source is None:
        source = Path.home() / ".claude" / "projects"
    else:
        source = Path(source)

    if not source.exists():
        raise click.ClickException(f"Source directory not found: {source}")

    output = Path(output)

    if not quiet:
        click.echo(f"Scanning {source}...")

    projects = find_all_sessions(source, include_agents=include_agents)

    if not projects:
        if not quiet:
            click.echo("No sessions found.")
        return

    # Calculate totals
    total_sessions = sum(len(p["sessions"]) for p in projects)

    if not quiet:
        click.echo(f"Found {len(projects)} projects with {total_sessions} sessions")

    if dry_run:
        # Dry-run always outputs (it's the point of dry-run), but respects --quiet
        if not quiet:
            click.echo("\nDry run - would convert:")
            for project in projects:
                click.echo(
                    f"\n  {project['name']} ({len(project['sessions'])} sessions)"
                )
                for session in project["sessions"][:3]:  # Show first 3
                    mod_time = datetime.fromtimestamp(session["mtime"])
                    click.echo(
                        f"    - {session['path'].stem} ({mod_time.strftime('%Y-%m-%d')})"
                    )
                if len(project["sessions"]) > 3:
                    click.echo(f"    ... and {len(project['sessions']) - 3} more")
        return

    if not quiet:
        click.echo(f"\nGenerating archive in {output}...")

    # Progress callback for non-quiet mode
    def on_progress(project_name, session_name, current, total):
        if not quiet and current % 10 == 0:
            click.echo(f"  Processed {current}/{total} sessions...")

    # Generate the archive using the library function
    stats = generate_batch_html(
        source,
        output,
        include_agents=include_agents,
        progress_callback=on_progress,
    )

    # Report any failures
    if stats["failed_sessions"]:
        click.echo(f"\nWarning: {len(stats['failed_sessions'])} session(s) failed:")
        for failure in stats["failed_sessions"]:
            click.echo(
                f"  {failure['project']}/{failure['session']}: {failure['error']}"
            )

    if not quiet:
        click.echo(
            f"\nGenerated archive with {stats['total_projects']} projects, "
            f"{stats['total_sessions']} sessions"
        )
        click.echo(f"Output: {output.resolve()}")

    if open_browser:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)


def main():
    cli()
