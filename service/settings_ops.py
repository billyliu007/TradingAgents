from __future__ import annotations

import json
from typing import Any, get_args

from fastapi import HTTPException

from tradingagents.default_config import DEFAULT_CONFIG

from service import db
from service.admin_auth import admin_password_configured
from service.app_config import get_deploy_mode, is_ephemeral_deploy
from service.constants import ANALYST_OPTIONS
from service.schemas import AnalyzeRequest, LlmProvider

# Keys allowed in app_settings JSONB (admin UI + global config merge)
_APP_SETTINGS_KEYS: frozenset[str] = frozenset({
    "llm_provider",
    "quick_llm_provider",
    "deep_llm_provider",
    "quick_think_llm",
    "deep_think_llm",
    "max_debate_rounds",
    "max_risk_discuss_rounds",
    "backend_url",
    "quick_backend_url",
    "deep_backend_url",
    "google_thinking_level",
    "openai_reasoning_effort",
    "anthropic_effort",
    "openai_api_key",
    "anthropic_api_key",
    "google_api_key",
    "xai_api_key",
    "openrouter_api_key",
    "moonshot_api_key",
    "kimi_quick_model_custom",
    "kimi_deep_model_custom",
})

_API_KEY_SETTINGS_KEYS: frozenset[str] = frozenset({
    "openai_api_key",
    "anthropic_api_key",
    "google_api_key",
    "xai_api_key",
    "openrouter_api_key",
    "moonshot_api_key",
})


def coerce_round_int(val: Any, default: int, lo: int = 1, hi: int = 5) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def apply_map_from_stored(config: dict[str, Any], stored: dict[str, Any]) -> None:
    for k in _APP_SETTINGS_KEYS:
        if k not in stored:
            continue
        v = stored[k]
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        config[k] = v.strip() if isinstance(v, str) else v


def apply_map_from_payload(config: dict[str, Any], payload: AnalyzeRequest) -> None:
    if payload.llm_provider is not None:
        config["llm_provider"] = payload.llm_provider
    if payload.quick_llm_provider is not None:
        config["quick_llm_provider"] = payload.quick_llm_provider
    if payload.deep_llm_provider is not None:
        config["deep_llm_provider"] = payload.deep_llm_provider
    if payload.quick_think_llm is not None:
        config["quick_think_llm"] = payload.quick_think_llm.strip()
    if payload.deep_think_llm is not None:
        config["deep_think_llm"] = payload.deep_think_llm.strip()
    if payload.max_debate_rounds is not None:
        config["max_debate_rounds"] = payload.max_debate_rounds
    if payload.max_risk_discuss_rounds is not None:
        config["max_risk_discuss_rounds"] = payload.max_risk_discuss_rounds
    if payload.google_thinking_level is not None:
        config["google_thinking_level"] = payload.google_thinking_level
    if payload.openai_reasoning_effort is not None:
        config["openai_reasoning_effort"] = payload.openai_reasoning_effort
    if payload.anthropic_effort is not None:
        config["anthropic_effort"] = payload.anthropic_effort
    if payload.backend_url is not None and str(payload.backend_url).strip():
        config["backend_url"] = str(payload.backend_url).strip()
    if payload.quick_backend_url is not None:
        s = str(payload.quick_backend_url).strip()
        config["quick_backend_url"] = s if s else None
    if payload.deep_backend_url is not None:
        s = str(payload.deep_backend_url).strip()
        config["deep_backend_url"] = s if s else None
    for key in (
        "openai_api_key",
        "anthropic_api_key",
        "google_api_key",
        "xai_api_key",
        "openrouter_api_key",
        "moonshot_api_key",
    ):
        v = getattr(payload, key)
        if v:
            config[key] = v
    if payload.kimi_quick_model_custom is not None:
        s = payload.kimi_quick_model_custom.strip()
        if s:
            config["kimi_quick_model_custom"] = s
    if payload.kimi_deep_model_custom is not None:
        s = payload.kimi_deep_model_custom.strip()
        if s:
            config["kimi_deep_model_custom"] = s


