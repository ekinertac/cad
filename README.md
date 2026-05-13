# cad — Coding Agent Driver

[![Tests](https://github.com/ekinertac/cad/workflows/Test/badge.svg)](https://github.com/ekinertac/cad/actions?query=workflow%3ATest)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/ekinertac/cad/blob/main/LICENSE)

One CLI to drive every coding agent on your machine. cad discovers local sessions from **claude**, **codex**, **pi**, **opencode**, and **forge**, groups them by working directory, and gives you two views: a project-based picker for resuming / renaming / rendering past work, and a live dashboard for the agents actually running right now.

## Two modes

**`cad`** — project picker → session picker. The everyday view.

```
» self-healing-crawler         2026-05-11 13:34   15 sessions  (9c+1p+1o+3f)
  arcade                       2026-05-11 12:18   12 sessions  (6c+5o+1f)
  Global Sessions              2026-05-10 23:40   95 sessions  (34c+3x+3p+50o+5f)
```

**`cad live`** — full-screen dashboard of every agent process running right now, grouped by project, auto-refreshing.

```
alpha
» ● [working]  claude/Wire EmulatorJS into Arcade Vault Catalog
  ● [idle]     claude/ArcadeVault — read @HANDOFF.md

self-healing-crawler
  ● [input]    claude/healing-2 — do you know what we were working on?

humbl.ai
  ● [input]    claude/Verify production API processes on remote server
```

## Install

```bash
git clone https://github.com/ekinertac/cad
cd cad
uv tool install --from . cad
```

Optional: `eval "$(cad shell-init zsh)"` in your rc file makes Enter (resume) leave your shell in the project's directory after the agent exits. (`bash` works too.)

---

## `cad` — project work

Run `cad` with no arguments.

**Auto-pick.** If your shell is inside a known project's directory (or a subdirectory of one), cad skips the project picker and drops you straight into that project's session list. The common case becomes one keystroke. Pass `--all` to bypass the auto-pick when you want to see the full project list, or press Esc/Bksp once you're in to back out. Launching from `~/` or `~/Code` always shows the picker.

**Project picker.** Every directory where you have any agent sessions, sorted by most-recent activity. Provider badge per row: `(6c+5o+1f)` = 6 claude + 5 opencode + 1 forge. Sessions from any agent that share a working directory share an entry. Shortcuts:

- **`n`** — start a new claude session in this project (cd + exec).
- **`r`** — rename a project end-to-end: `mv` the user folder, move claude's `~/.claude/{projects,file-history,todos,shell-snapshots}/` state, rewrite the embedded `cwd` in every JSONL, and back everything up to `~/.cad/agent-backups/` so a mistake is one `cp -R` away from being undone. *Do not run while the project's live claude session is open* — it moves files claude is actively writing.

**Session picker.** Each row shows date, size, provider prefix, and a title (the first user prompt, your `/rename` text, or a cad-set override). Programmatic `claude -p` sessions (from your SessionEnd hooks etc.) are hidden, matching `claude -r`'s own behavior. Sessions touched in the current cad run (rename / summarize / move) render bright green so you can see what just changed.

**Live indicator on this view too.** Sessions belonging to a currently-running claude process render a coloured dot inline — green = `[working]` (last JSONL write within 10s), yellow = `[input]` (alive but waiting for you), grey = `[idle]` (alive but stale 5+ min). Project rows show `[N live]` so you can tell which projects have active work without drilling in.

Both pickers support `/` for type-to-filter search.

### Session shortcuts

| key | action |
|---|---|
| `Enter` | **Resume** in the right agent. `cd`s to the recorded cwd, then execs the agent's resume command (`claude --resume`, `codex resume`, `pi --session`, `opencode --session`, `forge --conversation-id`). For claude, skip-permissions is on by default. Refuses if the session is already live in another terminal — two agents on one JSONL would corrupt it. |
| `n` | **New** claude session in this project's cwd (no `--resume`). |
| `h` | **Render** the session to a paginated HTML transcript (claude only). Honors `-o`, `--gist`, `--open`, `--json`. |
| `r` | **Rename**: prompt for a new title, save it to `~/.cad/titles.json`. Wins over the provider's own summary. |
| `s` | **Summarize** via LLM: pipes a session excerpt to `codex exec --ephemeral` (uses your ChatGPT-account auth, no API credits) and saves the 3-7 word title it returns. |
| `m` | **Move** to a different project: prompt for a new cwd, validate, save to `~/.cad/cwd-overrides.json`. For sessions that drift across folders. Agent files are never modified. |
| `p` | **Peek**: opens prompts/assistant replies (no tool noise) in `$PAGER`, scrolled to the most recent turn. Press `q` and you're back on the same row. |
| `/` | Search-filter mode. Type to narrow, Enter selects, Esc exits search. |
| `Esc` / `Backspace` | Back to the project picker. |
| `q` / `Ctrl-C` | Quit. |

---

## `cad live` — running-process dashboard

Full-screen view of every running agent, grouped by project. Refreshes every 2 seconds — `[working]` / `[input]` / `[idle]` transitions surface without re-running.

**Detection.** `pgrep -x claude` finds running claude processes; `lsof -Pn` reads each one's cwd; `ps` reads its argv. Claudes launched with `--resume <uuid>` are bound to that exact session. Claudes without a `--resume` arg (a fresh `claude` in some cwd) are matched heuristically to the N most recently-modified JSONLs in that cwd. The whole detection step is budgeted at 2 seconds wall-clock and degrades gracefully where any of those tools aren't installed.

**Enter behaviour.** Enter brings the terminal tab running the highlighted session to the front.

- **iTerm2** (today): cad resolves the claude PID's tty via `ps`, then runs an AppleScript that selects the iTerm2 session whose `tty` property matches.
- **Other terminals**: cad falls back to peek — read-only snapshot of the conversation in `$PAGER`. Enter is never a silent no-op.

Resume is intentionally **not** bound on this view. Every row is by definition a process with an open file handle on its JSONL; spawning a second agent on the same file scrambles the conversation. To resume safely, close the original terminal first, then `cad local`.

**No pagination.** The window grows to fit all live sessions — no `▼ N more below` cutting things off when you've got a dozen agents running.

---

## Sub-commands

```bash
cad <subcommand> --help    # for details
```

| | |
|---|---|
| `cad` / `cad local` | Project picker → session picker. The default. |
| `cad live` | Running-process dashboard, described above. |
| `cad json <file>` | Render a specific JSONL/JSON file to HTML. Accepts a URL too. |
| `cad all` | Bulk-render every claude session to a browsable archive. |
| `cad web [<session-id>]` | Claude-for-web sessions via the API. Currently broken upstream — see [simonw/claude-code-transcripts#77](https://github.com/simonw/claude-code-transcripts/issues/77). |
| `cad shell-init zsh\|bash` | Print the shell wrapper for post-exit `cd`. |

## Where things live

| What | Path |
|---|---|
| User title overrides | `~/.cad/titles.json` |
| Cwd overrides (moves) | `~/.cad/cwd-overrides.json` |
| Rename backups | `~/.cad/agent-backups/<timestamp>/` |
| Temp HTML output | `$TMPDIR/cad/<session-stem>/` (auto-pruned to last 20) |
| Old name compat | `~/.cct/` auto-migrates to `~/.cad/` on first launch |

cad reads from each agent's own storage; it never writes back. Agent files (`~/.claude/projects/...`, `~/.codex/sessions/...`, `~/.local/share/opencode/opencode.db`, etc.) are read-only from cad's perspective.

## Adding a provider

Each agent's discovery is one function returning a list of session dicts with `provider`, `session_id`, `cwd`, `filepath`, `mtime`. Wiring a new one is:

1. Add `find_<provider>_sessions()` returning that dict shape
2. Add a one-letter badge in `PROVIDER_BADGES`
3. Add the resume invocation in `PROVIDER_RESUME_COMMANDS`
4. (Optional) Add a summary extractor for `load_session_summary` / peek

Adding terminal integration for `cad live`'s Enter follows a similar shape — see `focus_live_session()` for the iTerm2 implementation; the user's [agamon](https://github.com/ekinertac/agamon) terminal is next on the list.

## Development

```bash
uv run pytest          # 221 tests
uv run black .         # format before commit
uv run cad             # run the dev copy
```

## Credit

The paginated HTML transcript renderer began as Simon Willison's [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) — that piece lives mostly intact under `cad json` / `cad all`. Everything around it (the picker, provider abstraction, overrides, peek/resume/summarize/move, `cad live`) is this project's own work.

Apache 2.0.
