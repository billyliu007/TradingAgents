from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import Lock

from service.constants import LOG_BUFFER_MAX

_log_buffer: deque[str] = deque(maxlen=LOG_BUFFER_MAX)
_log_lock = Lock()


def log_message(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] {message}"
    with _log_lock:
        _log_buffer.append(line)


def get_logs(limit: int) -> list[str]:
    with _log_lock:
        if limit <= 0:
            return []
        return list(_log_buffer)[-limit:]


def clear_logs() -> None:
    with _log_lock:
        _log_buffer.clear()
