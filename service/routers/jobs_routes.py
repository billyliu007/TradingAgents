from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from threading import Event as ThreadEvent
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from service import tickers as _ticker_svc
from service.analysis import (
    cache_lookup,
    cache_save,
    execute_analysis,
    normalize_analyze_request,
    run_analysis_job,
)
from service.constants import MAX_JOBS_STORE
from service.job_store import executor, jobs, jobs_lock
from service.schemas import AnalyzeRequest, AnalyzeResponse, JobStatusResponse, JobSubmitResponse
from service.server_logging import log_message

router = APIRouter(tags=["jobs"])


@router.post("/api/jobs", response_model=JobSubmitResponse)
def submit_job(payload: AnalyzeRequest) -> JobSubmitResponse:
    """Submit an analysis job. Returns immediately with a job_id to poll."""
    payload = normalize_analyze_request(payload)
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
    with jobs_lock:
        jobs[job_id] = {
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
        if len(jobs) > MAX_JOBS_STORE:
            oldest = sorted(jobs.keys(), key=lambda k: jobs[k]["created_at"])
            for k in oldest[: len(jobs) - MAX_JOBS_STORE]:
                del jobs[k]
    executor.submit(run_analysis_job, job_id, payload)
    log_message(f"[Job {job_id[:8]}] Queued ticker={payload.ticker}")
    return JobSubmitResponse(
        job_id=job_id,
        status="pending",
        message="Job queued. Connect to /ws/job/{job_id} for live results.",
        analysis_date=payload.analysis_date,
    )


@router.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str) -> JobStatusResponse:
    """Poll job status. Status: pending | running | done | failed."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found. The server may have restarted — check the exports list for your PDF.",
        )
    job_data = {k: v for k, v in job.items() if k not in ("event_log", "error_notes", "cancel_event")}
    return JobStatusResponse(job_id=job_id, **job_data)


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    """Signal a running job to stop at the next graph checkpoint."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Job is already {job['status']}")
    job["cancel_event"].set()
    log_message(f"[Job {job_id[:8]}] Cancel requested")
    return {"cancelled": True, "job_id": job_id}


@router.get("/api/jobs")
def list_recent_jobs() -> dict[str, Any]:
    """List the most recent 20 jobs (in-memory only, lost on server restart)."""
    with jobs_lock:
        snapshot = [(jid, dict(j)) for jid, j in jobs.items()]
    snapshot.sort(key=lambda x: x[1]["created_at"], reverse=True)
    return {"jobs": [{"job_id": jid, **job} for jid, job in snapshot[:20]]}


@router.websocket("/ws/job/{job_id}")
async def websocket_job_stream(websocket: WebSocket, job_id: str) -> None:
    """WebSocket stream of live analysis events for a job.
    Uses a persistent event log so reconnects replay all prior events.
    """
    await websocket.accept()
    log_message(f"[WS] Client connected to job {job_id[:8]}")

    with jobs_lock:
        if job_id not in jobs:
            await websocket.send_json({"type": "error", "message": "Job not found"})
            await websocket.close()
            return

    sent_index = 0  # how many events from the log we've sent so far
    try:
        while True:
            # Grab any new events since last send
            with jobs_lock:
                if job_id not in jobs:
                    break
                job = jobs[job_id]
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
        log_message(f"[WS] Client disconnected from job {job_id[:8]}")
    except Exception as exc:
        log_message(f"[WS] Error on job {job_id[:8]}: {exc}")
    finally:
        log_message(f"[WS] Closed connection for job {job_id[:8]}")


@router.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    payload = normalize_analyze_request(payload)
    # Always check DB cache first — same key as the async job path so results
    # are shared across all users, sessions, and entry points.
    cached = cache_lookup(payload, label="sync")
    if cached is not None:
        log_message(f"[sync] Cache hit ticker={payload.ticker} lang={payload.language}")
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
        result = execute_analysis(payload, job_id=None)
        cache_save(payload, result, [], label="sync")
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
        log_message(f"Analyze failed ticker={payload.ticker} error={exc}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc
