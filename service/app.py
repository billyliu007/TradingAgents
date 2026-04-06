"""TradingAgents FastAPI application.

``uvicorn service.app:app`` and ``tradingagents-service`` resolve the app via this module.
Implementation lives in :mod:`service.main`.
"""

from service.main import app, run

__all__ = ["app", "run"]

if __name__ == "__main__":
    run()
