from __future__ import annotations

from fastapi import FastAPI

from service.api.routes import admin, exports, jobs, meta, pages


def register_routes(app: FastAPI) -> None:
    app.include_router(pages.router)
    app.include_router(admin.router)
    app.include_router(meta.router)
    app.include_router(exports.router)
    app.include_router(jobs.rest_router)
    app.include_router(jobs.ws_router)
