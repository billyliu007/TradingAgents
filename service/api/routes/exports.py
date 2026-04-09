from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from service.api.handlers.exports_zip import pdf_zip_response
from service.app_config import is_ephemeral_deploy
from service.export_paths import exports_dir, paths_for_export_filenames, resolve_export_file
from service.schemas import ExportZipRequest

router = APIRouter(prefix="/api/exports", tags=["exports"])


def _exports_unavailable() -> None:
    if is_ephemeral_deploy():
        raise HTTPException(status_code=404, detail="Exports are not available on this deployment")


@router.get("")
def list_exports() -> dict[str, Any]:
    _exports_unavailable()
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


@router.get("/download/{filename}")
def download_export(filename: str) -> FileResponse:
    _exports_unavailable()
    path = resolve_export_file(filename)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="attachment",
    )


@router.post("/zip")
def download_selected_exports_zip(body: ExportZipRequest):
    _exports_unavailable()
    if not body.filenames:
        raise HTTPException(status_code=400, detail="No filenames provided")
    paths = paths_for_export_filenames(body.filenames)
    if not paths:
        raise HTTPException(status_code=404, detail="No matching PDF files")
    return pdf_zip_response(paths, "tradingagents-selected.zip")


@router.get("/all.zip")
def download_all_exports_zip():
    _exports_unavailable()
    exports = exports_dir()
    pdfs = sorted(exports.glob("*.pdf")) if exports.is_dir() else []
    if not pdfs:
        raise HTTPException(status_code=404, detail="No PDF exports available")
    return pdf_zip_response(pdfs, "tradingagents-exports.zip")
