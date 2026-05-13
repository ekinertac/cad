"""
core/session_model.py — read-only session parsers and extractors.

This module knows how to pull text and metadata out of an agent
session file regardless of which provider wrote it. Three groups of
helpers live here:

- Summary extractors: ``get_session_summary``,
  ``get_claude_session_metadata``, ``get_session_cwd``. These read a
  single JSONL/JSON file and return the user-visible bits (title /
  summary / cwd) used by the project & session pickers.
- Transcript extractors: ``get_session_transcript`` plus the
  ``_extract_role_text`` / ``_flatten_content_blocks`` helpers it
  uses. Per-provider knowledge of where the user/assistant text lives
  inside each JSONL event. Consumed by peek mode.
- File-level parsers: ``parse_session_file`` / ``_parse_jsonl_file``,
  used by the HTML renderer to turn a session into a flat
  ``loglines`` list.

Plus :func:`extract_text_from_content`, the historical "pull plain
text out of a content array" helper used by several callers.

May import from: ``core.util``, plus the Python stdlib. May NOT
import from: ``features/``, the picker, or anything in cad's
command/Click layer.
"""

import json
from pathlib import Path


def extract_text_from_content(content):
    """Extract plain text from message content.

    Handles both string content (older format) and array content (newer format).

    Args:
        content: Either a string or a list of content blocks like
                 [{"type": "text", "text": "..."}, {"type": "image", ...}]

    Returns:
        The extracted text as a string, or empty string if no text found.
    """
    if isinstance(content, str):
        return content.strip()
    elif isinstance(content, list):
        # Extract text from content blocks of type "text"
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    texts.append(text)
        return " ".join(texts).strip()
    return ""


def get_session_summary(filepath, max_length=200):
    """Extract a human-readable summary from a session file.

    Supports both JSON and JSONL formats.
    Returns a summary string or "(no summary)" if none found.
    """
    filepath = Path(filepath)
    try:
        if filepath.suffix == ".jsonl":
            return _get_jsonl_summary(filepath, max_length)
        else:
            # For JSON files, try to get first user message
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            loglines = data.get("loglines", [])
            for entry in loglines:
                if entry.get("type") == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    text = extract_text_from_content(content)
                    if text:
                        if len(text) > max_length:
                            return text[: max_length - 3] + "..."
                        return text
            return "(no summary)"
    except Exception:
        return "(no summary)"


def _get_jsonl_summary(filepath, max_length=200):
    """Extract summary from JSONL file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    # First priority: summary type entries
                    if obj.get("type") == "summary" and obj.get("summary"):
                        summary = obj["summary"]
                        if len(summary) > max_length:
                            return summary[: max_length - 3] + "..."
                        return summary
                except json.JSONDecodeError:
                    continue

        # Second pass: find first non-meta user message
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if (
                        obj.get("type") == "user"
                        and not obj.get("isMeta")
                        and obj.get("message", {}).get("content")
                    ):
                        content = obj["message"]["content"]
                        text = extract_text_from_content(content)
                        if text and not text.startswith("<"):
                            if len(text) > max_length:
                                return text[: max_length - 3] + "..."
                            return text
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass

    return "(no summary)"


def get_session_cwd(jsonl_path):
    """Return the working directory the session was started in, by scanning
    the JSONL for the first event line that carries a ``cwd`` field. Used
    by the resume action so we ``chdir`` to the right project before exec'ing
    claude — far more reliable than decoding the lossy folder-name encoding
    (where a folder like ``-Users-x-Code-cad`` could
    decode to several different real paths).

    Returns ``None`` if no event in the file has a ``cwd``. Malformed JSON
    lines are skipped so a partially-corrupted session still yields a path.
    """
    try:
        with open(jsonl_path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and d.get("cwd"):
                    return d["cwd"]
    except OSError:
        return None
    return None


def get_claude_session_metadata(filepath, max_summary_length=200):
    """Single-pass scan of a claude JSONL that captures both the first
    user prompt (the implicit title) and the last user-assigned name from
    ``/rename`` (a ``{"type":"custom-title","customTitle":...}`` event).

    Returns ``{"summary": str, "name": str|None}``. The summary follows
    the same rules as :func:`_get_jsonl_summary` — ``type:summary`` events
    win, otherwise the first non-meta user message. The name is whichever
    custom-title event came last in the file: rename can happen multiple
    times in one session and ``claude --resume <name>`` resolves to the
    current value.

    A single pass is cheaper than running two scans, but only invoked
    after the user picks a project — the project-picker layer doesn't
    need names or summaries.
    """
    summary = None
    name = None
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                if t == "custom-title":
                    new_name = d.get("customTitle")
                    if new_name:
                        name = new_name
                    continue
                if summary is not None:
                    # Once we have a summary we don't need to inspect more
                    # for that purpose, but keep walking in case a later
                    # custom-title event updates the name.
                    continue
                if t == "summary" and d.get("summary"):
                    summary = d["summary"]
                elif (
                    t == "user"
                    and not d.get("isMeta")
                    and d.get("message", {}).get("content")
                ):
                    text = extract_text_from_content(d["message"]["content"])
                    if text and not text.startswith("<"):
                        summary = text
    except OSError:
        pass

    if summary is None:
        summary = "(no summary)"
    elif len(summary) > max_summary_length:
        summary = summary[: max_summary_length - 3] + "..."

    return {"summary": summary, "name": name}


def _flatten_content_blocks(content):
    """Normalise an agent message's `content` field to a single text string.
    Handles flat-string content, content-block-array content (claude/pi),
    and anything else by returning empty.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return " ".join(parts).strip()
    return ""


