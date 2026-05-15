"""
features/search/find.py — the text-search engine.

:func:`search_sessions` walks every session cad knows about, scans
its text content (user prompts + assistant replies, via the
provider-aware extractors in :mod:`core.session_model`), and returns
hit dicts with a snippet around the first match and the total match
count.

Linear scan is fine for now — a couple-hundred JSONLs in the tens of
MB completes in well under a second. If perf becomes an issue, the
swap-in path is a SQLite FTS5 index at ``~/.cad/search.db`` that
builds lazily and updates incrementally by mtime. Not building it
until we feel the lag.

May import from: ``core/``. May NOT import from sibling features.
"""

import json
from pathlib import Path

from ...core.discovery import (
    find_claude_sessions,
    find_codex_sessions,
    find_forge_sessions,
    find_opencode_sessions,
    find_pi_sessions,
)
from ...core.session_model import _extract_role_text


# How many characters of context to keep around the first match in
# the snippet. The picker row has limited width so a smaller window
# is more readable than the whole matching line.
_SNIPPET_RADIUS = 60


def _snippet_around(text, match_lower, query_lower):
    """Return a short string centred on the first occurrence of
    ``query_lower`` in ``match_lower``. ``text`` and ``match_lower``
    must be the same length (lowered text mirroring the original)."""
    idx = match_lower.find(query_lower)
    if idx < 0:
        # Defensive — caller should only invoke us on a known hit.
        return text[: _SNIPPET_RADIUS * 2].strip()
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(text), idx + len(query_lower) + _SNIPPET_RADIUS)
    snippet = text[start:end].strip()
    # Mark the truncation so the user can see this is a window, not
    # the full message.
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    # Collapse whitespace runs (newlines especially) so the snippet
    # fits a single picker row.
    return " ".join(snippet.split())


def _scan_one_session(session, query_lower):
    """Walk one session's JSONL/SQLite content and look for the
    query. Returns ``(match_count, snippet)`` — (0, None) if no
    match. The first hit's surrounding context becomes the snippet;
    subsequent hits only contribute to the count.

    SQLite-backed providers (opencode / forge) aren't yet wired —
    their text lives in a DB row, not a file. Add a branch here when
    needed; for now those providers report no hits."""
    provider = session["provider"]
    if provider not in ("claude", "codex", "pi"):
        return 0, None
    filepath = session["filepath"]
    match_count = 0
    snippet = None
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _, text = _extract_role_text(event, provider)
                if not text:
                    continue
                text_lower = text.lower()
                if query_lower not in text_lower:
                    continue
                # Count one per matching message — multiple
                # occurrences inside one message still count as one
                # hit so users aren't surprised by inflated numbers
                # on chatty assistant replies.
                match_count += 1
                if snippet is None:
                    snippet = _snippet_around(text, text_lower, query_lower)
    except OSError:
        return 0, None
    return match_count, snippet


def search_sessions(query, *, cwd=None, provider=None, limit=None):
    """Find every local session whose text content contains ``query``.

    - ``query``: case-insensitive substring. Empty string returns no
      hits.
    - ``cwd`` (optional): only search sessions in this project (exact
      cwd match).
    - ``provider`` (optional): restrict to one provider (``claude`` /
      ``codex`` / ``pi`` / ``opencode`` / ``forge``). The latter two
      always return empty until DB scanning lands.
    - ``limit`` (optional): cap the number of hits returned, ordered
      by mtime descending (newest first).

    Hit dict shape::

        {
            "provider":   "claude",
            "session_id": "...",
            "cwd":        "...",
            "filepath":   Path(...),
            "mtime":      1700000000.0,
            "size":       <bytes>,
            "summary":    None,           # lazy — caller hydrates
            "display":    None,           # lazy — caller hydrates
            "snippet":    "...viewport culling...",
            "match_count": 3,
        }
    """
    if not query:
        return []
    query_lower = query.lower()

    # Build the candidate set by walking each provider's discovery.
    # Same callable cad uses to populate the regular pickers, so we
    # search exactly the rows the user would otherwise see.
    sessions = []
    if provider in (None, "claude"):
        sessions.extend(find_claude_sessions(Path.home() / ".claude" / "projects"))
    if provider in (None, "codex"):
        sessions.extend(find_codex_sessions())
    if provider in (None, "pi"):
        sessions.extend(find_pi_sessions())
    if provider in (None, "opencode"):
        sessions.extend(find_opencode_sessions())
    if provider in (None, "forge"):
        sessions.extend(find_forge_sessions())

    if cwd is not None:
        sessions = [s for s in sessions if s.get("cwd") == cwd]

    hits = []
    for s in sessions:
        count, snippet = _scan_one_session(s, query_lower)
        if not count:
            continue
        hit = dict(s)
        hit["snippet"] = snippet
        hit["match_count"] = count
        hits.append(hit)

    # Newest first so `cad search` lands on the most recent context
    # by default.
    hits.sort(key=lambda h: h.get("mtime", 0), reverse=True)
    if limit is not None:
        hits = hits[:limit]
    return hits
