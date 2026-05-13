"""
features/shell_init/command.py — the ``cad shell-init`` click subcommand.
"""

import click

from .wrappers import SHELL_WRAPPERS


@click.command("shell-init")
@click.argument("shell", type=click.Choice(sorted(SHELL_WRAPPERS.keys())))
def shell_init_cmd(shell):
    """Print a shell wrapper function for `cad`.

    Install once by adding this line to your rc file::

        eval "$(cad shell-init zsh)"   # or bash

    The wrapper makes Enter (resume) leave your shell inside the project
    directory after the agent exits. Without it, you stay in whichever
    directory you ran `cad` from — a Unix child process can't cd its
    parent shell.
    """
    click.echo(SHELL_WRAPPERS[shell], nl=False)
