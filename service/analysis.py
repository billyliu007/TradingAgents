from __future__ import annotations

import json
from datetime import date, datetime, timezone
from threading import Event as ThreadEvent
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from tradingagents.graph.trading_graph import TradingAgentsGraph

from service import db
from service.analysis_dates import normalize_analysis_date
from service.content_sanitize import (
    sanitize_event_payload,
    sanitize_log_event,
    strip_llm_fake_tool_artifacts,
)
from service.constants import ANALYST_OPTIONS
from service.export_paths import exports_dir
from service.job_store import jobs, jobs_lock
from service.pdf_export import export_filename, unique_path, write_analysis_pdf
from service.schemas import AnalyzeRequest
from service.server_logging import log_message
from service.settings_ops import build_graph_config, llm_cache_profile_from_config


def pdf_filename_calendar_date(payload: AnalyzeRequest) -> date:
    """Date segment in ``export_filename`` — user-local when provided, else session ``analysis_date``."""
    if payload.pdf_filename_date is not None:
        return payload.pdf_filename_date
    return payload.analysis_date


def normalize_analyze_request(payload: AnalyzeRequest) -> AnalyzeRequest:
    """Normalize ``analysis_date`` (US Eastern session day) and always use all data analysts."""
    d = normalize_analysis_date(payload.analysis_date)
    analysts = list(ANALYST_OPTIONS)
    if d == payload.analysis_date and list(payload.selected_analysts) == analysts:
        return payload
    return payload.model_copy(update={"analysis_date": d, "selected_analysts": analysts})


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _portfolio_section_heading(language: str) -> str:
    """Markdown section title for final decision + WebSocket `title` (matches UI / PDF)."""
    lk = (language or "en").strip().lower()
    by_lang = {
        "en": "Portfolio decision — not financial advice",
        "zh": "投资组合决策 — 非投资建议",
        "zh-hant": "投資組合決策 — 非投資建議",
        "es": "Decisión de cartera — no es asesoramiento financiero",
        "ja": "ポートフォリオ判断 — 投資助言ではありません",
    }
    return by_lang.get(lk, by_lang["en"])


_TOOL_FEED_TRUNCATION_DISCLAIMER = (
    "Abbreviated for display in this feed only. The complete data returned by tools is still "
    "used to produce your research results. For research and education — not investment, legal, or tax advice."
)


def format_tool_output_for_feed(output: Any, max_len: int = 4500) -> str:
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
                # Important: keep JSON valid even when truncating.
                # The UI prefers JSON it can parse/render vs. a cut-off string.
                obj: Any = output
                s = json.dumps(obj, indent=2, default=str, ensure_ascii=False)
                if len(s) > max_len:
                    if isinstance(obj, list):
                        original_len = len(obj)
                        # progressively shrink until it fits (keep JSON valid)
                        cap = min(original_len, 50)
                        while cap > 1:
                            preview = obj[:cap] + [f"… (truncated). {_TOOL_FEED_TRUNCATION_DISCLAIMER}"]
                            s2 = json.dumps(preview, indent=2, default=str, ensure_ascii=False)
                            if len(s2) <= max_len:
                                s = s2
                                break
                            cap = cap // 2
                        else:
                            s = json.dumps(
                                [f"… (truncated). {_TOOL_FEED_TRUNCATION_DISCLAIMER}"],
                                indent=2,
                                ensure_ascii=False,
                            )
                    else:
                        original_len = len(obj)
                        items = list(obj.items())
                        cap = min(original_len, 50)
                        while cap > 1:
                            preview = dict(items[:cap])
                            preview["…"] = (
                                f"(truncated: showing {cap} of {original_len} fields). "
                                f"{_TOOL_FEED_TRUNCATION_DISCLAIMER}"
                            )
                            s2 = json.dumps(preview, indent=2, default=str, ensure_ascii=False)
                            if len(s2) <= max_len:
                                s = s2
                                break
                            cap = cap // 2
                        else:
                            s = json.dumps(
                                {"…": f"(truncated). {_TOOL_FEED_TRUNCATION_DISCLAIMER}"},
                                indent=2,
                                ensure_ascii=False,
                            )
            else:
                s = str(output).strip()
        except Exception:
            s = str(output).strip()
    # For non-JSON strings, truncation is fine.
    if len(s) > max_len:
        s = (
            s[: max_len - 40].rstrip()
            + "\n\n… (truncated)\n\n"
            + _TOOL_FEED_TRUNCATION_DISCLAIMER
        )
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