def apply_kimi_custom_models(config: dict[str, Any]) -> None:
    qp = str(config.get("quick_llm_provider") or config.get("llm_provider") or "").lower()
    dp = str(config.get("deep_llm_provider") or config.get("llm_provider") or "").lower()
    kq = str(config.get("kimi_quick_model_custom") or "").strip()
    kd = str(config.get("kimi_deep_model_custom") or "").strip()
    if qp in ("kimi", "kimi_cn") and kq:
        config["quick_think_llm"] = kq
    if dp in ("kimi", "kimi_cn") and kd:
        config["deep_think_llm"] = kd


def build_graph_config(payload: AnalyzeRequest) -> dict[str, Any]:
    """Merge DEFAULT_CONFIG with Postgres app_settings (if any), then request payload."""
    config = DEFAULT_CONFIG.copy()
    stored = db.get_app_settings()
    if stored:
        apply_map_from_stored(config, stored)
    apply_map_from_payload(config, payload)

    apply_kimi_custom_models(config)

    config["max_debate_rounds"] = coerce_round_int(
        config.get("max_debate_rounds"), int(DEFAULT_CONFIG["max_debate_rounds"])
    )
    config["max_risk_discuss_rounds"] = coerce_round_int(
        config.get("max_risk_discuss_rounds"),
        int(DEFAULT_CONFIG["max_risk_discuss_rounds"]),
    )

    qb = config.get("quick_backend_url")
    config["quick_backend_url"] = str(qb).strip() if qb else None
    dbu = config.get("deep_backend_url")
    config["deep_backend_url"] = str(dbu).strip() if dbu else None

    if is_ephemeral_deploy():
        config["forbid_llm_env_keys"] = True

    return config


def llm_cache_profile_from_config(config: dict[str, Any]) -> str:
    """Stable string so analysis_cache keys differ when LLM routing changes."""
    lp = config.get("llm_provider") or "kimi_cn"
    qp = config.get("quick_llm_provider") or lp
    dp = config.get("deep_llm_provider") or lp
    blob = {
        "qp": qp,
        "dp": dp,
        "qm": config.get("quick_think_llm"),
        "dm": config.get("deep_think_llm"),
        "bu": str(config.get("backend_url") or "").strip(),
        "qbu": str(config.get("quick_backend_url") or "").strip(),
        "dbu": str(config.get("deep_backend_url") or "").strip(),
        "g": config.get("google_thinking_level"),
        "oe": config.get("openai_reasoning_effort"),
        "ae": config.get("anthropic_effort"),
    }
    return json.dumps(blob, sort_keys=True, default=str)


def admin_settings_form_defaults() -> dict[str, Any]:
    c = DEFAULT_CONFIG
    lp = c["llm_provider"]
    qlp = c.get("quick_llm_provider") or lp
    dlp = c.get("deep_llm_provider") or lp
    return {
        "llm_provider": lp,
        "quick_llm_provider": qlp if isinstance(qlp, str) else lp,
        "deep_llm_provider": dlp if isinstance(dlp, str) else lp,
        "quick_think_llm": c["quick_think_llm"],
        "deep_think_llm": c["deep_think_llm"],
        "max_debate_rounds": c["max_debate_rounds"],
        "max_risk_discuss_rounds": c["max_risk_discuss_rounds"],
        "backend_url": c.get("backend_url") or "",
        "quick_backend_url": "",
        "deep_backend_url": "",
        "google_thinking_level": c.get("google_thinking_level") or "",
        "openai_reasoning_effort": c.get("openai_reasoning_effort") or "",
        "anthropic_effort": c.get("anthropic_effort") or "",
        "openai_api_key": "",
        "anthropic_api_key": "",
        "google_api_key": "",
        "xai_api_key": "",
        "openrouter_api_key": "",
        "moonshot_api_key": "",
        "kimi_quick_model_custom": "",
        "kimi_deep_model_custom": "",
    }


