from __future__ import annotations

import io
import zipfile
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from tradingagents.default_config import DEFAULT_CONFIG

from service import db
from service import tickers as _ticker_svc
from service.constants import ANALYST_OPTIONS
from service.export_paths import exports_dir, paths_for_export_filenames, resolve_export_file
from service.schemas import ExportZipRequest
from service.server_logging import clear_logs, get_logs
from service.settings_ops import apply_kimi_custom_models, apply_map_from_stored, coerce_round_int

router = APIRouter(tags=["api"])


@router.get("/api/options")
def options() -> dict[str, Any]:
    stored = db.get_app_settings()
    cfg = DEFAULT_CONFIG.copy()
    if stored:
        apply_map_from_stored(cfg, stored)
    apply_kimi_custom_models(cfg)
    qp = cfg.get("quick_llm_provider") or cfg.get("llm_provider")
    dp = cfg.get("deep_llm_provider") or cfg.get("llm_provider")
    return {
        "analysts": ANALYST_OPTIONS,
        "llm_provider_default": cfg.get("llm_provider"),
        "quick_llm_provider_default": qp,
        "deep_llm_provider_default": dp,
        "deep_think_default": cfg.get("deep_think_llm"),
        "quick_think_default": cfg.get("quick_think_llm"),
        "max_debate_rounds_default": coerce_round_int(
            cfg.get("max_debate_rounds"), int(DEFAULT_CONFIG["max_debate_rounds"])
        ),
        "max_risk_discuss_rounds_default": coerce_round_int(
            cfg.get("max_risk_discuss_rounds"),
            int(DEFAULT_CONFIG["max_risk_discuss_rounds"]),
        ),
        "backend_url_default": cfg.get("backend_url") or "",
        "global_settings_from_db": bool(stored),
    }


@router.get("/api/tickers/search")
def ticker_search(q: str = "", limit: int = 10) -> dict[str, Any]:
    """Autocomplete search over the in-memory US stock ticker index."""
    q = q.strip()
    if not q:
        return {"results": [], "loaded": _ticker_svc.is_loaded()}
    capped = max(1, min(limit, 20))
    return {
        "results": _ticker_svc.search(q, capped),
        "loaded": _ticker_svc.is_loaded(),
    }


@router.get("/api/logs")
def logs(limit: int = 200) -> dict[str, Any]:
    capped_limit = max(1, min(limit, 1000))
    return {"logs": get_logs(capped_limit)}


@router.delete("/api/logs")
def clear_logs_endpoint() -> dict[str, bool]:
    clear_logs()
    return {"cleared": True}


@router.get("/api/exports")
def list_exports() -> dict[str, Any]:
    d = exports_dir()
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


@router.get("/api/exports/download/{filename}")
def download_export(filename: str) -> FileResponse:
    path = resolve_export_file(filename)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="attachment",
    )


@router.post("/api/exports/zip")
def download_selected_exports_zip(body: ExportZipRequest) -> StreamingResponse:
    if not body.filenames:
        raise HTTPException(status_code=400, detail="No filenames provided")
    paths = paths_for_export_filenames(body.filenames)
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


@router.get("/api/exports/all.zip")
def download_all_exports_zip() -> StreamingResponse:
    exports = exports_dir()
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