def _extract_role_text(event, provider):
    """For one JSONL event, return ``(role, text)`` if it's a user or
    assistant message and the text is non-empty; otherwise ``("", "")``.
    Tool calls / tool results / system meta are intentionally skipped —
    peek mode is "what did the human and the agent say to each other?"
    """
    if not isinstance(event, dict):
        return "", ""
    if provider == "claude":
        msg = event.get("message")
        if not isinstance(msg, dict):
            return "", ""
        role = msg.get("role") or event.get("type")
        if role not in ("user", "assistant"):
            return "", ""
        if event.get("isMeta"):  # claude marks injected system notes
            return "", ""
        return role, _flatten_content_blocks(msg.get("content"))
    if provider == "codex":
        if event.get("type") != "event_msg":
            return "", ""
        p = event.get("payload") or {}
        ptype = p.get("type")
        if ptype == "user_message":
            return "user", (p.get("message") or "").strip()
        if ptype == "agent_message":
            return "assistant", (p.get("message") or "").strip()
        return "", ""
    if provider == "pi":
        if event.get("type") != "message":
            return "", ""
        msg = event.get("message") or {}
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return "", ""
        return role, _flatten_content_blocks(msg.get("content"))
    return "", ""


def get_session_transcript(session, max_chars=200_000):
    """Return a list of ``(role, text)`` tuples for peek mode — user
    prompts and assistant text replies only, no tool calls.

    Caps total characters so an enormous session doesn't make the pager
    take ages to load. SQLite-backed providers (opencode, forge) aren't
    yet supported and return an empty list — peek prints a friendly
    message in that case.
    """
    provider = session["provider"]
    if provider not in ("claude", "codex", "pi"):
        return []
    out = []
    total = 0
    try:
        with open(session["filepath"], "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role, text = _extract_role_text(d, provider)
                if not text:
                    continue
                out.append((role, text))
                total += len(text)
                if total >= max_chars:
                    out.append(("system", "[... truncated by cct peek]"))
                    break
    except OSError:
        pass
    return out


def _read_session_excerpt_for_summary(session, max_chars=2000):
    """Pull a reasonable excerpt from a session for the summarize prompt.

    For JSONL providers, walk the first ~max_chars of textual content
    (user prompts + assistant replies). For SQLite providers (opencode,
    forge), we already store a title at discovery time so summarize is
    mostly redundant — but we still fall back to whatever summary we have
    so the LLM has something to reword. Returns a single string.
    """
    provider = session["provider"]
    parts = []
    if provider in ("claude", "codex", "pi"):
        try:
            with open(session["filepath"], "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = _extract_summarizable_text(d)
                    if text:
                        parts.append(text)
                        if sum(len(p) for p in parts) >= max_chars:
                            break
        except OSError:
            pass
    if not parts and session.get("summary"):
        parts.append(session["summary"])
    excerpt = "\n\n".join(parts)
    return excerpt[:max_chars]


def _extract_summarizable_text(event):
    """Best-effort text extractor for one JSONL event across providers.
    Returns a string or '' if nothing useful."""
    if not isinstance(event, dict):
        return ""
    # Claude shape: {"type":"user|assistant", "message":{"content": ...}}
    msg = event.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    chunks.append(b.get("text", ""))
                elif isinstance(b, str):
                    chunks.append(b)
            return " ".join(chunks).strip()
    # Codex shape: event_msg with payload.message
    payload = event.get("payload")
    if isinstance(payload, dict):
        if payload.get("type") in ("user_message", "agent_message"):
            return (payload.get("message") or "").strip()
    return ""


def parse_session_file(filepath):
    """Parse a session file and return normalized data.

    Supports both JSON and JSONL formats.
    Returns a dict with 'loglines' key containing the normalized entries.
    """
    filepath = Path(filepath)

    if filepath.suffix == ".jsonl":
        return _parse_jsonl_file(filepath)
    else:
        # Standard JSON format
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)


def _parse_jsonl_file(filepath):
    """Parse JSONL file and convert to standard format."""
    loglines = []

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                entry_type = obj.get("type")

                # Skip non-message entries
                if entry_type not in ("user", "assistant"):
                    continue

                # Convert to standard format
                entry = {
                    "type": entry_type,
                    "timestamp": obj.get("timestamp", ""),
                    "message": obj.get("message", {}),
                }

                # Preserve isCompactSummary if present
                if obj.get("isCompactSummary"):
                    entry["isCompactSummary"] = True

                loglines.append(entry)
            except json.JSONDecodeError:
                continue

    return {"loglines": loglines}
