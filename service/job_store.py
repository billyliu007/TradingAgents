from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Event as ThreadEvent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

executor = ThreadPoolExecutor(max_workers=3)
jobs: dict[str, dict] = {}
jobs_lock = Lock()
job_websockets: dict[str, list[WebSocket]] = {}
ws_lock = Lock()
