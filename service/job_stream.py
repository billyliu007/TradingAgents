from __future__ import annotations

import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from service.job_store import jobs, jobs_lock
from service.server_logging import log_message


async def stream_job_websocket(websocket: WebSocket, job_id: str) -> None:
    """Replay + stream live events for one job; closes when terminal and log drained."""
    await websocket.accept()
    log_message(f"[WS] Client connected to job {job_id[:8]}")

    with jobs_lock:
        if job_id not in jobs:
            await websocket.send_json({"type": "error", "message": "Job not found"})
            await websocket.close()
            return

    sent_index = 0
    try:
        while True:
            with jobs_lock:
                if job_id not in jobs:
                    break
                job = jobs[job_id]
                new_events = job["event_log"][sent_index:]
                status = job["status"]

            for event in new_events:
                await websocket.send_json(event)
                sent_index += 1

            if status in ("done", "failed", "cancelled") and not new_events:
                break

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
