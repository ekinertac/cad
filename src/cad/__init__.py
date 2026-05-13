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

# Local actions (peek + summarize) live in features/local/. Re-exported
# at the cad top level because features/live and the existing tests
# reach for `cad.peek_session` / `cad.summarize_session` directly.
from .features.local import peek_session, summarize_session  # noqa: E402,F401


# Register feature commands. Each features/<name>/__init__.py exports a
# register(cli) hook so subcommands plug in here without __init__.py
# having to know the internals. To remove a feature: delete its
# directory and remove the corresponding register() call.
from .features import html as _html_feature  # noqa: E402
from .features import live as _live_feature  # noqa: E402
from .features import local as _local_feature  # noqa: E402
from .features import shell_init as _shell_init_feature  # noqa: E402
from .features import web as _web_feature  # noqa: E402

_local_feature.register(cli)
_live_feature.register(cli)
_shell_init_feature.register(cli)
_web_feature.register(cli)
_html_feature.register(cli)

def main():
    cli()
