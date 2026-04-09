from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from service import db
from service import tickers as _ticker_svc
from service.api import register_routes
from service.app_config import get_deploy_mode
from service.constants import STATIC_DIR
from service.server_logging import log_message

load_dotenv()


def create_app() -> FastAPI:
    app = FastAPI(
        title="TradingAgents API",
        description="HTTP wrapper for TradingAgents multi-agent analysis",
        version="0.1.0",
    )

    _cors_raw = os.getenv("TRADINGAGENTS_CORS_ORIGINS", "*").strip()
    if _cors_raw == "*":
        _cors_origins: list[str] = ["*"]
    else:
        _cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def _startup() -> None:
        log_message(f"Deploy mode: {get_deploy_mode()}")
        try:
            db.init_db()
            log_message("DB cache initialised")
        except Exception as exc:
            log_message(f"DB cache init failed (caching disabled): {exc}")

        # Load ticker list in a background thread so startup is not blocked.
        import threading

        def _load_tickers() -> None:
            try:
                _ticker_svc.load()
                log_message(f"Ticker index ready: {_ticker_svc.count()} symbols")
            except Exception as exc:
                log_message(f"Ticker load failed (non-fatal): {exc}")

        threading.Thread(target=_load_tickers, daemon=True).start()

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    register_routes(app)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    host = os.getenv("TRADINGAGENTS_SERVICE_HOST", "0.0.0.0")
    port = int(os.getenv("TRADINGAGENTS_SERVICE_PORT", "8000"))
    uvicorn.run("service.main:app", host=host, port=port, reload=False)