def emit_event(job_id: str, event_type: str, data: dict[str, Any]) -> None:
    """Emit event (thread-safe, appends to persistent event log)."""
    data = sanitize_event_payload(event_type, data)
    event = {"type": event_type, "timestamp": datetime.now(timezone.utc).isoformat(), **data}
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["event_log"].append(event)
    log_message(f"[Event] {event_type}: {data.get('analyst', data.get('phase', ''))}")


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
        content = format_tool_output_for_feed(output)
        if not content:
            return
        analyst_key, label = _TOOL_LABELS.get(name, ("market", f"🔧 {name}"))
        emit_event(self.job_id, "data_fetched", {
            "analyst": analyst_key,
            "tool": name,
            "label": label,
            "preview": content,
        })

    def on_tool_error(self, error: Any, *, run_id: Any, **kwargs: Any) -> None:
        name = self._pending.pop(str(run_id), "")
        error_msg = str(error)
        analyst_key, label = _TOOL_LABELS.get(name, ("market", f"🔧 {name}"))
        log_message(f"[DataFetchError] tool={name or 'unknown'} analyst={analyst_key} error={error_msg}")
        # Store in error_notes so it can be injected into analyst dialogue context later
        with jobs_lock:
            if self.job_id in jobs:
                jobs[self.job_id]["error_notes"].append({
                    "tool": name,
                    "analyst": analyst_key,
                    "label": label,
                    "error": error_msg,
                })
        emit_event(self.job_id, "data_error", {
            "analyst": analyst_key,
            "tool": name,
            "label": label,
            "error": error_msg,
        })


def build_report(state: dict[str, Any], *, language: str = "en") -> tuple[str, dict[str, str]]:
    sections: dict[str, str] = {}

    lk = (language or "en").strip().lower()
    # Keep headings localized; body content is already language-controlled by the graph prompts.
    titles: dict[str, dict[str, str]] = {
        "en": {
            "market": "Market Analysis",
            "social": "Social Sentiment Analysis",
            "news": "News Analysis",
            "fundamentals": "Fundamentals Analysis",
            "research_decision": "Research Team Summary",
            "trader_plan": "Trader Plan",
        },
        "zh": {
            "market": "市场分析",
            "social": "情绪分析",
            "news": "新闻分析",
            "fundamentals": "基本面分析",
            "research_decision": "研究团队摘要",
            "trader_plan": "交易计划",
        },
        "zh-hant": {
            "market": "市場分析",
            "social": "情緒分析",
            "news": "新聞分析",
            "fundamentals": "基本面分析",
            "research_decision": "研究團隊摘要",
            "trader_plan": "交易計畫",
        },
        "es": {
            "market": "Análisis de mercado",
            "social": "Análisis de sentimiento",
            "news": "Análisis de noticias",
            "fundamentals": "Análisis fundamental",
            "research_decision": "Resumen del equipo de investigación",
            "trader_plan": "Plan del trader",
        },
        "ja": {
            "market": "マーケット分析",
            "social": "センチメント分析",
            "news": "ニュース分析",
            "fundamentals": "ファンダメンタルズ分析",
            "research_decision": "リサーチチーム要約",
            "trader_plan": "取引プラン",
        },
    }
    t = {**(titles.get(lk, titles["en"])), "final_label": _portfolio_section_heading(lk)}

    mapping = {
        t["market"]: state.get("market_report"),
        t["social"]: state.get("sentiment_report"),
        t["news"]: state.get("news_report"),
        t["fundamentals"]: state.get("fundamentals_report"),
        t["research_decision"]: state.get("investment_plan"),
        t["trader_plan"]: state.get("trader_investment_plan"),
        t["final_label"]: state.get("final_trade_decision"),
    }

    for title, content in mapping.items():
        text = strip_llm_fake_tool_artifacts(as_text(content))
        if text:
            sections[title] = text

    lines: list[str] = []
    for title, content in sections.items():
        lines.append(f"## {title}\n\n{content}")

    combined = "\n\n".join(lines) if lines else "No report content was generated."
    return combined, sections


