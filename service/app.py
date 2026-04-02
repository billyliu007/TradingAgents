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
from threading import Lock
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from service.pdf_export import export_filename, unique_path, write_analysis_pdf

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
    status: str  # pending | running | done | failed
    ticker: str
    created_at: str
    completed_at: str | None = None
    decision: str | None = None
    pdf_filename: str | None = None
    pdf_download_url: str | None = None
    error: str | None = None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


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


def _execute_analysis(payload: AnalyzeRequest) -> dict[str, Any]:
    """Run the full analysis and return a result dict. Called by both sync and async paths."""
    _log(
        "Analyze request received "
        f"ticker={payload.ticker} date={payload.analysis_date.isoformat()} "
        f"provider={payload.llm_provider} analysts={','.join(payload.selected_analysts)} "
        f"debate_rounds={payload.max_debate_rounds} risk_rounds={payload.max_risk_discuss_rounds}"
    )
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

    graph = TradingAgentsGraph(
        selected_analysts=payload.selected_analysts,
        debug=payload.debug,
        config=config,
        progress_callback=lambda msg: _log(f"Progress: {msg}"),
    )
    final_state, decision = graph.propagate(payload.ticker, payload.analysis_date.isoformat())
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
        _log(f"PDF export saved: {pdf_filename}")
    except Exception as pdf_exc:
        _log(f"PDF export failed: {pdf_exc}")

    return {
        "decision": _as_text(decision),
        "final_trade_decision": _as_text(final_state.get("final_trade_decision")),
        "human_readable_report": human_report,
        "sections": sections,
        "raw_state": final_state,
        "pdf_filename": pdf_filename,
        "pdf_download_url": pdf_download_url,
    }


def _run_analysis_job(job_id: str, payload: AnalyzeRequest) -> None:
    """Background worker: runs analysis and updates job state."""
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
    _log(f"[Job {job_id[:8]}] Starting ticker={payload.ticker}")
    try:
        result = _execute_analysis(payload)
        with _jobs_lock:
            _jobs[job_id].update(
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                decision=result["decision"],
                pdf_filename=result["pdf_filename"],
                pdf_download_url=result["pdf_download_url"],
            )
        _log(f"[Job {job_id[:8]}] Done ticker={payload.ticker}")
    except Exception as exc:
        _log(f"[Job {job_id[:8]}] Failed ticker={payload.ticker} error={exc}")
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
        message="Job queued. Poll GET /api/jobs/{job_id} for status.",
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
    return JobStatusResponse(job_id=job_id, **job)


@app.get("/api/jobs")
def list_recent_jobs() -> dict[str, Any]:
    """List the most recent 20 jobs (in-memory only, lost on server restart)."""
    with _jobs_lock:
        snapshot = [(jid, dict(j)) for jid, j in _jobs.items()]
    snapshot.sort(key=lambda x: x[1]["created_at"], reverse=True)
    return {"jobs": [{"job_id": jid, **job} for jid, job in snapshot[:20]]}


# ── Legacy synchronous endpoint (kept for backward compatibility) ─────────────

@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    try:
        result = _execute_analysis(payload)
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
