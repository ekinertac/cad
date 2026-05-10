# cad — Coding Agent Driver

[![Tests](https://github.com/ekinertac/cad/workflows/Test/badge.svg)](https://github.com/ekinertac/cad/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/ekinertac/cad/blob/main/LICENSE)

One CLI to drive every coding agent on your machine. `cad` discovers local sessions from **claude**, **codex**, **pi**, **opencode**, and **forge**, groups them by working directory, and lets you resume / rename / summarize / move / peek / render them — all from a single picker.

```
» self-healing-crawler         2026-05-11 13:34   15 sessions  (9c+1p+1o+3f)
  arcade                       2026-05-11 12:18   12 sessions  (6c+5o+1f)
  Global Sessions              2026-05-10 23:40   95 sessions  (34c+3x+3p+50o+5f)
  ...
```

## Install

```bash
git clone https://github.com/ekinertac/cad
cd cad
uv tool install --from . cad
```

Optional: `eval "$(cad shell-init zsh)"` in your rc file makes Enter (resume) leave your shell in the project's directory after the agent exits. (`bash` works too.)

## What it does

Run `cad` with no arguments. You get a two-step picker.

**Step 1 — projects.** Every directory where you have any agent sessions, sorted by most-recent activity. Provider badge per row: `(6c+5o+1f)` = 6 claude + 5 opencode + 1 forge. Sessions from any agent that share a working directory share an entry. Press **`r`** to rename a project end-to-end: `mv` the user folder, move claude's `~/.claude/{projects,file-history,todos,shell-snapshots}/` state, rewrite the embedded `cwd` in every JSONL, and back everything up to `~/.cad/agent-backups/` so a mistake is one `cp -R` away from being undone. *Do not run while the project's live claude session is open* — it moves files claude is actively writing.

**Step 2 — sessions in the chosen project.** Each row shows date, size, provider prefix, and a title (the first user prompt, your `/rename` text, or a cad-set override).

Both pickers support `/` for type-to-filter search.

## Session shortcuts

On the session picker:

| key | action |
|---|---|
| `Enter` | **Resume** in the right agent. `cd`s to the recorded cwd, then execs the agent's resume command (`claude --resume`, `codex resume`, `pi --session`, `opencode --session`, `forge --conversation-id`). For claude, skip-permissions is on by default — alarm fatigue is real. |
| `h` | **Render** the session to a paginated HTML transcript (claude only — other schemas need separate renderers). Honors `-o`, `--gist`, `--open`, `--json`. |
| `r` | **Rename**: prompt for a new title, save it to `~/.cad/titles.json`. Wins over the provider's own summary. |
| `s` | **Summarize** via LLM: pipes a session excerpt to `codex exec --ephemeral` (uses your ChatGPT-account auth, no API credits needed) and saves the 3-7 word title it returns. |
| `m` | **Move** to a different project: prompt for a new cwd, validate, save to `~/.cad/cwd-overrides.json`. Sessions that drift across folders (started in `~/Code`, became their own project) get re-homed. Agent files are never modified. |
| `p` | **Peek**: opens prompts/assistant replies (no tool noise) in `$PAGER`. Press `q` and you're back on the same row. |
| `/` | Search-filter mode. Type to narrow, Enter selects, Esc exits search. |
| `Esc` / `Backspace` | Back to the project picker. |
| `q` / `Ctrl-C` | Quit. |

Rows touched in the current session (rename/summarize/move) render bright green so you can see what just changed.

## Where things live

| What | Path |
|---|---|
| User title overrides | `~/.cad/titles.json` |
| Cwd overrides (moves) | `~/.cad/cwd-overrides.json` |
| Temp HTML output | `$TMPDIR/cad/<session-stem>/` (auto-pruned to last 20) |
| Old name compat | `~/.cct/` auto-migrates to `~/.cad/` on first launch |

cad reads from each agent's own storage; it never writes back. Agent files (`~/.claude/projects/...`, `~/.codex/sessions/...`, `~/.local/share/opencode/opencode.db`, etc.) are read-only from cad's perspective.

## Sub-commands

Most of the time you just run `cad`. The other commands are HTML-rendering carryovers from the original tool this grew out of:

- `cad` / `cad local` — the picker described above (default)
- `cad json <file>` — render a specific JSONL/JSON file to HTML; accepts a URL too
- `cad all` — bulk-render every claude session to a browsable archive
- `cad web [<session-id>]` — claude-for-web sessions via the API (currently broken upstream, see [simonw/claude-code-transcripts#77](https://github.com/simonw/claude-code-transcripts/issues/77))
- `cad shell-init zsh|bash` — print the wrapper function for post-exit `cd`

`cad <subcommand> --help` for details.

## Adding a provider

Each agent's discovery is one function returning a list of session dicts with `provider`, `session_id`, `cwd`, `filepath`, `mtime`. Wiring a new one is:

1. Add `find_<provider>_sessions()` returning that dict shape
2. Add a one-letter badge in `PROVIDER_BADGES`
3. Add the resume invocation in `PROVIDER_RESUME_COMMANDS`
4. (Optional) Add a summary extractor for `load_session_summary` / peek

## Development

```bash
uv run pytest          # 185 tests
uv run black .         # format before commit
uv run cad             # run the dev copy
```

## Credit

The paginated HTML transcript renderer began as Simon Willison's [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) — that piece lives mostly intact under `cad json` / `cad all`. Everything around it (the picker, provider abstraction, overrides, peek/resume/summarize/move) is this project's own work.

Apache 2.0.
