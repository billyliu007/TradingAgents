from __future__ import annotations

import io
import os
import re
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from threading import Lock, Event as ThreadEvent
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from langchain_core.callbacks import BaseCallbackHandler
import json

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from service.pdf_export import export_filename, unique_path, write_analysis_pdf
from service import db

load_dotenv()

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"

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


class AnalyzeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, description="Ticker, e.g. NVDA")
    analysis_date: date
    selected_analysts: list[Literal["market", "social", "news", "fundamentals"]] = Field(
        default_factory=lambda: ANALYST_OPTIONS.copy()
    )
    llm_provider: Literal["openai", "google", "anthropic", "xai", "openrouter", "ollama"] = "openai"
    backend_url: str | None = None
    deep_think_llm: str = "gpt-5.2"
    quick_think_llm: str = "gpt-5-mini"
    max_debate_rounds: int = Field(default=1, ge=1, le=5)
    max_risk_discuss_rounds: int = Field(default=1, ge=1, le=5)
    google_thinking_level: str | None = None
    openai_reasoning_effort: str | None = None
    anthropic_effort: str | None = None
    debug: bool = False


class AnalyzeResponse(BaseModel):
    decision: str
    final_trade_decision: str
    human_readable_report: str
    sections: dict[str, str]
    raw_state: dict[str, Any]
    pdf_filename: str | None = None
    pdf_download_url: str | None = None


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str  # pending | running | done | failed | cancelled
    ticker: str
    created_at: str
    completed_at: str | None = None
    decision: str | None = None
    pdf_filename: str | None = None
    pdf_download_url: str | None = None
    sections: dict[str, str] | None = None
    error: str | None = None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


_TOOL_LABELS: dict[str, tuple[str, str]] = {
    "get_stock_data":           ("market",       "📈 Stock Price & Volume"),
    "get_indicators":           ("market",       "📊 Technical Indicators"),
    "get_fundamentals":         ("fundamentals", "🏦 Company Fundamentals"),
    "get_balance_sheet":        ("fundamentals", "📋 Balance Sheet"),
    "get_cashflow":             ("fundamentals", "💵 Cash Flow Statement"),
    "get_income_statement":     ("fundamentals", "📄 Income Statement"),
    "get_news":                 ("news",         "📰 News & Sentiment"),
    "get_global_news":          ("news",         "🌐 Global Market News"),
    "get_insider_transactions": ("news",         "👤 Insider Transactions"),
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
        content = str(output)[:1200] if output else ""
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
    _log(
        "Analyze request received "
        f"ticker={payload.ticker} date={payload.analysis_date.isoformat()} "
        f"provider={payload.llm_provider} analysts={','.join(payload.selected_analysts)} "
        f"debate_rounds={payload.max_debate_rounds} risk_rounds={payload.max_risk_discuss_rounds}"
    )

    if job_id:
        _emit_event(job_id, "analysis_start", {
            "ticker": payload.ticker,
            "analysts": payload.selected_analysts,
        })

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = payload.llm_provider
    config["deep_think_llm"] = payload.deep_think_llm
    config["quick_think_llm"] = payload.quick_think_llm
    config["max_debate_rounds"] = payload.max_debate_rounds
    config["max_risk_discuss_rounds"] = payload.max_risk_discuss_rounds
    config["google_thinking_level"] = payload.google_thinking_level
    config["openai_reasoning_effort"] = payload.openai_reasoning_effort
    config["anthropic_effort"] = payload.anthropic_effort

    if payload.backend_url:
        config["backend_url"] = payload.backend_url

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

    graph = TradingAgentsGraph(
        selected_analysts=payload.selected_analysts,
        debug=payload.debug,
        config=config,
    )

    callbacks = [DataFeedCallback(job_id)] if job_id else []

    if job_id:
        _emit_event(job_id, "phase_update", {"phase": "data_collection", "message": "Fetching market data..."})

    final_state, decision = graph.propagate(
        payload.ticker,
        payload.analysis_date.isoformat(),
        state_callback=state_callback,
        callbacks=callbacks,
        cancel_event=cancel_event,
    )

    if job_id:
        _emit_event(job_id, "phase_update", {"phase": "analysis_complete", "message": "Analysis complete"})

    human_report, sections = _build_report(final_state)
    _log(
        "Analyze completed "
        f"ticker={payload.ticker} decision={_as_text(decision) or 'N/A'} "
        f"final_trade_decision={_as_text(final_state.get('final_trade_decision')) or 'N/A'}"
    )

    pdf_filename: str | None = None
    pdf_download_url: str | None = None
    try:
        fname = export_filename(
            payload.ticker, payload.analysis_date, payload.selected_analysts
        )
        out_path = unique_path(_exports_dir(), fname)
        write_analysis_pdf(
            out_path,
            ticker=payload.ticker,
            analysis_date=payload.analysis_date,
            analysts=list(payload.selected_analysts),
            decision=_as_text(decision),
            human_readable_report=human_report,
        )
        pdf_filename = out_path.name
        pdf_download_url = f"/api/exports/download/{out_path.name}"
        _log(f"✔ PDF saved locally: {pdf_filename}")
    except Exception as pdf_exc:
        _log(f"✘ PDF export failed: {pdf_exc}")

    return {
        "decision": _as_text(decision),
        "final_trade_decision": _as_text(final_state.get("final_trade_decision")),
        "human_readable_report": human_report,
        "sections": sections,
        "raw_state": final_state,
        "pdf_filename": pdf_filename,
        "pdf_download_url": pdf_download_url,
    }


def _replay_cached_job(job_id: str, payload: AnalyzeRequest, cached: dict[str, Any]) -> None:
    """Replay a cached analysis: write PDF if needed, then push all stored events."""
    _log(f"[Job {job_id[:8]}] Cache hit — replaying ticker={payload.ticker}")

    # Restore PDF to disk if we have the bytes and the file is missing
    pdf_filename: str | None = cached.get("pdf_filename")
    pdf_download_url: str | None = None

    if pdf_filename and cached.get("pdf_data"):
        exports = _exports_dir()
        exports.mkdir(parents=True, exist_ok=True)
        pdf_path = exports / pdf_filename
        if not pdf_path.exists():
            try:
                pdf_path.write_bytes(cached["pdf_data"])
                _log(f"[Job {job_id[:8]}] Restored PDF from DB: {pdf_filename}")
            except Exception as exc:
                _log(f"[Job {job_id[:8]}] PDF restore failed: {exc}")
                pdf_filename = None
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

    # Mark job as done
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                decision=cached["decision"],
                pdf_filename=pdf_filename,
                pdf_download_url=pdf_download_url,
                sections=cached.get("sections"),
            )
    _log(f"[Job {job_id[:8]}] Cache replay complete ticker={payload.ticker}")