def execute_analysis(
    payload: AnalyzeRequest,
    job_id: str | None = None,
    cancel_event: ThreadEvent | None = None,
) -> dict[str, Any]:
    """Run the full analysis and return a result dict. Called by both sync and async paths."""
    config = build_graph_config(payload)
    qp = config.get("quick_llm_provider") or config.get("llm_provider")
    dp = config.get("deep_llm_provider") or config.get("llm_provider")
    log_message(
        "Analyze request received "
        f"ticker={payload.ticker} date={payload.analysis_date.isoformat()} "
        f"quick={qp} deep={dp} analysts={','.join(payload.selected_analysts)} "
        f"debate_rounds={config['max_debate_rounds']} risk_rounds={config['max_risk_discuss_rounds']}"
    )

    if job_id:
        emit_event(job_id, "analysis_start", {
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
                emit_event(job_id, "analyst_complete", {
                    "analyst": analyst_key, "title": title,
                    "content": as_text(curr_state[state_key]),
                })

        # ── Bull/Bear debate messages
        for role, hist_key in [("bull", "bull_history"), ("bear", "bear_history")]:
            prev_hist = (prev.get("investment_debate_state") or {}).get(hist_key, "")
            curr_hist = (curr_state.get("investment_debate_state") or {}).get(hist_key, "")
            if curr_hist and len(curr_hist) > len(prev_hist):
                new_text = curr_hist[len(prev_hist):].strip()
                if new_text:
                    emit_event(job_id, "debate_message", {"role": role, "content": new_text})

        # Research Manager decision (investment_plan)
        if not prev.get("investment_plan") and curr_state.get("investment_plan"):
            emit_event(job_id, "analyst_complete", {
                "analyst": "research_manager",
                "title": "Research Manager Decision",
                "content": as_text(curr_state["investment_plan"]),
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
                    emit_event(job_id, "risk_message", {"role": role, "content": new_text})

        # ── Portfolio Manager simulated label / summary
        if not prev.get("final_trade_decision") and curr_state.get("final_trade_decision"):
            emit_event(job_id, "analyst_complete", {
                "analyst": "portfolio",
                "title": _portfolio_section_heading(payload.language),
                "content": as_text(curr_state["final_trade_decision"]),
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
        emit_event(job_id, "phase_update", {"phase": "data_collection", "message": "Fetching market data..."})

    final_state_requested, decision_requested = graph_requested.propagate(
        payload.ticker,
        payload.analysis_date.isoformat(),
        state_callback=state_callback,
        callbacks=callbacks,
        cancel_event=cancel_event,
    )

    if job_id:
        emit_event(job_id, "phase_update", {"phase": "analysis_complete", "message": "Analysis complete"})

    # Build report from the requested language state (for display to user)
    human_report, sections = build_report(final_state_requested, language=payload.language)
    log_message(
        "Analyze completed "
        f"ticker={payload.ticker} decision={as_text(decision_requested) or 'N/A'} "
        f"final_trade_decision={as_text(final_state_requested.get('final_trade_decision')) or 'N/A'} "
        f"language={payload.language}"
    )

    pdf_filenames: list[str] = []
    pdf_download_urls: list[str] = []

    try:
        fname = export_filename(
            payload.ticker,
            pdf_filename_calendar_date(payload),
            language=payload.language,
        )
        out_path = unique_path(exports_dir(), fname)
        write_analysis_pdf(
            out_path,
            ticker=payload.ticker,
            analysis_date=payload.analysis_date,
            analysts=list(payload.selected_analysts),
            decision=as_text(decision_requested),
            human_readable_report=human_report,
            language=payload.language,
        )
        log_message(f"✔ PDF saved locally ({payload.language.upper()}): {out_path.name}")
        pdf_filenames.append(out_path.name)
        pdf_download_urls.append(f"/api/exports/download/{out_path.name}")
    except Exception as pdf_exc:
        log_message(f"✘ PDF export failed: {pdf_exc}")

    primary_pdf = pdf_filenames[0] if pdf_filenames else None
    return {
        "decision": as_text(decision_requested),
        "final_trade_decision": as_text(final_state_requested.get("final_trade_decision")),
        "human_readable_report": human_report,
        "sections": sections,
        "raw_state": final_state_requested,
        "pdf_filename": primary_pdf,
        "pdf_filenames": pdf_filenames if pdf_filenames else None,
        "pdf_download_urls": pdf_download_urls if pdf_download_urls else None,
    }


def cache_lookup(payload: AnalyzeRequest, label: str = "") -> dict[str, Any] | None:
    """Check the DB cache for a previous analysis with identical parameters.

    Returns the cached result dict on hit, or None on miss / DB unavailable.
    The cache key also includes an LLM profile (providers, models, backends, effort).
    """
    prefix = f"[{label}] " if label else ""
    cfg = build_graph_config(payload)
    try:
        cached = db.get_cached_analysis(
            payload.ticker,
            payload.analysis_date,
            list(payload.selected_analysts),
            language=payload.language,
            llm_profile=llm_cache_profile_from_config(cfg),
        )
    except Exception as exc:
        log_message(f"{prefix}DB lookup error (skipping cache): {exc}")
        return None

    if cached is None:
        log_message(
            f"{prefix}Cache miss — ticker={payload.ticker} "
            f"date={payload.analysis_date} lang={payload.language} "
            f"analysts={sorted(payload.selected_analysts)}"
        )
    return cached


def cache_save(
    payload: AnalyzeRequest,
    result: dict[str, Any],
    events: list[dict[str, Any]],
    label: str = "",
) -> None:
    """Persist an analysis result to the DB cache (non-fatal on error)."""
    prefix = f"[{label}] " if label else ""
    cfg = build_graph_config(payload)
    try:
        pdf_name = result.get("pdf_filename")
        if not pdf_name and result.get("pdf_filenames"):
            pdf_name = result["pdf_filenames"][0]
        pdf_path = str(exports_dir() / pdf_name) if pdf_name else None
        if db.save_analysis(
            payload.ticker,
            payload.analysis_date,
            list(payload.selected_analysts),
            result,
            events,
            pdf_path,
            language=payload.language,
            llm_profile=llm_cache_profile_from_config(cfg),
        ):
            log_message(f"{prefix}Saved to DB cache ticker={payload.ticker}")
        else:
            log_message(
                f"{prefix}DB cache not used — set DATABASE_URL "
                "and ensure psycopg2 is installed"
            )
    except Exception as db_exc:
        log_message(f"{prefix}DB save failed (non-fatal): {db_exc}")


def replay_cached_job(job_id: str, payload: AnalyzeRequest, cached: dict[str, Any]) -> None:
    """Replay a cached analysis: write PDF if needed, then push all stored events."""
    log_message(f"[Job {job_id[:8]}] Cache hit — replaying ticker={payload.ticker}")

    # Restore PDF to disk if we have the bytes and the file is missing
    pdf_filename: str | None = cached.get("pdf_filename")
    pdf_download_url: str | None = None
    exports = exports_dir()

    if pdf_filename and cached.get("pdf_data"):
        exports.mkdir(parents=True, exist_ok=True)
        pdf_path = exports / pdf_filename
        if not pdf_path.exists():
            try:
                pdf_path.write_bytes(cached["pdf_data"])
                log_message(f"[Job {job_id[:8]}] Restored PDF from DB: {pdf_filename}")
            except Exception as exc:
                log_message(f"[Job {job_id[:8]}] PDF restore failed: {exc}")
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
            replayed_events.append(sanitize_log_event(event))

    # Bulk-append events, then mark done (singular + plural PDF keys for WS + JobStatusResponse).
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["event_log"].append(cache_notice)
            jobs[job_id]["event_log"].extend(replayed_events)
            jobs[job_id].update(
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                decision=cached["decision"],
                pdf_filename=pdf_filename,
                pdf_download_url=pdf_download_url,
                pdf_filenames=[pdf_filename] if pdf_filename else None,
                pdf_download_urls=[pdf_download_url] if pdf_download_url else None,
                sections=cached.get("sections"),
            )
    log_message(f"[Job {job_id[:8]}] Cache replay complete ticker={payload.ticker}")


def run_analysis_job(job_id: str, payload: AnalyzeRequest) -> None:
    """Background worker: checks DB cache first, then runs analysis if needed."""
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        cancel_event = jobs[job_id]["cancel_event"]
    log_message(f"[Job {job_id[:8]}] Starting ticker={payload.ticker}")

    # ── Cache lookup (global: same ticker/date/analysts/lang = same result) ──
    cached = cache_lookup(payload, label=job_id[:8])
    if cached is not None:
        try:
            replay_cached_job(job_id, payload, cached)
        except Exception as replay_exc:
            log_message(f"[Job {job_id[:8]}] Cache replay failed ({replay_exc}); falling back to fresh analysis")
            emit_event(job_id, "cache_replay_error", {"error": str(replay_exc)})
            # Fall through to run a fresh analysis rather than leaving job stuck
        else:
            return

    # ── Cache miss: run full analysis ───────────────────────────────────────
    try:
        result = execute_analysis(payload, job_id=job_id, cancel_event=cancel_event)

        # Emit completion event (include pdf_filename — UI listens for singular key)
        _urls = result.get("pdf_download_urls") or []
        emit_event(job_id, "job_complete", {
            "decision": result["decision"],
            "pdf_filename": result.get("pdf_filename"),
            "pdf_download_url": _urls[0] if _urls else None,
            "pdf_filenames": result["pdf_filenames"],
            "pdf_download_urls": result["pdf_download_urls"],
        })

        with jobs_lock:
            jobs[job_id].update(
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                decision=result["decision"],
                pdf_filenames=result["pdf_filenames"],
                pdf_download_urls=result["pdf_download_urls"],
                sections=result.get("sections"),
            )
            events_snapshot = list(jobs[job_id]["event_log"])
        log_message(f"[Job {job_id[:8]}] Done ticker={payload.ticker}")

        # Save result for the requested language
        cache_save(payload, result, events_snapshot, label=job_id[:8])

    except InterruptedError:
        log_message(f"[Job {job_id[:8]}] Cancelled ticker={payload.ticker}")
        emit_event(job_id, "job_cancelled", {})
        with jobs_lock:
            jobs[job_id].update(
                status="cancelled",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error="Cancelled by user",
            )
    except Exception as exc:
        log_message(f"[Job {job_id[:8]}] Failed ticker={payload.ticker} error={exc}")
        emit_event(job_id, "job_failed", {"error": str(exc)})
        with jobs_lock:
            jobs[job_id].update(
                status="failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error=str(exc),
            )
