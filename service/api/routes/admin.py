from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Response

from service import db
from service.admin_auth import (
    admin_cookie_max_age,
    admin_cookie_name,
    admin_issue_token,
    admin_password_configured,
    admin_username,
    admin_verify_credentials,
    admin_verify_token,
)
from service.api.deps import CurrentAdmin
from service.schemas import AdminLoginRequest
from service.settings_ops import admin_sanitize_put_body, admin_settings_get_payload

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/login")
def admin_login(body: AdminLoginRequest, response: Response) -> dict[str, bool]:
    if not admin_password_configured():
        raise HTTPException(
            status_code=503,
            detail="Admin login is not configured (set TRADINGAGENTS_ADMIN_PASSWORD in .env).",
        )
    if not admin_verify_credentials(body.username.strip(), body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = admin_issue_token()
    if not token:
        raise HTTPException(status_code=503, detail="Could not issue session (check admin env vars).")
    response.set_cookie(
        key=admin_cookie_name(),
        value=token,
        httponly=True,
        samesite="lax",
        max_age=admin_cookie_max_age(),
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
def admin_logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(admin_cookie_name(), path="/")
    return {"ok": True}


@router.get("/me")
def admin_me(request: Request) -> dict[str, Any]:
    tok = request.cookies.get(admin_cookie_name())
    if admin_verify_token(tok):
        return {"ok": True, "user": admin_username()}
    raise HTTPException(status_code=401, detail="Not authenticated")


@router.get("/settings")
def admin_settings_get(_admin: CurrentAdmin) -> dict[str, Any]:
    return admin_settings_get_payload()


@router.put("/settings")
def admin_settings_put(_admin: CurrentAdmin, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    merged = admin_sanitize_put_body(body)
    if not db.save_app_settings(merged):
        raise HTTPException(
            status_code=503,
            detail="Could not save settings. Set DATABASE_URL and ensure psycopg2 is installed.",
        )
    return {"ok": True}
