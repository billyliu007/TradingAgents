from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from service.constants import STATIC_DIR

router = APIRouter()


@router.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")
