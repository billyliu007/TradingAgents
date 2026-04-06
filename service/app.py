from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import re
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from threading import Lock, Event as ThreadEvent
from typing import Any, Literal, get_args

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from langchain_core.callbacks import BaseCallbackHandler
import json

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from service import tickers as _ticker_svc
from service.pdf_export import (
    export_filename,
    unique_path,
    write_analysis_pdf,
)
from service import db
from service.analysis_dates import normalize_analysis_date

load_dotenv()

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"

# ── Admin UI (separate /admin page) ───────────────────────────────────────────
_ADMIN_COOKIE = "ta_admin_session"
_ADMIN_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def _admin_username() -> str:
    return (os.getenv("TRADINGAGENTS_ADMIN_USER") or "billyliu").strip()


def _admin_password_configured() -> bool:
    return bool((os.getenv("TRADINGAGENTS_ADMIN_PASSWORD") or "").strip())


def _admin_session_secret() -> bytes:
    s = (os.getenv("TRADINGAGENTS_ADMIN_SESSION_SECRET") or "").strip()
    if s:
        return s.encode("utf-8")
    p = (os.getenv("TRADINGAGENTS_ADMIN_PASSWORD") or "").strip()
    return p.encode("utf-8") if p else b""


def _admin_verify_credentials(username: str, password: str) -> bool:
    if not _admin_password_configured():
        return False
    if username != _admin_username():
        return False
    expected = (os.getenv("TRADINGAGENTS_ADMIN_PASSWORD") or "").strip().encode("utf-8")
    return hmac.compare_digest(password.encode("utf-8"), expected)


def _admin_issue_token() -> str:
    secret = _admin_session_secret()
    if not secret:
        return ""
    user = _admin_username()
    exp = int(time.time()) + _ADMIN_COOKIE_MAX_AGE
    payload = f"{user}|{exp}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode("utf-8")).decode("ascii")


def _admin_verify_token(raw: str | None) -> bool:
    if not raw:
        return False
    secret = _admin_session_secret()
    if not secret:
        return False
    try:
        inner = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        user, exp_s, sig = inner.rsplit("|", 2)
        if user != _admin_username():
            return False
        if int(time.time()) > int(exp_s):
            return False
        payload = f"{user}|{exp_s}"
        expect = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, sig)
    except (ValueError, OSError, UnicodeDecodeError):
        return False


class AdminLoginRequest(BaseModel):
    username: str = ""
    password: str = ""


ANALYST_OPTIONS = ["market", "social", "news", "fundamentals"]
LOG_BUFFER_MAX = 1000
_log_buffer: deque[str] = deque(maxlen=LOG_BUFFER_MAX)
_log_lock = Lock()

# ── Job queue ────────────────────────────────────────────────────────────────
_executor = ThreadPoolExecutor(max_workers=3)
_jobs: dict[str, dict] = {}
_jobs_lock = Lock()
MAX_JOBS_STORE = 100
_job_websockets: dict[str, list[WebSocket]] = {}
_ws_lock = Lock()


def _log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] {message}"
    with _log_lock:
        _log_buffer.append(line)


def _get_logs(limit: int) -> list[str]:
    with _log_lock:
        if limit <= 0:
            return []
        return list(_log_buffer)[-limit:]


def _clear_logs() -> None:
    with _log_lock:
        _log_buffer.clear()


_EXPORT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.pdf$")