def allowed_llm_providers() -> frozenset[str]:
    return frozenset(get_args(LlmProvider))


def admin_settings_get_payload() -> dict[str, Any]:
    base = admin_settings_form_defaults()
    stored = db.get_app_settings()
    api_keys_set = {
        k: bool(str(stored.get(k) or "").strip()) for k in _API_KEY_SETTINGS_KEYS
    }
    for k in _APP_SETTINGS_KEYS:
        if k in _API_KEY_SETTINGS_KEYS:
            continue
        if k not in stored:
            continue
        v = stored[k]
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        base[k] = v.strip() if isinstance(v, str) else v
    base["max_debate_rounds"] = coerce_round_int(
        base.get("max_debate_rounds"), int(DEFAULT_CONFIG["max_debate_rounds"])
    )
    base["max_risk_discuss_rounds"] = coerce_round_int(
        base.get("max_risk_discuss_rounds"),
        int(DEFAULT_CONFIG["max_risk_discuss_rounds"]),
    )
    return {"settings": base, "api_keys_set": api_keys_set}


def admin_sanitize_put_body(body: dict[str, Any]) -> dict[str, Any]:
    """Merge admin form JSON into stored app_settings (keys omitted in body are kept)."""
    allowed_p = allowed_llm_providers()
    existing = db.get_app_settings()
    out: dict[str, Any] = {
        k: v for k, v in existing.items() if k in _APP_SETTINGS_KEYS
    }

    for k, raw in body.items():
        if k not in _APP_SETTINGS_KEYS:
            continue
        if k in _API_KEY_SETTINGS_KEYS:
            if raw is None:
                out.pop(k, None)
                continue
            s = str(raw).strip()
            if s:
                out[k] = s
            continue
        if k in ("max_debate_rounds", "max_risk_discuss_rounds"):
            out[k] = coerce_round_int(
                raw,
                int(DEFAULT_CONFIG[k]),
            )
            continue
        if k in ("quick_llm_provider", "deep_llm_provider"):
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                raise HTTPException(status_code=422, detail=f"{k} is required")
            s = str(raw).strip().lower()
            if s not in allowed_p:
                raise HTTPException(status_code=422, detail=f"Invalid {k}")
            out[k] = s
            continue
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                out.pop(k, None)
            else:
                out[k] = s
        elif raw is not None:
            out[k] = raw

    return out


def ui_options_response() -> dict[str, Any]:
    """Payload for ``GET /api/options`` (merged DB settings + defaults for the web UI)."""
    stored = db.get_app_settings()
    cfg = DEFAULT_CONFIG.copy()
    if stored:
        apply_map_from_stored(cfg, stored)
    apply_kimi_custom_models(cfg)
    qp = cfg.get("quick_llm_provider") or cfg.get("llm_provider")
    dp = cfg.get("deep_llm_provider") or cfg.get("llm_provider")
    mode = get_deploy_mode()
    return {
        "analysts": ANALYST_OPTIONS,
        "llm_provider_default": cfg.get("llm_provider"),
        "quick_llm_provider_default": qp,
        "deep_llm_provider_default": dp,
        "deep_think_default": cfg.get("deep_think_llm"),
        "quick_think_default": cfg.get("quick_think_llm"),
        "max_debate_rounds_default": coerce_round_int(
            cfg.get("max_debate_rounds"), int(DEFAULT_CONFIG["max_debate_rounds"])
        ),
        "max_risk_discuss_rounds_default": coerce_round_int(
            cfg.get("max_risk_discuss_rounds"),
            int(DEFAULT_CONFIG["max_risk_discuss_rounds"]),
        ),
        "backend_url_default": cfg.get("backend_url") or "",
        "global_settings_from_db": bool(stored),
        "deploy_mode": mode,
        "exports_enabled": mode == "hosted",
        "ephemeral_requires_session_keys": mode == "ephemeral",
        "admin_password_configured": admin_password_configured(),
    }
