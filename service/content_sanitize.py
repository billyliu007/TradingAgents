"""Strip LLM-hallucinated tool markup from user-visible chat and report text."""

from __future__ import annotations

import re

# Fake tool / search stubs models sometimes print instead of using real bindings.
_TOOL_BLOCK = re.compile(r"<tool\b[^>]*>.*?</tool>", re.DOTALL | re.IGNORECASE)
_QUERY_BLOCK = re.compile(r"<query\b[^>]*>.*?</query>", re.DOTALL | re.IGNORECASE)
# Single-line rows that are only a fake tool/query tag (after block removal).
_ORPHAN_TAG_LINE = re.compile(
    r"^\s*<(?:tool|query)\b[^>]*>.*</(?:tool|query)>\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# Collapse excessive blank lines left after removals
_BLANK_RUN = re.compile(r"\n{3,}")


def strip_llm_fake_tool_artifacts(text: str) -> str:
    """Remove XML-like fake tool blocks and tidy whitespace. Safe for empty input."""
    if not text or not text.strip():
        return text
    s = _TOOL_BLOCK.sub("", text)
    s = _QUERY_BLOCK.sub("", s)
    s = _ORPHAN_TAG_LINE.sub("", s)
    s = _BLANK_RUN.sub("\n\n", s)
    return s.strip()


# WebSocket / job log event types that carry analyst or debate prose.
EVENT_TYPES_WITH_CONTENT = frozenset({"analyst_complete", "debate_message", "risk_message"})


def sanitize_event_payload(event_type: str, data: dict) -> dict:
    """Return a shallow copy of ``data`` with ``content`` sanitized when applicable."""
    if event_type not in EVENT_TYPES_WITH_CONTENT:
        return data
    content = data.get("content")
    if not isinstance(content, str):
        return data
    out = dict(data)
    out["content"] = strip_llm_fake_tool_artifacts(content)
    return out


def sanitize_log_event(event: dict) -> dict:
    """Sanitize a full event dict as stored/replayed from the job log (includes ``type``)."""
    t = event.get("type")
    if not isinstance(t, str) or t not in EVENT_TYPES_WITH_CONTENT:
        return event
    content = event.get("content")
    if not isinstance(content, str):
        return event
    return {**event, "content": strip_llm_fake_tool_artifacts(content)}
