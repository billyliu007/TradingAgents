from __future__ import annotations

from fastapi import APIRouter, HTTPException, WebSocket
from fastapi.responses import Response

from service.api.handlers.analyze import sync_analyze
from service.app_config import is_ephemeral_deploy
from service.job_store import jobs, jobs_lock
from service.job_stream import stream_job_websocket
from service.jobs_service import cancel_job, job_status, list_recent_jobs, submit_job
from service.schemas import AnalyzeRequest, AnalyzeResponse, JobStatusResponse, JobSubmitResponse

rest_router = APIRouter(prefix="/api", tags=["jobs"])
ws_router = APIRouter(tags=["jobs"])


@rest_router.post("/jobs", response_model=JobSubmitResponse)
def post_job(payload: AnalyzeRequest) -> JobSubmitResponse:
    return submit_job(payload)


@rest_router.get("/jobs/{job_id}/pdf")
def download_job_pdf(job_id: str) -> Response:
    """Ephemeral deploy: one-time in-memory PDF for a completed job."""
    if not is_ephemeral_deploy():
        raise HTTPException(status_code=404, detail="Not found")
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        data = job.get("ephemeral_pdf")
        fname = job.get("ephemeral_pdf_filename") or "report.pdf"
        if not data:
            raise HTTPException(status_code=404, detail="PDF not available for this job")
        job["ephemeral_pdf"] = None
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@rest_router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str) -> JobStatusResponse:
    return job_status(job_id)


@rest_router.post("/jobs/{job_id}/cancel")
def post_cancel(job_id: str):
    return cancel_job(job_id)


@rest_router.get("/jobs")
def get_jobs():
    return list_recent_jobs()


@rest_router.post("/analyze", response_model=AnalyzeResponse)
def post_analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    return sync_analyze(payload)


@ws_router.websocket("/ws/job/{job_id}")
async def job_websocket(websocket: WebSocket, job_id: str) -> None:
    await stream_job_websocket(websocket, job_id)