def _run_analysis_job(job_id: str, payload: AnalyzeRequest) -> None:
    """Background worker: checks DB cache first, then runs analysis if needed."""
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        cancel_event = _jobs[job_id]["cancel_event"]
    _log(f"[Job {job_id[:8]}] Starting ticker={payload.ticker}")

    # ── Cache lookup ────────────────────────────────────────────────────────
    try:
        cached = db.get_cached_analysis(
            payload.ticker, payload.analysis_date, list(payload.selected_analysts)
        )
    except Exception as exc:
        _log(f"[Job {job_id[:8]}] DB lookup error (continuing without cache): {exc}")
        cached = None

    if cached is not None:
        _replay_cached_job(job_id, payload, cached)
        return

    # ── Cache miss: run full analysis ───────────────────────────────────────
    try:
        result = _execute_analysis(payload, job_id=job_id, cancel_event=cancel_event)

        # Emit completion event
        _emit_event(job_id, "job_complete", {
            "decision": result["decision"],
            "pdf_filename": result["pdf_filename"],
            "pdf_download_url": result["pdf_download_url"],
        })

        with _jobs_lock:
            _jobs[job_id].update(
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                decision=result["decision"],
                pdf_filename=result["pdf_filename"],
                pdf_download_url=result["pdf_download_url"],
                sections=result.get("sections"),
            )
            events_snapshot = list(_jobs[job_id]["event_log"])
        _log(f"[Job {job_id[:8]}] Done ticker={payload.ticker}")

        # ── Persist to DB cache ─────────────────────────────────────────────
        try:
            pdf_path: str | None = None
            if result.get("pdf_filename"):
                pdf_path = str(_exports_dir() / result["pdf_filename"])
            db.save_analysis(
                payload.ticker,
                payload.analysis_date,
                list(payload.selected_analysts),
                result,
                events_snapshot,
                pdf_path,
            )
            _log(f"[Job {job_id[:8]}] Saved to DB cache")
        except Exception as db_exc:
            _log(f"[Job {job_id[:8]}] DB save failed (non-fatal): {db_exc}")

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


@app.on_event("startup")
async def _startup() -> None:
    try:
        db.init_db()
        _log("DB cache initialised")
    except Exception as exc:
        _log(f"DB cache init failed (caching disabled): {exc}")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/options")
def options() -> dict[str, Any]:
    return {
        "analysts": ANALYST_OPTIONS,
        "llm_provider_default": DEFAULT_CONFIG["llm_provider"],
        "deep_think_default": DEFAULT_CONFIG["deep_think_llm"],
        "quick_think_default": DEFAULT_CONFIG["quick_think_llm"],
        "max_debate_rounds_default": DEFAULT_CONFIG["max_debate_rounds"],
        "max_risk_discuss_rounds_default": DEFAULT_CONFIG["max_risk_discuss_rounds"],
        "backend_url_default": DEFAULT_CONFIG["backend_url"],
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
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "pending",
            "ticker": payload.ticker,
            "created_at": now,
            "completed_at": None,
            "decision": None,
            "pdf_filename": None,
            "pdf_download_url": None,
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
    try:
        result = _execute_analysis(payload, job_id=None)
        return AnalyzeResponse(**result)
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
