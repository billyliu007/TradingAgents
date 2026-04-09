"""Validate merged LLM config before starting analysis (ephemeral BYOK)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def _nonempty(config: dict[str, Any], key: str) -> bool:
    v = config.get(key)
    return bool(v and str(v).strip())


def _provider_key_field(provider: str) -> str | None:
    p = (provider or "").strip().lower()
    if p in ("openai", "ollama"):
        return "openai_api_key"
    if p == "anthropic":
        return "anthropic_api_key"
    if p == "google":
        return "google_api_key"
    if p == "xai":
        return "xai_api_key"
    if p == "openrouter":
        return "openrouter_api_key"
    if p in ("kimi", "kimi_cn"):
        return "moonshot_api_key"
    return None


def assert_ephemeral_llm_keys(config: dict[str, Any]) -> None:
    """Raise HTTP 400 if ephemeral deploy is missing required API keys for chosen providers."""
    lp = (config.get("llm_provider") or "").strip().lower()
    qp = (config.get("quick_llm_provider") or lp or "").strip().lower()
    dp = (config.get("deep_llm_provider") or lp or "").strip().lower()
    need: set[str] = set()
    for p in (qp, dp):
        field = _provider_key_field(p)
        if field is None:
            continue
        if p == "ollama":
            # Local Ollama: optional key in config; no cloud key required.
            continue
        need.add(field)
    missing = [k for k in sorted(need) if not _nonempty(config, k)]
    if missing:
        labels = ", ".join(missing)
        raise HTTPException(
            status_code=400,
            detail=(
                "Ephemeral mode requires API keys in the request (Settings). "
                f"Missing or empty: {labels}. Open Settings, add keys for your selected "
                "LLM providers, then try again."
            ),
        )
