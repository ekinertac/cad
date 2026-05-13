"""
core/util.py — small, dependency-light helpers shared across cad.

Three families of utility live here:

- ``_loading_message``: a context manager that prints a placeholder
  line before a slow operation and erases it on exit, so we don't
  leave "Loading projects..." in scrollback above the picker.
- ``_atomic_write_json``: safe JSON writes with a one-step ``.bak``,
  used by every sidecar file under ``~/.cad/``.
- ``_temp_output_dir`` / ``_prune_temp_outputs``: ``$TMPDIR/cad/``
  housekeeping for the HTML renderer so the user's tmp directory
  doesn't accumulate transcripts between OS-level temp sweeps.

This module imports only from the stdlib plus ``click`` (for echoing
through click's stream). It does NOT import from any other cad module
— anything in here must be safe to import from any layer above.
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import click


# Subdir under $TMPDIR that holds every cad-rendered HTML output set.
# Single parent so the prune step has one place to look. Older sibling
# dirs beyond TEMP_OUTPUT_KEEP get evicted to keep this from growing
# without bound between system temp sweeps.
TEMP_OUTPUT_PARENT = "cad"
TEMP_OUTPUT_KEEP = 20


def _temp_output_dir(stem):
    """Return a per-session temp output dir under a single shared parent
    (``$TMPDIR/cad/``). Old sibling dirs beyond
    :data:`TEMP_OUTPUT_KEEP` are pruned so the user's tmp folder doesn't
    accumulate transcripts indefinitely between system-level temp cleanups.
    """
    parent = Path(tempfile.gettempdir()) / TEMP_OUTPUT_PARENT
    parent.mkdir(parents=True, exist_ok=True)
    _prune_temp_outputs(parent, keep=TEMP_OUTPUT_KEEP)
    return parent / stem


def _prune_temp_outputs(parent, keep):
    """Delete all but the ``keep`` most-recently-modified subdirectories of
    ``parent``. Best-effort: tolerates missing parents and unreadable
    entries so a cleanup hiccup never blocks the user's render.
    """
    try:
        children = [c for c in parent.iterdir() if c.is_dir()]
    except (OSError, FileNotFoundError):
        return
    if len(children) <= keep:
        return
    children.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in children[keep:]:
        try:
            shutil.rmtree(old)
        except OSError:
            pass


def _atomic_write_json(path, data):
    """Write ``data`` to ``path`` as JSON without risking a half-written
    file. Steps:

    1. Copy the existing file (if any) to ``<path>.bak`` — one-step undo
       if the user later realises they made a mistake.
    2. Write to ``<path>.tmp`` next to the target.
    3. ``os.replace`` the temp file onto the target — POSIX atomic; the
       file is either fully old or fully new, never partial.

    If a crash happens between (1) and (3), the user still has a valid
    ``<path>`` (the previous version) plus a ``.bak`` of the same.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            # Best effort — don't block a save because backup failed.
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _loading_message(message):
    """Print ``message`` before a slow operation and erase the line on
    exit. Without this, ``Loading projects...`` lingers in scrollback
    above the picker (and above the post-exit shell prompt) — visual
    clutter that telegraphs nothing useful once the picker is up.

    Uses raw ANSI: ``\\r`` (return to col 0) then ``\\x1b[K`` (erase to
    end of line). Same sequence every modern terminal honours, no
    dependency on prompt_toolkit.
    """
    click.echo(message, nl=False)
    sys.stdout.flush()
    try:
        yield
    finally:
        click.echo("\r\x1b[K", nl=False)
        sys.stdout.flush()
