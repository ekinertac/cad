"""
features/ — one directory per cad command surface (live, local, html,
web, shell_init, project_rename).

Each subpackage exports a ``register(cli)`` function that the
top-level CLI wires up. To remove a feature: delete its directory
and drop the matching ``register()`` call. To add one: mirror the
shape of an existing directory.

Features may import from ``core/`` freely. They may import from each
other only via explicitly-exported public surfaces (each feature's
``__init__.py``) — never reach into another feature's internals.
"""
