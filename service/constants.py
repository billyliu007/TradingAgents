from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"

ANALYST_OPTIONS = ["market", "social", "news", "fundamentals"]
LOG_BUFFER_MAX = 1000
MAX_JOBS_STORE = 100
