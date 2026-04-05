"""Optional JSONL logging of chat LLM prompts for cost / token analysis.

Enable with TRADINGAGENTS_PROMPT_AUDIT=1 (see .env.example).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

_TRUE = frozenset({"1", "true", "yes", "on"})


def prompt_audit_enabled() -> bool:
    v = (os.environ.get("TRADINGAGENTS_PROMPT_AUDIT") or "").strip().lower()
    return v in _TRUE


def default_audit_file_path(config: Dict[str, Any]) -> str:
    custom = (os.environ.get("TRADINGAGENTS_PROMPT_AUDIT_FILE") or "").strip()
    if custom:
        return str(Path(custom).expanduser().resolve())
    rd = config.get("results_dir") or "./results"
    base = Path(rd).expanduser().resolve()
    return str(base / "prompt_audit.jsonl")


def _max_content_chars() -> Optional[int]:
    raw = (os.environ.get("TRADINGAGENTS_PROMPT_AUDIT_MAX_CHARS") or "").strip()
    if not raw:
        return None
    try:
        n = int(raw, 10)
    except ValueError:
        return None
    return n if n > 0 else None


def _message_snapshot(msg: Any) -> Dict[str, Any]:
    role = getattr(msg, "type", None)
    if not isinstance(role, str):
        role = type(msg).__name__
    content = getattr(msg, "content", None)
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    else:
        text = json.dumps(content, default=str)
    return {"role": str(role), "content": text, "chars": len(text)}


def _model_name(serialized: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
    inv = kwargs.get("invocation_params") or {}
    m = inv.get("model") or inv.get("model_name")
    if m:
        return str(m)
    sk = (serialized or {}).get("kwargs") or {}
    return str(sk.get("model") or sk.get("model_name") or "")


class PromptAuditCallback(BaseCallbackHandler):
    """Append one JSON object per LLM call to a JSONL file (thread-safe)."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path
        self._max_chars = _max_content_chars()
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def _truncate(self, text: str) -> tuple[str, bool]:
        if self._max_chars is None or len(text) <= self._max_chars:
            return text, False
        return text[: self._max_chars] + "\n...[truncated]", True

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[Any]],
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        flat: List[Dict[str, Any]] = []
        total_chars = 0
        for batch in messages or []:
            for msg in batch or []:
                snap = _message_snapshot(msg)
                content, truncated = self._truncate(snap["content"])
                total_chars += snap["chars"]
                flat.append(
                    {
                        "role": snap["role"],
                        "chars": snap["chars"],
                        "content_truncated": truncated,
                        "content": content,
                    }
                )

        record = {
            "event": "chat_model_start",
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": str(run_id),
            "model": _model_name(serialized, kwargs),
            "total_chars": total_chars,
            "approx_input_tokens": max(1, total_chars // 4),
            "messages": flat,
        }
        self._append(record)

    def on_llm_end(self, response: LLMResult, *, run_id: Any, **kwargs: Any) -> None:
        usage: Dict[str, Any] = {}
        try:
            generation = response.generations[0][0]
            message = getattr(generation, "message", None)
            if message is not None:
                meta = getattr(message, "usage_metadata", None)
                if isinstance(meta, dict):
                    usage = {
                        "input_tokens": meta.get("input_tokens"),
                        "output_tokens": meta.get("output_tokens"),
                        "total_tokens": meta.get("total_tokens"),
                    }
        except (IndexError, TypeError):
            pass

        self._append(
            {
                "event": "llm_end",
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": str(run_id),
                "usage": usage,
            }
        )

    def _append(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
