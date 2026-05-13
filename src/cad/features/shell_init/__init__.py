"""
features/shell_init/ — the ``cad shell-init zsh|bash`` subcommand.

Prints a small shell wrapper function that intercepts ``cad`` calls,
captures the ``$CAD_CWD_FILE`` the agent writes on exit, and ``cd``\\s
the parent shell. Without this, a child process can't change its
parent's working directory and the user stays in whichever folder
they invoked ``cad`` from.

Two modules:

- :mod:`wrappers`: the literal shell snippets (zsh + bash).
- :mod:`command`: the click subcommand.

Public surface: :func:`register` + ``SHELL_WRAPPERS`` for callers
that need the dict directly.
"""

from .wrappers import SHELL_WRAPPERS


def register(cli):
    """Attach `cad shell-init` to the click group."""
    from .command import shell_init_cmd

    cli.add_command(shell_init_cmd, name="shell-init")


__all__ = ["SHELL_WRAPPERS", "register"]
