from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse

from service.app_config import is_ephemeral_deploy
from service.constants import STATIC_DIR

router = APIRouter()


@router.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/admin")
def admin_page() -> FileResponse | RedirectResponse:
    if is_ephemeral_deploy():
        return RedirectResponse(url="/?settings=1", status_code=307)
    return FileResponse(STATIC_DIR / "admin.html")
