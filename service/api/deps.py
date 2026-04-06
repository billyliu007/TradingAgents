from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from service.admin_auth import admin_username, require_admin


def _require_admin_user(request: Request) -> str:
    require_admin(request)
    return admin_username()


CurrentAdmin = Annotated[str, Depends(_require_admin_user)]
