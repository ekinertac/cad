"""
features/web/ — the ``cad web`` subcommand for claude-for-web sessions.

Pulls session data from the Anthropic API (rather than from local
JSONLs), renders it through the same HTML pipeline ``cad json`` /
``cad all`` use, and offers an interactive picker when no session id
is provided.

Note: this feature is currently broken upstream — see
simonw/claude-code-transcripts#77 — but the structure is intact for
the day it's revived.

Modules:

- :mod:`credentials`: macOS-keychain access token + ``~/.claude.json``
  org UUID, plus :func:`resolve_credentials` which combines them.
- :mod:`api`: HTTP client helpers (``fetch_sessions``, ``fetch_session``)
  and the API constants.
- :mod:`repos`: extract / enrich / filter sessions by GitHub repo.
- :mod:`display`: :func:`format_session_for_display` for the picker
  rows and :func:`generate_html_from_session_data` (the adapter
  feeding API-shape sessions through the HTML render pipeline).
- :mod:`command`: the click subcommand.

Public surface: :func:`register` for the CLI, plus the data-shape
helpers that tests import directly.
"""

from .api import (
    API_BASE_URL,
    ANTHROPIC_VERSION,
    fetch_session,
    fetch_sessions,
    get_api_headers,
)
from .credentials import (
    CredentialsError,
    get_access_token_from_keychain,
    get_org_uuid_from_config,
    resolve_credentials,
)
from .display import format_session_for_display
from .repos import (
    enrich_sessions_with_repos,
    extract_repo_from_session,
    filter_sessions_by_repo,
)


def register(cli):
    """Attach `cad web` to the click group."""
    from .command import web_cmd

    cli.add_command(web_cmd, name="web")


__all__ = [
    "ANTHROPIC_VERSION",
    "API_BASE_URL",
    "CredentialsError",
    "enrich_sessions_with_repos",
    "extract_repo_from_session",
    "fetch_session",
    "fetch_sessions",
    "filter_sessions_by_repo",
    "format_session_for_display",
    "get_access_token_from_keychain",
    "get_api_headers",
    "get_org_uuid_from_config",
    "register",
    "resolve_credentials",
]
