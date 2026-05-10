# cct — multi-agent coding-session manager

[![Tests](https://github.com/ekinertac/claude-code-transcripts/workflows/Test/badge.svg)](https://github.com/ekinertac/claude-code-transcripts/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/ekinertac/claude-code-transcripts/blob/main/LICENSE)

`cct` discovers and manages local coding-agent sessions across **claude**, **codex**, **pi**, **opencode**, and **forge**. Two-step picker (project → session), grouped by working directory so sessions from any agent in the same project land under one entry. Resume any session in its original agent, rename it, summarize it via LLM, peek at the prompts/replies in `$PAGER`, or render the full transcript to HTML.

Originally a fork of Simon Willison's [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) (the HTML transcript renderer is still in here). The session-manager UX, multi-provider discovery, and overrides are this project's work.

## Installation

Install from this repo using `uv`:
```bash
uv tool install --from git+https://github.com/ekinertac/claude-code-transcripts cct
```
Or for local development:
```bash
git clone https://github.com/ekinertac/claude-code-transcripts
cd claude-code-transcripts
uv tool install --from . claude-code-transcripts
```

## Usage

This tool converts Claude Code session files into browseable multi-page HTML transcripts.

There are four commands available:

- `local` (default) - select from local Claude Code sessions stored in `~/.claude/projects`
- `web` - select from web sessions via the Claude API
- `json` - convert a specific JSON or JSONL session file
- `all` - convert all local sessions to a browsable HTML archive

The quickest way to view a recent local session:

```bash
claude-code-transcripts
```

This shows an interactive picker to select a session, generates HTML, and opens it in your default browser.

### Output options

All commands support these options:

- `-o, --output DIRECTORY` - output directory (default: writes to temp dir and opens browser)
- `-a, --output-auto` - auto-name output subdirectory based on session ID or filename
- `--repo OWNER/NAME` - GitHub repo for commit links (auto-detected if not specified). For `web` command, also filters the session list.
- `--open` - open the generated `index.html` in your default browser (default if no `-o` specified)
- `--gist` - upload the generated HTML files to a GitHub Gist and output a preview URL
- `--json` - include the original session file in the output directory

The generated output includes:
- `index.html` - an index page with a timeline of prompts and commits
- `page-001.html`, `page-002.html`, etc. - paginated transcript pages

### Local sessions

`cct` discovers sessions from five agent CLIs:

- **Claude Code** — `~/.claude/projects/*/<uuid>.jsonl`
- **Codex CLI** — `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`
- **pi** — `~/.pi/agent/sessions/<encoded>/<ts>_<uuid>.jsonl`
- **opencode** — `~/.local/share/opencode/opencode.db` (SQLite)
- **forge** — `~/forge/.forge.db` (SQLite)

Sessions are grouped by their recorded working directory (cwd), so sessions from any agent in the same project share one entry. Each row's badge shows per-provider counts: `(6c+5o+1f)` = 6 claude + 5 opencode + 1 forge.

User-named sessions (claude `/rename`, pi `--name`) are surfaced in the picker as `claude/MyName — <prompt>` so they're easy to spot.

Run with no arguments to launch a two-step picker — first choose a project, then a session within it:

```bash
cct
# or explicitly:
cct local
```

The first picker lists every project folder under `~/.claude/projects`, sorted by most recent activity, with each project's session count. Type to filter. Once you pick a project, every session in it is shown (newest first), so older sessions stay reachable.

On the session step:

- **Enter** resumes the session — `cd`s into the project's original working directory and execs the right agent CLI for that session:
  - claude: `claude --dangerously-skip-permissions --resume <id>`
  - codex: `codex resume <id>`
  - pi: `pi --session <id>`
  - opencode: `opencode --session <id>`
  - forge: `forge --conversation-id <id>`
  Skip-permissions on claude is intentional: long sessions devolve into rubber-stamping permission prompts.
- **h** renders the session to HTML (claude only — other providers' schemas need separate renderers).
- **r** renames the session — prompts inline for a new title, saves it to `~/.cct/titles.json` keyed by `<provider>:<session_id>`, and re-enters the picker. The override wins over whatever summary the provider would otherwise show. Works across all providers.
- **m** moves the session into a different project — prompts for a new directory, validates it exists, and saves a cwd override to `~/.cct/cwd-overrides.json`. On the next `cct` run, the session shows up under the new project. Useful when you started a session at `~/Code` and later created a subfolder for it. Empty input clears the override. Agent files are never modified.
- **p** peeks into the session — opens its user prompts and assistant replies in `$PAGER` (falls back to `less -R`). Tool calls and system metadata are filtered out so it reads like a chat transcript. Press `q` in the pager and you're back on the same row in cct. Currently supports claude / codex / pi (JSONL providers); opencode and forge sessions print a "not yet supported" message.
- **s** auto-summarizes by piping a session excerpt to `codex exec`, saves the returned title to the same sidecar. Uses codex's ChatGPT-account auth, so no API key needed and credit balance isn't an issue.
- **/** opens search-filter mode (type to filter, `Enter` confirms+resumes, `Esc` exits search).
- **Esc** cancels.

#### Stay in the project folder after claude exits

A child process can't change its parent shell's working directory, so by default your shell returns to wherever you ran `cct` from once claude exits. To make Enter leave you inside the project directory, install the shell wrapper once:

```bash
# zsh
eval "$(cct shell-init zsh)"  >> ~/.zshrc

# bash
eval "$(cct shell-init bash)" >> ~/.bashrc
```

Reload the shell after editing the rc file. The wrapper passes a temp file via `CCT_CWD_FILE`; `cct` writes the project path there before exec'ing claude, and the wrapper `cd`s your shell to it on exit.

### Web sessions

Import sessions directly from the Claude API:

```bash
# Interactive session picker
claude-code-transcripts web

# Import a specific session by ID
claude-code-transcripts web SESSION_ID

# Import and publish to gist
claude-code-transcripts web SESSION_ID --gist
```

The session picker displays sessions grouped by their associated GitHub repository:

```
simonw/datasette              2025-01-15T10:30:00  Fix the bug in query parser
simonw/llm                    2025-01-14T09:00:00  Add streaming support
(no repo)                     2025-01-13T14:22:00  General coding session
```

Use `--repo` to filter the session list to a specific repository:

```bash
claude-code-transcripts web --repo simonw/datasette
```

On macOS, API credentials are automatically retrieved from your keychain (requires being logged into Claude Code). On other platforms, provide `--token` and `--org-uuid` manually.

### Publishing to GitHub Gist

Use the `--gist` option to automatically upload your transcript to a GitHub Gist and get a shareable preview URL:

```bash
claude-code-transcripts --gist
claude-code-transcripts web --gist
claude-code-transcripts json session.json --gist
```

This will output something like:
```
Gist: https://gist.github.com/username/abc123def456
Preview: https://gisthost.github.io/?abc123def456/index.html
Files: /var/folders/.../session-id
```

The preview URL uses [gisthost.github.io](https://gisthost.github.io/) to render your HTML gist. The tool automatically injects JavaScript to fix relative links when served through gisthost.

Combine with `-o` to keep a local copy:

```bash
claude-code-transcripts json session.json -o ./my-transcript --gist
```

**Requirements:** The `--gist` option requires the [GitHub CLI](https://cli.github.com/) (`gh`) to be installed and authenticated (`gh auth login`).

### Auto-naming output directories

Use `-a/--output-auto` to automatically create a subdirectory named after the session:

```bash
# Creates ./session_ABC123/ subdirectory
claude-code-transcripts web SESSION_ABC123 -a

# Creates ./transcripts/session_ABC123/ subdirectory
claude-code-transcripts web SESSION_ABC123 -o ./transcripts -a
```

### Including the source file

Use the `--json` option to include the original session file in the output directory:

```bash
claude-code-transcripts json session.json -o ./my-transcript --json
```

This will output:
```
JSON: ./my-transcript/session_ABC.json (245.3 KB)
```

This is useful for archiving the source data alongside the HTML output.

### Converting from JSON/JSONL files

Convert a specific session file directly:

```bash
claude-code-transcripts json session.json -o output-directory/
claude-code-transcripts json session.jsonl --open
```
This works with both JSONL files in the `~/.claude/projects/` folder and JSON session files extracted from Claude Code for web.

The `json` command can take a URL to a JSON or JSONL file as an alternative to a path on disk.

### Converting all sessions

Convert all your local Claude Code sessions to a browsable HTML archive:

```bash
claude-code-transcripts all
```

This creates a directory structure with:
- A master index listing all projects
- Per-project pages listing sessions
- Individual session transcripts

Options:

- `-s, --source DIRECTORY` - source directory (default: `~/.claude/projects`)
- `-o, --output DIRECTORY` - output directory (default: `./claude-archive`)
- `--include-agents` - include agent session files (excluded by default)
- `--dry-run` - show what would be converted without creating files
- `--open` - open the generated archive in your default browser
- `-q, --quiet` - suppress all output except errors

Examples:

```bash
# Preview what would be converted
claude-code-transcripts all --dry-run

# Convert all sessions and open in browser
claude-code-transcripts all --open

# Convert to a specific directory
claude-code-transcripts all -o ./my-archive

# Include agent sessions
claude-code-transcripts all --include-agents
```

## Development

To contribute to this tool, first checkout the code. You can run the tests using `uv run`:
```bash
cd claude-code-transcripts
uv run pytest
```
And run your local development copy of the tool like this:
```bash
uv run claude-code-transcripts --help
```
