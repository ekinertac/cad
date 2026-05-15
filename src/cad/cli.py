"""
cli.py — the click command group and the feature register loop.

This module owns nothing functional; it just composes the cli surface
out of the registered features. Adding a feature means:

1. Creating ``features/<name>/`` with a ``register(cli)`` function
   that calls ``cli.add_command(...)``.
2. Adding two lines below (one import + one register call).

Removing a feature is the inverse — delete the directory, drop the
two lines, and the subcommand is gone. No knowledge of the
feature's internals lives here.

The :func:`main` entry point at the bottom is what
``pyproject.toml``'s ``[project.scripts]`` points at.
"""

import click
from click_default_group import DefaultGroup


@click.group(cls=DefaultGroup, default="local", default_if_no_args=True)
@click.version_option(None, "-v", "--version", package_name="cad")
def cli():
    """cad — Coding Agent Driver. Manage sessions across claude, codex,
    pi, opencode, and forge from one picker, or render Claude Code
    sessions to HTML."""
    pass


# Register each feature's subcommand(s) with the click group.
# Order matters only for help-text ordering — runtime behaviour is
# the same regardless. We put `local` first because it's the
# default (and the most-used) command.
from .features import archive as _archive_feature  # noqa: E402
from .features import html as _html_feature  # noqa: E402
from .features import live as _live_feature  # noqa: E402
from .features import local as _local_feature  # noqa: E402
from .features import search as _search_feature  # noqa: E402
from .features import shell_init as _shell_init_feature  # noqa: E402


_local_feature.register(cli)
_live_feature.register(cli)
_shell_init_feature.register(cli)
_html_feature.register(cli)
_archive_feature.register(cli)
_search_feature.register(cli)


def main():
    """Entry point used by ``pyproject.toml``'s ``[project.scripts]``."""
    cli()
