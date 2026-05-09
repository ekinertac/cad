"""One-off cleanup for accidentally-looped Claude Code sessions in the
two folders that merge into "Global Sessions" (the home dir and ~/Code).

Detects clusters of sessions where:
  - 5+ files share the same first-30-chars of summary, AND
  - they all fall within a 1-hour window.
For each cluster it keeps the oldest session (the user-initiated prompt
that started the loop) and offers to delete the rest after explicit y/N.

This script is intended to be run once and removed. It exists in
``scripts/`` rather than as a CLI subcommand because the heuristic is
tuned to a specific recovery, not a permanent feature.

Run from the project root:
    uv run python scripts/dedup_global_sessions.py
"""

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from claude_code_transcripts import (
    get_session_summary,
    _global_session_folder_names,
)

PREFIX_LEN = 30
MIN_CLUSTER_SIZE = 5
TIME_WINDOW_SECONDS = 3600  # 1 hour


def find_clusters(folder):
    """Group sessions in ``folder`` into time-windowed clusters that share a
    summary prefix. Returns ``[(prefix, [(path, mtime, size, summary), ...])]``.
    """
    by_prefix = defaultdict(list)
    for f in folder.glob("*.jsonl"):
        if f.name.startswith("agent-"):
            continue
        summary = get_session_summary(f)
        # Skip degenerate summaries — clustering on these would create
        # false positives across unrelated sessions.
        if summary == "(no summary)" or summary.lower() == "warmup":
            continue
        st = f.stat()
        prefix = summary[:PREFIX_LEN]
        by_prefix[prefix].append((f, st.st_mtime, st.st_size, summary))

    clusters = []
    for prefix, sessions in by_prefix.items():
        if len(sessions) < MIN_CLUSTER_SIZE:
            continue
        sessions.sort(key=lambda s: s[1])
        # Walk in order, breaking when the gap from the cluster's first
        # element exceeds the window.
        current = [sessions[0]]
        for s in sessions[1:]:
            if s[1] - current[0][1] <= TIME_WINDOW_SECONDS:
                current.append(s)
            else:
                if len(current) >= MIN_CLUSTER_SIZE:
                    clusters.append((prefix, current))
                current = [s]
        if len(current) >= MIN_CLUSTER_SIZE:
            clusters.append((prefix, current))
    return clusters


def main():
    projects = Path.home() / ".claude" / "projects"
    folders = [projects / name for name in _global_session_folder_names()]
    folders = [f for f in folders if f.exists()]

    all_clusters = []
    for folder in folders:
        for prefix, sessions in find_clusters(folder):
            all_clusters.append((folder, prefix, sessions))

    if not all_clusters:
        print("No loop clusters detected.")
        return

    total_to_delete = sum(len(s) - 1 for _, _, s in all_clusters)
    total_keep = len(all_clusters)

    print(f"Found {len(all_clusters)} loop cluster(s):\n")
    for folder, prefix, sessions in all_clusters:
        first_t = datetime.fromtimestamp(sessions[0][1])
        last_t = datetime.fromtimestamp(sessions[-1][1])
        total_size_kb = sum(s[2] for s in sessions) / 1024
        print(f"  Folder: {folder.name}")
        print(f"  Prefix: {prefix!r}")
        print(f"  Count:  {len(sessions)}  ({first_t} -> {last_t})")
        print(f"  Total:  {total_size_kb:.0f} KB")
        print(f"  Keep:   {sessions[0][0].name}  (oldest)")
        print(f"  Delete: {len(sessions) - 1} session(s)")
        print()

    print(f"Total to delete: {total_to_delete} session(s)")
    print(f"Total to keep:   {total_keep} session(s) (oldest per cluster)")
    print()

    ans = input("Proceed with deletion? [y/N] ").strip().lower()
    if ans != "y":
        print("Aborted.")
        return

    deleted = 0
    for _, _, sessions in all_clusters:
        for sess in sessions[1:]:  # skip oldest
            sess[0].unlink()
            deleted += 1
    print(f"Deleted {deleted} session file(s).")


if __name__ == "__main__":
    main()
