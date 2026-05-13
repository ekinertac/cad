"""
features/project_rename/ — the project-level rename machinery used by
the ``r`` shortcut on the project picker.

Isolated in its own feature because the risk surface is unusual:
:func:`migrate_claude_project` moves files claude may be actively
writing to, rewrites embedded ``cwd`` fields in JSONLs, and creates a
backup tree that the user relies on if something goes wrong.
Touching this code requires extra care; pulling it into one
directory makes that boundary explicit.

There's no click subcommand exposed by this feature (you reach it
through the project picker), so this package doesn't currently have
a ``register(cli)`` function — callers import :func:`migrate_claude_project`
directly.
"""

from .migrate import (
    _CLAUDE_STATE_DIRS,
    _claude_encode_path,
    migrate_claude_project,
)


__all__ = [
    "_CLAUDE_STATE_DIRS",
    "_claude_encode_path",
    "migrate_claude_project",
]
