from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock

executor = ThreadPoolExecutor(max_workers=3)
jobs: dict[str, dict] = {}
jobs_lock = Lock()
