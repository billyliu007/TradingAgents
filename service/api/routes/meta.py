from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from service import tickers as _ticker_svc
from service.server_logging import clear_logs, get_logs
from service.settings_ops import ui_options_response

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/options")
def options() -> dict[str, Any]:
    return ui_options_response()


@router.get("/tickers/search")
def ticker_search(q: str = "", limit: int = 10) -> dict[str, Any]:
    q = q.strip()
    if not q:
        return {"results": [], "loaded": _ticker_svc.is_loaded()}
    capped = max(1, min(limit, 20))
    return {
        "results": _ticker_svc.search(q, capped),
        "loaded": _ticker_svc.is_loaded(),
    }


@router.get("/logs")
def logs(limit: int = 200) -> dict[str, Any]:
    capped_limit = max(1, min(limit, 1000))
    return {"logs": get_logs(capped_limit)}


@router.delete("/logs")
def clear_logs_endpoint() -> dict[str, bool]:
    clear_logs()
    return {"cleared": True}
