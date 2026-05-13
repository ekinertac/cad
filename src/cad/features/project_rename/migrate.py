"""
features/project_rename/migrate.py — the project-state mover.

``migrate_claude_project`` is the most destructive code path cad has:
it moves four parallel state directories under ``~/.claude/`` (one
per kind of state claude persists per-project) and rewrites the
embedded ``cwd`` field in every JSONL under the new location. A
backup tree is created first so a failure or user "wait that wasn't
right" is recoverable.

Mechanism cross-checked against
https://www.vincentschmalbach.com/migrate-claude-code-sessions-to-a-new-computer/
and claude's own behaviour (claude --resume filters its picker by
the JSONL-embedded cwd, not by the encoded folder name; so the cwd
rewrite is what makes the moved project visible in claude after the
move).

This is *not* safe to run while the project's claude session is
open — the JSONL is being appended to and writes might be lost.

May import from: stdlib + click. May NOT import from sibling
features. core/ imports are fine.
"""

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import click


def _claude_encode_path(path):
    """Replicate Claude Code's directory-encoding scheme. Both ``/`` and
    ``.`` are replaced with ``-`` — e.g. ``/Users/x/Code/humbl.ai`` becomes
    ``-Users-x-Code-humbl-ai``. Verified against folders on disk."""
    return re.sub(r"[/.]", "-", str(path))


# State that claude maintains in parallel directories alongside projects/.
# Each is keyed by the same encoded path. We move all that exist for a
# given project — leaving any behind would let claude see stale references.
_CLAUDE_STATE_DIRS = ("projects", "file-history", "todos", "shell-snapshots")


def migrate_claude_project(old_cwd, new_cwd, backup_root=None, dry_run=False):
    """Move a claude project's on-disk state from one cwd to another.

    1. Backup the four ``~/.claude/<dir>/<old_enc>/`` trees (where they
       exist) into ``backup_root`` so the user has a one-command undo.
    2. Move each ``~/.claude/<dir>/<old_enc>/`` to ``<new_enc>/``. If the
       destination already exists, merge file-by-file (existing files at
       the destination win — we never overwrite).
    3. Rewrite the ``cwd`` field in every JSONL line under the new
       ``projects/<new_enc>/`` directory.

    Mechanism is from https://www.vincentschmalbach.com/migrate-claude-code-sessions-to-a-new-computer/
    cross-checked against claude's actual filter behaviour (claude --resume
    filters its picker by cwd inside the JSONL, not by folder name).

    Returns a dict::

        {
            "moved_dirs":      [(old_path, new_path), ...],
            "rewritten_files": [Path, ...],
            "backup_dir":      Path | None,
            "skipped":         ["projects exists at target", ...],
        }
    """
    old_enc = _claude_encode_path(old_cwd)
    new_enc = _claude_encode_path(new_cwd)
    if old_enc == new_enc:
        raise click.ClickException(
            "Old and new cwd encode to the same path — nothing to migrate."
        )

    claude_root = Path.home() / ".claude"
    result = {
        "moved_dirs": [],
        "rewritten_files": [],
        "backup_dir": None,
        "skipped": [],
    }

    # Phase 1 — backup. Only copy what actually exists; don't create empty
    # backup trees for state dirs that don't apply to this project.
    if backup_root and not dry_run:
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        backup_dir = backup_root / f"claude-migrate-{ts}"
        for base in _CLAUDE_STATE_DIRS:
            src = claude_root / base / old_enc
            if src.exists():
                dst = backup_dir / base / old_enc
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dst)
        result["backup_dir"] = backup_dir if backup_dir.exists() else None

    # Phase 2 — move each state dir if it exists.
    for base in _CLAUDE_STATE_DIRS:
        src = claude_root / base / old_enc
        dst = claude_root / base / new_enc
        if not src.exists():
            continue
        if dst.exists():
            # Merge: move individual entries, skipping any name collisions
            # so we never clobber an existing file in the destination.
            for entry in src.iterdir():
                target = dst / entry.name
                if target.exists():
                    result["skipped"].append(f"{base}/{new_enc}/{entry.name} exists")
                    continue
                if not dry_run:
                    shutil.move(str(entry), str(target))
            # Best-effort cleanup of the (now hopefully empty) source dir.
            try:
                if not dry_run:
                    src.rmdir()
            except OSError:
                pass
            result["moved_dirs"].append((src, dst))
        else:
            if not dry_run:
                shutil.move(str(src), str(dst))
            result["moved_dirs"].append((src, dst))

    # Phase 3 — rewrite cwd inside every JSONL under the new projects dir.
    new_project_dir = claude_root / "projects" / new_enc
    if new_project_dir.exists():
        old_cwd_str = str(old_cwd)
        new_cwd_str = str(new_cwd)
        for jsonl in new_project_dir.glob("*.jsonl"):
            if dry_run:
                result["rewritten_files"].append(jsonl)
                continue
            # Preserve mtime — the session content didn't semantically
            # change, only its location label. Lets cad's sort order
            # remain stable across a migration.
            stat = jsonl.stat()
            text = jsonl.read_text(encoding="utf-8")
            new_text = text.replace(f'"cwd":"{old_cwd_str}"', f'"cwd":"{new_cwd_str}"')
            if new_text != text:
                jsonl.write_text(new_text, encoding="utf-8")
                os.utime(jsonl, (stat.st_atime, stat.st_mtime))
                result["rewritten_files"].append(jsonl)

    return result