def _exports_dir() -> Path:
    raw = os.getenv("TRADINGAGENTS_EXPORTS_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return (APP_DIR.parent / "exports").resolve()


def _safe_export_basename(name: str) -> bool:
    if not name or len(name) > 240:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return bool(_EXPORT_NAME_RE.match(name))


def _resolve_export_file(filename: str) -> Path:
    if not _safe_export_basename(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    base = _exports_dir().resolve()
    path = (base / filename).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


def _paths_for_export_filenames(filenames: list[str]) -> list[Path]:
    """Resolve existing PDF paths under exports dir; dedupe; skip invalid or missing."""
    base = _exports_dir().resolve()
    out: list[Path] = []
    seen: set[str] = set()
    for name in filenames:
        if not _safe_export_basename(name) or name in seen:
            continue
        seen.add(name)
        path = (base / name).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            continue
        if path.is_file() and path.suffix.lower() == ".pdf":
            out.append(path)
    return out


class ExportZipRequest(BaseModel):
    filenames: list[str] = Field(default_factory=list, max_length=100)


_LlmProvider = Literal[
    "openai", "google", "anthropic", "xai", "kimi", "kimi_cn", "openrouter", "ollama"
]

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


class AnalyzeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, description="Ticker, e.g. NVDA")
    analysis_date: date = Field(
        ...,
        description=(
            "As-of calendar date. If this equals the server's UTC calendar date for 'today', "
            "it is normalized to today's date in America/New_York; otherwise it is treated as an "
            "explicit US Eastern calendar date."
        ),
    )
    selected_analysts: list[Literal["market", "social", "news", "fundamentals"]] = Field(
        default_factory=lambda: ANALYST_OPTIONS.copy()
    )
    language: Literal["en", "zh"] = "en"
    # Below: optional. When omitted, values come from DB app_settings or DEFAULT_CONFIG.
    llm_provider: _LlmProvider | None = None
    quick_llm_provider: _LlmProvider | None = None
    deep_llm_provider: _LlmProvider | None = None
    backend_url: str | None = None
    quick_backend_url: str | None = None
    deep_backend_url: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None
    max_debate_rounds: int | None = None
    max_risk_discuss_rounds: int | None = None
    google_thinking_level: str | None = None
    openai_reasoning_effort: str | None = None
    anthropic_effort: str | None = None
    debug: bool = False
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    xai_api_key: str | None = None
    openrouter_api_key: str | None = None
    moonshot_api_key: str | None = None
    kimi_quick_model_custom: str | None = None
    kimi_deep_model_custom: str | None = None


class AnalyzeResponse(BaseModel):
    decision: str
    final_trade_decision: str
    human_readable_report: str
    sections: dict[str, str]
    raw_state: dict[str, Any]
    pdf_filenames: list[str] | None = None
    pdf_download_urls: list[str] | None = None
    analysis_date: date | None = None


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    message: str
    analysis_date: date


class JobStatusResponse(BaseModel):
    job_id: str
    status: str  # pending | running | done | failed | cancelled
    ticker: str
    created_at: str
    completed_at: str | None = None
    decision: str | None = None
    pdf_filenames: list[str] | None = None
    pdf_download_urls: list[str] | None = None
    sections: dict[str, str] | None = None
    error: str | None = None


def _require_admin(request: Request) -> None:
    if not _admin_verify_token(request.cookies.get(_ADMIN_COOKIE)):
        raise HTTPException(status_code=401, detail="Not authenticated")


def _normalize_analyze_request(payload: AnalyzeRequest) -> AnalyzeRequest:
    """Normalize ``analysis_date`` (US Eastern session day) and always use all data analysts."""
    d = normalize_analysis_date(payload.analysis_date)
    analysts = list(ANALYST_OPTIONS)
    if d == payload.analysis_date and list(payload.selected_analysts) == analysts:
        return payload
    return payload.model_copy(update={"analysis_date": d, "selected_analysts": analysts})


def _coerce_round_int(val: Any, default: int, lo: int = 1, hi: int = 5) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _apply_map_from_stored(config: dict[str, Any], stored: dict[str, Any]) -> None:
    for k in _APP_SETTINGS_KEYS:
        if k not in stored:
            continue
        v = stored[k]
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        config[k] = v.strip() if isinstance(v, str) else v


def _apply_map_from_payload(config: dict[str, Any], payload: AnalyzeRequest) -> None:
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


def _apply_kimi_custom_models(config: dict[str, Any]) -> None:
    qp = str(config.get("quick_llm_provider") or config.get("llm_provider") or "").lower()
    dp = str(config.get("deep_llm_provider") or config.get("llm_provider") or "").lower()
    kq = str(config.get("kimi_quick_model_custom") or "").strip()
    kd = str(config.get("kimi_deep_model_custom") or "").strip()
    if qp in ("kimi", "kimi_cn") and kq:
        config["quick_think_llm"] = kq
    if dp in ("kimi", "kimi_cn") and kd:
        config["deep_think_llm"] = kd


def _build_graph_config(payload: AnalyzeRequest) -> dict[str, Any]:
    """Merge DEFAULT_CONFIG with Postgres app_settings (if any) or request payload."""
    config = DEFAULT_CONFIG.copy()
    stored = db.get_app_settings()
    if stored:
        _apply_map_from_stored(config, stored)
    else:
        _apply_map_from_payload(config, payload)

    _apply_kimi_custom_models(config)

    config["max_debate_rounds"] = _coerce_round_int(
        config.get("max_debate_rounds"), int(DEFAULT_CONFIG["max_debate_rounds"])
    )
    config["max_risk_discuss_rounds"] = _coerce_round_int(
        config.get("max_risk_discuss_rounds"),
        int(DEFAULT_CONFIG["max_risk_discuss_rounds"]),
    )

    qb = config.get("quick_backend_url")
    config["quick_backend_url"] = str(qb).strip() if qb else None
    dbu = config.get("deep_backend_url")
    config["deep_backend_url"] = str(dbu).strip() if dbu else None

    return config


def _llm_cache_profile_from_config(config: dict[str, Any]) -> str:
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


def _admin_settings_form_defaults() -> dict[str, Any]:
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


def _allowed_llm_providers() -> frozenset[str]:
    return frozenset(get_args(_LlmProvider))


def _admin_settings_get_payload() -> dict[str, Any]:
    base = _admin_settings_form_defaults()
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
    base["max_debate_rounds"] = _coerce_round_int(
        base.get("max_debate_rounds"), int(DEFAULT_CONFIG["max_debate_rounds"])
    )
    base["max_risk_discuss_rounds"] = _coerce_round_int(
        base.get("max_risk_discuss_rounds"),
        int(DEFAULT_CONFIG["max_risk_discuss_rounds"]),
    )
    return {"settings": base, "api_keys_set": api_keys_set}


def _admin_sanitize_put_body(body: dict[str, Any]) -> dict[str, Any]:
    """Merge admin form JSON into stored app_settings (keys omitted in body are kept)."""
    allowed_p = _allowed_llm_providers()
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
            out[k] = _coerce_round_int(
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


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _format_tool_output_for_feed(output: Any, max_len: int = 4500) -> str:
    """Normalize tool return values for the UI data feed (pretty JSON, sane truncation)."""
    if output is None:
        return ""
    s: str
    if isinstance(output, str):
        s = output.strip()
        if len(s) >= 2 and s[0] in "{[" and s[-1] in "}]":
            try:
                s = json.dumps(json.loads(s), indent=2, ensure_ascii=False)
            except (json.JSONDecodeError, ValueError):
                pass
    else:
        try:
            if isinstance(output, (dict, list)):
                s = json.dumps(output, indent=2, default=str, ensure_ascii=False)
            else:
                s = str(output).strip()
        except Exception:
            s = str(output).strip()
    if len(s) > max_len:
        s = s[: max_len - 40].rstrip() + "\n\n… (truncated)"
    return s


_TOOL_LABELS: dict[str, tuple[str, str]] = {
    "get_stock_data":           ("market",       "📈 Stock Price & Volume"),
    "get_indicators":           ("market",       "📊 Technical Indicators"),
    "get_fundamentals":         ("fundamentals", "🏦 Company Fundamentals"),
    "get_balance_sheet":        ("fundamentals", "📋 Balance Sheet"),
    "get_cashflow":             ("fundamentals", "💵 Cash Flow Statement"),
    "get_income_statement":     ("fundamentals", "📄 Income Statement"),
    "get_news":                 ("news",         "📰 News & Sentiment"),
    "get_global_news":          ("news",         "🌐 Global Market News"),
    "get_sentiment_news":       ("social",       "💬 Sentiment News"),         # called by social analyst
    "get_insider_transactions": ("fundamentals", "👤 Insider Transactions"),   # called by fundamentals analyst
}


class DataFeedCallback(BaseCallbackHandler):
    """LangChain callback — fires on every tool call, emits raw data events immediately."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._pending: dict[str, str] = {}  # run_id -> tool_name

    def on_tool_start(self, serialized: dict, input_str: str, *, run_id: Any, **kwargs: Any) -> None:
        name = (serialized or {}).get("name", "")
        if name:
            self._pending[str(run_id)] = name

    def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        name = self._pending.pop(str(run_id), "")
        if not name:
            return
        content = _format_tool_output_for_feed(output)
        if not content:
            return
        analyst_key, label = _TOOL_LABELS.get(name, ("market", f"🔧 {name}"))
        _emit_event(self.job_id, "data_fetched", {
            "analyst": analyst_key,
            "tool": name,
            "label": label,
            "preview": content,
        })

    def on_tool_error(self, error: Any, *, run_id: Any, **kwargs: Any) -> None:
        name = self._pending.pop(str(run_id), "")
        error_msg = str(error)
        analyst_key, label = _TOOL_LABELS.get(name, ("market", f"🔧 {name}"))
        _log(f"[DataFetchError] tool={name or 'unknown'} analyst={analyst_key} error={error_msg}")
        # Store in error_notes so it can be injected into analyst dialogue context later
        with _jobs_lock:
            if self.job_id in _jobs:
                _jobs[self.job_id]["error_notes"].append({
                    "tool": name,
                    "analyst": analyst_key,
                    "label": label,
                    "error": error_msg,
                })
        _emit_event(self.job_id, "data_error", {
            "analyst": analyst_key,
            "tool": name,
            "label": label,
            "error": error_msg,
        })


def _emit_event(job_id: str, event_type: str, data: dict[str, Any]) -> None:
    """Emit event (thread-safe, appends to persistent event log)."""
    event = {"type": event_type, "timestamp": datetime.now(timezone.utc).isoformat(), **data}
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["event_log"].append(event)
    _log(f"[Event] {event_type}: {data.get('analyst', data.get('phase', ''))}")


def _build_report(state: dict[str, Any]) -> tuple[str, dict[str, str]]:
    sections: dict[str, str] = {}

    mapping = {
        "Market Analysis": state.get("market_report"),
        "Social Sentiment Analysis": state.get("sentiment_report"),
        "News Analysis": state.get("news_report"),
        "Fundamentals Analysis": state.get("fundamentals_report"),
        "Research Team Decision": state.get("investment_plan"),
        "Trader Plan": state.get("trader_investment_plan"),
        "Final Portfolio Decision": state.get("final_trade_decision"),
    }

    for title, content in mapping.items():
        text = _as_text(content)
        if text:
            sections[title] = text

    lines: list[str] = []
    for title, content in sections.items():
        lines.append(f"## {title}\n\n{content}")

    combined = "\n\n".join(lines) if lines else "No report content was generated."
    return combined, sections


def _execute_analysis(payload: AnalyzeRequest, job_id: str | None = None, cancel_event: ThreadEvent | None = None) -> dict[str, Any]:
    """Run the full analysis and return a result dict. Called by both sync and async paths."""
    config = _build_graph_config(payload)
    qp = config.get("quick_llm_provider") or config.get("llm_provider")
    dp = config.get("deep_llm_provider") or config.get("llm_provider")
    _log(
        "Analyze request received "
        f"ticker={payload.ticker} date={payload.analysis_date.isoformat()} "
        f"quick={qp} deep={dp} analysts={','.join(payload.selected_analysts)} "
        f"debate_rounds={config['max_debate_rounds']} risk_rounds={config['max_risk_discuss_rounds']}"
    )

    if job_id:
        _emit_event(job_id, "analysis_start", {
            "ticker": payload.ticker,
            "analysts": payload.selected_analysts,
        })

    def state_callback(prev_state: dict | None, curr_state: dict) -> None:
        if not job_id:
            return
        prev = prev_state or {}

        # ── Analyst reports
        for state_key, analyst_key, title in [
            ("market_report",          "market",          "Market Analysis"),
            ("sentiment_report",       "social",          "Social Sentiment"),
            ("news_report",            "news",            "News Analysis"),
            ("fundamentals_report",    "fundamentals",    "Fundamentals Analysis"),
            ("trader_investment_plan", "trader",          "Trader Plan"),
        ]:
            if not prev.get(state_key) and curr_state.get(state_key):
                _emit_event(job_id, "analyst_complete", {
                    "analyst": analyst_key, "title": title,
                    "content": _as_text(curr_state[state_key]),
                })

        # ── Bull/Bear debate messages
        for role, hist_key in [("bull", "bull_history"), ("bear", "bear_history")]:
            prev_hist = (prev.get("investment_debate_state") or {}).get(hist_key, "")
            curr_hist = (curr_state.get("investment_debate_state") or {}).get(hist_key, "")
            if curr_hist and len(curr_hist) > len(prev_hist):
                new_text = curr_hist[len(prev_hist):].strip()
                if new_text:
                    _emit_event(job_id, "debate_message", {"role": role, "content": new_text})

        # Research Manager decision (investment_plan)
        if not prev.get("investment_plan") and curr_state.get("investment_plan"):
            _emit_event(job_id, "analyst_complete", {
                "analyst": "research_manager",
                "title": "Research Manager Decision",
                "content": _as_text(curr_state["investment_plan"]),
            })

        # ── Risk debate messages
        for role, hist_key in [
            ("aggressive",   "aggressive_history"),
            ("conservative", "conservative_history"),
            ("neutral",      "neutral_history"),
        ]:
            prev_hist = (prev.get("risk_debate_state") or {}).get(hist_key, "")
            curr_hist = (curr_state.get("risk_debate_state") or {}).get(hist_key, "")
            if curr_hist and len(curr_hist) > len(prev_hist):
                new_text = curr_hist[len(prev_hist):].strip()
                if new_text:
                    _emit_event(job_id, "risk_message", {"role": role, "content": new_text})

        # ── Portfolio Manager final decision
        if not prev.get("final_trade_decision") and curr_state.get("final_trade_decision"):
            _emit_event(job_id, "analyst_complete", {
                "analyst": "portfolio",
                "title": "Portfolio Manager Decision",
                "content": _as_text(curr_state["final_trade_decision"]),
            })

    # Run analysis for requested language
    graph_requested = TradingAgentsGraph(
        selected_analysts=payload.selected_analysts,
        debug=payload.debug,
        config=config,
        language=payload.language,
    )

    callbacks = [DataFeedCallback(job_id)] if job_id else []

    if job_id:
        _emit_event(job_id, "phase_update", {"phase": "data_collection", "message": "Fetching market data..."})

    final_state_requested, decision_requested = graph_requested.propagate(
        payload.ticker,
        payload.analysis_date.isoformat(),
        state_callback=state_callback,
        callbacks=callbacks,
        cancel_event=cancel_event,
    )

    if job_id:
        _emit_event(job_id, "phase_update", {"phase": "analysis_complete", "message": "Analysis complete"})

    # Build report from the requested language state (for display to user)
    human_report, sections = _build_report(final_state_requested)
    _log(
        "Analyze completed "
        f"ticker={payload.ticker} decision={_as_text(decision_requested) or 'N/A'} "
        f"final_trade_decision={_as_text(final_state_requested.get('final_trade_decision')) or 'N/A'} "
        f"language={payload.language}"
    )

    pdf_filenames: list[str] = []
    pdf_download_urls: list[str] = []

    try:
        fname = export_filename(
            payload.ticker,
            payload.analysis_date,
            language=payload.language,
        )
        out_path = unique_path(_exports_dir(), fname)
        write_analysis_pdf(
            out_path,
            ticker=payload.ticker,
            analysis_date=payload.analysis_date,
            analysts=list(payload.selected_analysts),
            decision=_as_text(decision_requested),
            human_readable_report=human_report,
            language=payload.language,
        )
        _log(f"✔ PDF saved locally ({payload.language.upper()}): {out_path.name}")
        pdf_filenames.append(out_path.name)
        pdf_download_urls.append(f"/api/exports/download/{out_path.name}")
    except Exception as pdf_exc:
        _log(f"✘ PDF export failed: {pdf_exc}")

    primary_pdf = pdf_filenames[0] if pdf_filenames else None
    return {
        "decision": _as_text(decision_requested),
        "final_trade_decision": _as_text(final_state_requested.get("final_trade_decision")),
        "human_readable_report": human_report,
        "sections": sections,
        "raw_state": final_state_requested,
        "pdf_filename": primary_pdf,
        "pdf_filenames": pdf_filenames if pdf_filenames else None,
        "pdf_download_urls": pdf_download_urls if pdf_download_urls else None,
    }


def _cache_lookup(payload: AnalyzeRequest, label: str = "") -> dict[str, Any] | None:
    """Check the DB cache for a previous analysis with identical parameters.

    Returns the cached result dict on hit, or None on miss / DB unavailable.
    The cache key also includes an LLM profile (providers, models, backends, effort).
    """
    prefix = f"[{label}] " if label else ""
    cfg = _build_graph_config(payload)
    try:
        cached = db.get_cached_analysis(
            payload.ticker,
            payload.analysis_date,
            list(payload.selected_analysts),
            language=payload.language,
            llm_profile=_llm_cache_profile_from_config(cfg),
        )
    except Exception as exc:
        _log(f"{prefix}DB lookup error (skipping cache): {exc}")
        return None

    if cached is None:
        _log(
            f"{prefix}Cache miss — ticker={payload.ticker} "
            f"date={payload.analysis_date} lang={payload.language} "
            f"analysts={sorted(payload.selected_analysts)}"
        )
    return cached


def _cache_save(payload: AnalyzeRequest, result: dict[str, Any],
                events: list[dict[str, Any]], label: str = "") -> None:
    """Persist an analysis result to the DB cache (non-fatal on error)."""
    prefix = f"[{label}] " if label else ""
    cfg = _build_graph_config(payload)
    try:
        pdf_name = result.get("pdf_filename")
        if not pdf_name and result.get("pdf_filenames"):
            pdf_name = result["pdf_filenames"][0]
        pdf_path = str(_exports_dir() / pdf_name) if pdf_name else None
        if db.save_analysis(
            payload.ticker,
            payload.analysis_date,
            list(payload.selected_analysts),
            result,
            events,
            pdf_path,
            language=payload.language,
            llm_profile=_llm_cache_profile_from_config(cfg),
        ):
            _log(f"{prefix}Saved to DB cache ticker={payload.ticker}")
        else:
            _log(
                f"{prefix}DB cache not used — set DATABASE_URL "
                "and ensure psycopg2 is installed"
            )
    except Exception as db_exc:
        _log(f"{prefix}DB save failed (non-fatal): {db_exc}")


def _replay_cached_job(job_id: str, payload: AnalyzeRequest, cached: dict[str, Any]) -> None:
    """Replay a cached analysis: write PDF if needed, then push all stored events."""
    _log(f"[Job {job_id[:8]}] Cache hit — replaying ticker={payload.ticker}")

    # Restore PDF to disk if we have the bytes and the file is missing
    pdf_filename: str | None = cached.get("pdf_filename")
    pdf_download_url: str | None = None
    exports = _exports_dir()

    if pdf_filename and cached.get("pdf_data"):
        exports.mkdir(parents=True, exist_ok=True)
        pdf_path = exports / pdf_filename
        if not pdf_path.exists():
            try:
                pdf_path.write_bytes(cached["pdf_data"])
                _log(f"[Job {job_id[:8]}] Restored PDF from DB: {pdf_filename}")
            except Exception as exc:
                _log(f"[Job {job_id[:8]}] PDF restore failed: {exc}")
                pdf_filename = None

    # Set download URL if PDF file is available on disk (regardless of whether
    # pdf_data bytes were stored — old cache entries may have NULL pdf_data but
    # the file can still be present from the original analysis run).
    if pdf_filename and (exports / pdf_filename).exists():
        pdf_download_url = f"/api/exports/download/{pdf_filename}"

    # Prepend a cache_hit notice so the UI can show it
    now = datetime.now(timezone.utc).isoformat()
    cache_notice: dict[str, Any] = {
        "type": "cache_hit",
        "timestamp": now,
        "message": "Replaying cached analysis — no LLM calls needed",
        "ticker": payload.ticker,
    }

    # Patch the stored job_complete event with fresh PDF info
    replayed_events: list[dict[str, Any]] = []
    for event in cached.get("events", []):
        if event.get("type") == "job_complete":
            replayed_events.append({
                **event,
                "decision": cached["decision"],
                "pdf_filename": pdf_filename,
                "pdf_download_url": pdf_download_url,
            })
        else:
            replayed_events.append(event)

    # Bulk-append to the job's event log (WebSocket handler picks them up)
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["event_log"].append(cache_notice)
            _jobs[job_id]["event_log"].extend(replayed_events)

    # Mark job as done — set both singular and plural PDF keys so that
    # JobStatusResponse (which uses plural) and the WS job_complete event
    # (which uses singular) both work correctly for cache hits.
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                decision=cached["decision"],
                pdf_filename=pdf_filename,
                pdf_download_url=pdf_download_url,
                pdf_filenames=[pdf_filename] if pdf_filename else None,
                pdf_download_urls=[pdf_download_url] if pdf_download_url else None,
                sections=cached.get("sections"),
            )
    _log(f"[Job {job_id[:8]}] Cache replay complete ticker={payload.ticker}")


def _run_analysis_job(job_id: str, payload: AnalyzeRequest) -> None:
    """Background worker: checks DB cache first, then runs analysis if needed."""
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        cancel_event = _jobs[job_id]["cancel_event"]
    _log(f"[Job {job_id[:8]}] Starting ticker={payload.ticker}")

    # ── Cache lookup (global: same ticker/date/analysts/lang = same result) ──
    cached = _cache_lookup(payload, label=job_id[:8])
    if cached is not None:
        try:
            _replay_cached_job(job_id, payload, cached)
        except Exception as replay_exc:
            _log(f"[Job {job_id[:8]}] Cache replay failed ({replay_exc}); falling back to fresh analysis")
            _emit_event(job_id, "cache_replay_error", {"error": str(replay_exc)})
            # Fall through to run a fresh analysis rather than leaving job stuck
        else:
            return

    # ── Cache miss: run full analysis ───────────────────────────────────────
    try:
        result = _execute_analysis(payload, job_id=job_id, cancel_event=cancel_event)

        # Emit completion event (include pdf_filename — UI listens for singular key)
        _urls = result.get("pdf_download_urls") or []
        _emit_event(job_id, "job_complete", {
            "decision": result["decision"],
            "pdf_filename": result.get("pdf_filename"),
            "pdf_download_url": _urls[0] if _urls else None,
            "pdf_filenames": result["pdf_filenames"],
            "pdf_download_urls": result["pdf_download_urls"],
        })

        with _jobs_lock:
            _jobs[job_id].update(
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                decision=result["decision"],
                pdf_filenames=result["pdf_filenames"],
                pdf_download_urls=result["pdf_download_urls"],
                sections=result.get("sections"),
            )
            events_snapshot = list(_jobs[job_id]["event_log"])
        _log(f"[Job {job_id[:8]}] Done ticker={payload.ticker}")

        # Save result for the requested language
        _cache_save(payload, result, events_snapshot, label=job_id[:8])

    except InterruptedError:
        _log(f"[Job {job_id[:8]}] Cancelled ticker={payload.ticker}")
        _emit_event(job_id, "job_cancelled", {})
        with _jobs_lock:
            _jobs[job_id].update(
                status="cancelled",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error="Cancelled by user",
            )
    except Exception as exc:
        _log(f"[Job {job_id[:8]}] Failed ticker={payload.ticker} error={exc}")
        _emit_event(job_id, "job_failed", {"error": str(exc)})
        with _jobs_lock:
            _jobs[job_id].update(
                status="failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=str(exc),
            )


app = FastAPI(
    title="TradingAgents API",
    description="HTTP wrapper for TradingAgents multi-agent analysis",
    version="0.1.0",
)

_cors_raw = os.getenv("TRADINGAGENTS_CORS_ORIGINS", "*").strip()
if _cors_raw == "*":
    _cors_origins: list[str] = ["*"]
else:
    _cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    try:
        db.init_db()
        _log("DB cache initialised")
    except Exception as exc:
        _log(f"DB cache init failed (caching disabled): {exc}")

    # Load ticker list in a background thread so startup is not blocked.
    import threading
    def _load_tickers() -> None:
        try:
            _ticker_svc.load()
            _log(f"Ticker index ready: {_ticker_svc.count()} symbols")
        except Exception as exc:
            _log(f"Ticker load failed (non-fatal): {exc}")
    threading.Thread(target=_load_tickers, daemon=True).start()

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.post("/api/admin/login")
def admin_login(body: AdminLoginRequest, response: Response) -> dict[str, bool]:
    if not _admin_password_configured():
        raise HTTPException(
            status_code=503,
            detail="Admin login is not configured (set TRADINGAGENTS_ADMIN_PASSWORD in .env).",
        )
    if not _admin_verify_credentials(body.username.strip(), body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = _admin_issue_token()
    if not token:
        raise HTTPException(status_code=503, detail="Could not issue session (check admin env vars).")
    response.set_cookie(
        key=_ADMIN_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=_ADMIN_COOKIE_MAX_AGE,
        path="/",
    )
    return {"ok": True}


@app.post("/api/admin/logout")
def admin_logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(_ADMIN_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/admin/me")
def admin_me(request: Request) -> dict[str, Any]:
    tok = request.cookies.get(_ADMIN_COOKIE)
    if _admin_verify_token(tok):
        return {"ok": True, "user": _admin_username()}
    raise HTTPException(status_code=401, detail="Not authenticated")


@app.get("/api/admin/settings")
def admin_settings_get(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return _admin_settings_get_payload()


@app.put("/api/admin/settings")
def admin_settings_put(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_admin(request)
    merged = _admin_sanitize_put_body(body)
    if not db.save_app_settings(merged):
        raise HTTPException(
            status_code=503,
            detail="Could not save settings. Set DATABASE_URL and ensure psycopg2 is installed.",
        )
    return {"ok": True}


@app.get("/api/options")
def options() -> dict[str, Any]:
    stored = db.get_app_settings()
    cfg = DEFAULT_CONFIG.copy()
    if stored:
        _apply_map_from_stored(cfg, stored)
    _apply_kimi_custom_models(cfg)
    qp = cfg.get("quick_llm_provider") or cfg.get("llm_provider")
    dp = cfg.get("deep_llm_provider") or cfg.get("llm_provider")
    return {
        "analysts": ANALYST_OPTIONS,
        "llm_provider_default": cfg.get("llm_provider"),
        "quick_llm_provider_default": qp,
        "deep_llm_provider_default": dp,
        "deep_think_default": cfg.get("deep_think_llm"),
        "quick_think_default": cfg.get("quick_think_llm"),
        "max_debate_rounds_default": _coerce_round_int(
            cfg.get("max_debate_rounds"), int(DEFAULT_CONFIG["max_debate_rounds"])
        ),
        "max_risk_discuss_rounds_default": _coerce_round_int(
            cfg.get("max_risk_discuss_rounds"),
            int(DEFAULT_CONFIG["max_risk_discuss_rounds"]),
        ),
        "backend_url_default": cfg.get("backend_url") or "",
        "global_settings_from_db": bool(stored),
    }


@app.get("/api/tickers/search")
def ticker_search(q: str = "", limit: int = 10) -> dict[str, Any]:
    """Autocomplete search over the in-memory US stock ticker index."""
    q = q.strip()
    if not q:
        return {"results": [], "loaded": _ticker_svc.is_loaded()}
    capped = max(1, min(limit, 20))
    return {
        "results": _ticker_svc.search(q, capped),
        "loaded": _ticker_svc.is_loaded(),
    }


@app.get("/api/logs")
def logs(limit: int = 200) -> dict[str, Any]:
    capped_limit = max(1, min(limit, 1000))
    return {"logs": _get_logs(capped_limit)}


@app.delete("/api/logs")
def clear_logs() -> dict[str, bool]:
    _clear_logs()
    return {"cleared": True}


@app.get("/api/exports")
def list_exports() -> dict[str, Any]:
    d = _exports_dir()
    if not d.is_dir():
        return {"files": []}
    files: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.pdf"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        files.append(
            {
                "filename": p.name,
                "size_bytes": st.st_size,
                "modified_unix": int(st.st_mtime),
            }
        )
    return {"files": files}


@app.get("/api/exports/download/{filename}")
def download_export(filename: str) -> FileResponse:
    path = _resolve_export_file(filename)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="attachment",
    )


@app.post("/api/exports/zip")
def download_selected_exports_zip(body: ExportZipRequest) -> StreamingResponse:
    if not body.filenames:
        raise HTTPException(status_code=400, detail="No filenames provided")
    paths = _paths_for_export_filenames(body.filenames)
    if not paths:
        raise HTTPException(status_code=404, detail="No matching PDF files")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, arcname=p.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tradingagents-selected.zip"'},
    )


@app.get("/api/exports/all.zip")
def download_all_exports_zip() -> StreamingResponse:
    exports = _exports_dir()
    pdfs = sorted(exports.glob("*.pdf")) if exports.is_dir() else []
    if not pdfs:
        raise HTTPException(status_code=404, detail="No PDF exports available")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in pdfs:
            zf.write(p, arcname=p.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tradingagents-exports.zip"'},
    )


# ── Async job endpoints ───────────────────────────────────────────────────────

@app.post("/api/jobs", response_model=JobSubmitResponse)
def submit_job(payload: AnalyzeRequest) -> JobSubmitResponse:
    """Submit an analysis job. Returns immediately with a job_id to poll."""
    payload = _normalize_analyze_request(payload)
    # Validate ticker against the loaded index (skip if index not ready yet)
    valid = _ticker_svc.exists(payload.ticker)
    if valid is False:
        raise HTTPException(
            status_code=422,
            detail=f"'{payload.ticker}' is not a recognised US stock ticker. "
                   "Please select a ticker from the suggestions.",
        )
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "pending",
            "ticker": payload.ticker,
            "created_at": now,
            "completed_at": None,
            "decision": None,
            "pdf_filenames": None,
            "pdf_download_urls": None,
            "error": None,
            "event_log": [],      # persistent — survives reconnects
            "error_notes": [],    # data-fetch errors for dialogue context
            "cancel_event": ThreadEvent(),
        }
        # Trim oldest jobs if over limit
        if len(_jobs) > MAX_JOBS_STORE:
            oldest = sorted(_jobs.keys(), key=lambda k: _jobs[k]["created_at"])
            for k in oldest[: len(_jobs) - MAX_JOBS_STORE]:
                del _jobs[k]
    _executor.submit(_run_analysis_job, job_id, payload)
    _log(f"[Job {job_id[:8]}] Queued ticker={payload.ticker}")
    return JobSubmitResponse(
        job_id=job_id,
        status="pending",
        message="Job queued. Connect to /ws/job/{job_id} for live results.",
        analysis_date=payload.analysis_date,
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str) -> JobStatusResponse:
    """Poll job status. Status: pending | running | done | failed."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found. The server may have restarted — check the exports list for your PDF.",
        )
    job_data = {k: v for k, v in job.items() if k not in ("event_log", "error_notes", "cancel_event")}
    return JobStatusResponse(job_id=job_id, **job_data)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    """Signal a running job to stop at the next graph checkpoint."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Job is already {job['status']}")
    job["cancel_event"].set()
    _log(f"[Job {job_id[:8]}] Cancel requested")
    return {"cancelled": True, "job_id": job_id}


@app.get("/api/jobs")
def list_recent_jobs() -> dict[str, Any]:
    """List the most recent 20 jobs (in-memory only, lost on server restart)."""
    with _jobs_lock:
        snapshot = [(jid, dict(j)) for jid, j in _jobs.items()]
    snapshot.sort(key=lambda x: x[1]["created_at"], reverse=True)
    return {"jobs": [{"job_id": jid, **job} for jid, job in snapshot[:20]]}


@app.websocket("/ws/job/{job_id}")
async def websocket_job_stream(websocket: WebSocket, job_id: str) -> None:
    """WebSocket stream of live analysis events for a job.
    Uses a persistent event log so reconnects replay all prior events.
    """
    import asyncio
    await websocket.accept()
    _log(f"[WS] Client connected to job {job_id[:8]}")

    with _jobs_lock:
        if job_id not in _jobs:
            await websocket.send_json({"type": "error", "message": "Job not found"})
            await websocket.close()
            return

    sent_index = 0  # how many events from the log we've sent so far
    try:
        while True:
            # Grab any new events since last send
            with _jobs_lock:
                if job_id not in _jobs:
                    break
                job = _jobs[job_id]
                new_events = job["event_log"][sent_index:]
                status = job["status"]

            for event in new_events:
                await websocket.send_json(event)
                sent_index += 1

            # If job is finished and all events sent, close cleanly
            if status in ("done", "failed", "cancelled") and not new_events:
                break

            # Keep-alive ping while waiting for new events
            try:
                await websocket.send_json({"type": "ping"})
            except Exception:
                break

            await asyncio.sleep(0.5)

    except WebSocketDisconnect:
        _log(f"[WS] Client disconnected from job {job_id[:8]}")
    except Exception as exc:
        _log(f"[WS] Error on job {job_id[:8]}: {exc}")
    finally:
        _log(f"[WS] Closed connection for job {job_id[:8]}")


# ── Legacy synchronous endpoint (kept for backward compatibility) ─────────────

@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    payload = _normalize_analyze_request(payload)
    # Always check DB cache first — same key as the async job path so results
    # are shared across all users, sessions, and entry points.
    cached = _cache_lookup(payload, label="sync")
    if cached is not None:
        _log(f"[sync] Cache hit ticker={payload.ticker} lang={payload.language}")
        return AnalyzeResponse(
            decision=cached["decision"],
            final_trade_decision=cached.get("final_trade_decision", ""),
            human_readable_report=cached.get("human_readable_report", ""),
            sections=cached.get("sections") or {},
            raw_state={},
            pdf_filenames=None,
            pdf_download_urls=None,
            analysis_date=payload.analysis_date,
        )
    try:
        result = _execute_analysis(payload, job_id=None)
        _cache_save(payload, result, [], label="sync")
        return AnalyzeResponse(
            decision=result["decision"],
            final_trade_decision=result["final_trade_decision"],
            human_readable_report=result["human_readable_report"],
            sections=result["sections"],
            raw_state=result["raw_state"],
            pdf_filenames=result.get("pdf_filenames"),
            pdf_download_urls=result.get("pdf_download_urls"),
            analysis_date=payload.analysis_date,
        )
    except Exception as exc:
        _log(f"Analyze failed ticker={payload.ticker} error={exc}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc


def run() -> None:
    import uvicorn

    host = os.getenv("TRADINGAGENTS_SERVICE_HOST", "0.0.0.0")
    port = int(os.getenv("TRADINGAGENTS_SERVICE_PORT", "8000"))
    uvicorn.run("service.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
