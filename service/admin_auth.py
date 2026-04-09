from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

from fastapi import HTTPException, Request

_ADMIN_COOKIE = "ta_admin_session"
_ADMIN_COOKIE_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def admin_username() -> str:
    return (os.getenv("TRADINGAGENTS_ADMIN_USER") or "billyliu").strip()


def admin_password_configured() -> bool:
    return bool((os.getenv("TRADINGAGENTS_ADMIN_PASSWORD") or "").strip())


def _admin_signing_secret() -> bytes:
    p = (os.getenv("TRADINGAGENTS_ADMIN_PASSWORD") or "").strip()
    return p.encode("utf-8") if p else b""


def admin_verify_credentials(username: str, password: str) -> bool:
    if not admin_password_configured():
        return False
    if username != admin_username():
        return False
    expected = (os.getenv("TRADINGAGENTS_ADMIN_PASSWORD") or "").strip().encode("utf-8")
    return hmac.compare_digest(password.encode("utf-8"), expected)


def admin_issue_token() -> str:
    secret = _admin_signing_secret()
    if not secret:
        return ""
    user = admin_username()
    exp = int(time.time()) + _ADMIN_COOKIE_MAX_AGE
    payload = f"{user}|{exp}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode("utf-8")).decode("ascii")


def admin_verify_token(raw: str | None) -> bool:
    if not raw:
        return False
    secret = _admin_signing_secret()
    if not secret:
        return False
    try:
        inner = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        user, exp_s, sig = inner.rsplit("|", 2)
        if user != admin_username():
            return False
        if int(time.time()) > int(exp_s):
            return False
        payload = f"{user}|{exp_s}"
        expect = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expect, sig)
    except (ValueError, OSError, UnicodeDecodeError):
        return False


def require_admin(request: Request) -> None:
    if not admin_verify_token(request.cookies.get(_ADMIN_COOKIE)):
        raise HTTPException(status_code=401, detail="Not authenticated")


def admin_cookie_name() -> str:
    return _ADMIN_COOKIE


def admin_cookie_max_age() -> int:
    return _ADMIN_COOKIE_MAX_AGE
