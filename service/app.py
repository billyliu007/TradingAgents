from __future__ import annotations

import os
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from threading import Lock
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

load_dotenv()

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"

ANALYST_OPTIONS = ["market", "social", "news", "fundamentals"]
LOG_BUFFER_MAX = 1000
_log_buffer: deque[str] = deque(maxlen=LOG_BUFFER_MAX)
_log_lock = Lock()


def _log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] {message}"
    with _log_lock:
        _log_buffer.append(line)


def _get_logs(limit: int) -> list[str]:
    with _log_lock:
        if limit <= 0:
            return []
        return list(_log_buffer)[-limit:]


class AnalyzeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, description="Ticker, e.g. NVDA")
    analysis_date: date
    selected_analysts: list[Literal["market", "social", "news", "fundamentals"]] = Field(
        default_factory=lambda: ANALYST_OPTIONS.copy()
    )
    llm_provider: Literal["openai", "google", "anthropic", "xai", "openrouter", "ollama"] = "openai"
    backend_url: str | None = None
    deep_think_llm: str = "gpt-5.2"
    quick_think_llm: str = "gpt-5-mini"
    max_debate_rounds: int = Field(default=1, ge=1, le=5)
    max_risk_discuss_rounds: int = Field(default=1, ge=1, le=5)
    google_thinking_level: str | None = None
    openai_reasoning_effort: str | None = None
    anthropic_effort: str | None = None
    debug: bool = False


class AnalyzeResponse(BaseModel):
    decision: str
    final_trade_decision: str
    human_readable_report: str
    sections: dict[str, str]
    raw_state: dict[str, Any]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _build_report(state: dict[str, Any]) -> tuple[str, dict[str, str]]:
    sections: dict[str, str] = {}

    mapping = {
        "Market Analysis": state.get("market_report"),
        "Social Sentiment Analysis": state.get("sentiment_report"),
        "News Analysis": state.get("news_report"),
        "Fundamentals Analysis": state.get("fundamentals_report"),
        "Research Team Decision": state.get("investment_plan"),
        "Trader Plan": state.get("trader_investment_plan"),
        "Final Portfolio Decision": state.get("final_trade_decision"),
    }

    for title, content in mapping.items():
        text = _as_text(content)
        if text:
            sections[title] = text

    lines: list[str] = []
    for title, content in sections.items():
        lines.append(f"## {title}\n\n{content}")

    combined = "\n\n".join(lines) if lines else "No report content was generated."
    return combined, sections


app = FastAPI(
    title="TradingAgents API",
    description="HTTP wrapper for TradingAgents multi-agent analysis",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/options")
def options() -> dict[str, Any]:
    return {
        "analysts": ANALYST_OPTIONS,
        "llm_provider_default": DEFAULT_CONFIG["llm_provider"],
        "deep_think_default": DEFAULT_CONFIG["deep_think_llm"],
        "quick_think_default": DEFAULT_CONFIG["quick_think_llm"],
        "max_debate_rounds_default": DEFAULT_CONFIG["max_debate_rounds"],
        "max_risk_discuss_rounds_default": DEFAULT_CONFIG["max_risk_discuss_rounds"],
        "backend_url_default": DEFAULT_CONFIG["backend_url"],
    }


@app.get("/api/logs")
def logs(limit: int = 200) -> dict[str, Any]:
    capped_limit = max(1, min(limit, 1000))
    return {"logs": _get_logs(capped_limit)}


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(payload: AnalyzeRequest) -> AnalyzeResponse:
    try:
        _log(
            "Analyze request received "
            f"ticker={payload.ticker} date={payload.analysis_date.isoformat()} "
            f"provider={payload.llm_provider} analysts={','.join(payload.selected_analysts)} "
            f"debate_rounds={payload.max_debate_rounds} risk_rounds={payload.max_risk_discuss_rounds}"
        )
        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = payload.llm_provider
        config["deep_think_llm"] = payload.deep_think_llm
        config["quick_think_llm"] = payload.quick_think_llm
        config["max_debate_rounds"] = payload.max_debate_rounds
        config["max_risk_discuss_rounds"] = payload.max_risk_discuss_rounds
        config["google_thinking_level"] = payload.google_thinking_level
        config["openai_reasoning_effort"] = payload.openai_reasoning_effort
        config["anthropic_effort"] = payload.anthropic_effort

        if payload.backend_url:
            config["backend_url"] = payload.backend_url

        graph = TradingAgentsGraph(
            selected_analysts=payload.selected_analysts,
            debug=payload.debug,
            config=config,
            progress_callback=lambda msg: _log(f"Progress: {msg}"),
        )
        final_state, decision = graph.propagate(payload.ticker, payload.analysis_date.isoformat())
        human_report, sections = _build_report(final_state)
        _log(
            "Analyze completed "
            f"ticker={payload.ticker} decision={_as_text(decision) or 'N/A'} "
            f"final_trade_decision={_as_text(final_state.get('final_trade_decision')) or 'N/A'}"
        )

        return AnalyzeResponse(
            decision=_as_text(decision),
            final_trade_decision=_as_text(final_state.get("final_trade_decision")),
            human_readable_report=human_report,
            sections=sections,
            raw_state=final_state,
        )
    except Exception as exc:
        _log(f"Analyze failed ticker={payload.ticker} error={exc}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc


def run() -> None:
    import uvicorn

    host = os.getenv("TRADINGAGENTS_SERVICE_HOST", "0.0.0.0")
    port = int(os.getenv("TRADINGAGENTS_SERVICE_PORT", "8000"))
    uvicorn.run("service.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()

