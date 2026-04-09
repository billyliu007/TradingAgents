from __future__ import annotations

import uuid
from datetime import datetime, timezone
from threading import Event as ThreadEvent
from typing import Any

from fastapi import HTTPException

from service import tickers as _ticker_svc
from service.analysis import normalize_analyze_request, run_analysis_job
from service.app_config import is_ephemeral_deploy
from service.constants import MAX_JOBS_STORE
from service.llm_config_validate import assert_ephemeral_llm_keys
from service.settings_ops import build_graph_config
from service.job_store import executor, jobs, jobs_lock
from service.schemas import AnalyzeRequest, JobStatusResponse, JobSubmitResponse
from service.server_logging import log_message


def submit_job(payload: AnalyzeRequest) -> JobSubmitResponse:
    payload = normalize_analyze_request(payload)
    if is_ephemeral_deploy():
        assert_ephemeral_llm_keys(build_graph_config(payload))
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
            "event_log": [],
            "error_notes": [],
            "cancel_event": ThreadEvent(),
        }
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


def job_status(job_id: str) -> JobStatusResponse:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail="Job not found. The server may have restarted — check the exports list for your PDF.",
        )
    job_data = {
        k: v
        for k, v in job.items()
        if k not in ("event_log", "error_notes", "cancel_event", "ephemeral_pdf")
    }
    return JobStatusResponse(job_id=job_id, **job_data)


def cancel_job(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Job is already {job['status']}")
    job["cancel_event"].set()
    log_message(f"[Job {job_id[:8]}] Cancel requested")
    return {"cancelled": True, "job_id": job_id}


def list_recent_jobs() -> dict[str, Any]:
    with jobs_lock:
        snapshot = [(jid, dict(j)) for jid, j in jobs.items()]
    snapshot.sort(key=lambda x: x[1]["created_at"], reverse=True)
    scrub = ("event_log", "error_notes", "cancel_event", "ephemeral_pdf")
    return {
        "jobs": [
            {"job_id": jid, **{k: v for k, v in job.items() if k not in scrub}}
            for jid, job in snapshot[:20]
        ]
    }
