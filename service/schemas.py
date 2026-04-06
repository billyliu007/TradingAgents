from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from service.constants import ANALYST_OPTIONS

LlmProvider = Literal[
    "openai", "google", "anthropic", "xai", "kimi", "kimi_cn", "openrouter", "ollama"
]


class AdminLoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class ExportZipRequest(BaseModel):
    filenames: list[str] = Field(default_factory=list, max_length=100)


class AnalyzeRequest(BaseModel):
    ticker: str = Field(..., min_length=1, description="Ticker, e.g. NVDA")
    analysis_date: date = Field(
        ...,
        description=(
            "As-of calendar date. If this equals the server's UTC calendar date for 'today', "
            "it is normalized to today's date in America/New_York; otherwise it is treated as an "
            "explicit US Eastern calendar date."
        ),
    )
    pdf_filename_date: date | None = Field(
        default=None,
        description=(
            "Optional calendar date for the exported PDF filename only (typically the user's "
            "local date). When omitted, ``analysis_date`` is used in the filename."
        ),
    )
    selected_analysts: list[Literal["market", "social", "news", "fundamentals"]] = Field(
        default_factory=lambda: ANALYST_OPTIONS.copy()
    )
    # Language code (UI + prompt + PDF + DB cache dimension)
    # - "zh" = Simplified Chinese (default Chinese)
    # - "zh-hant" = Traditional Chinese
    language: Literal["en", "zh", "zh-hant", "es", "ja"] = "en"
    # Below: optional. When omitted, values come from DB app_settings or DEFAULT_CONFIG.
    llm_provider: LlmProvider | None = None
    quick_llm_provider: LlmProvider | None = None
    deep_llm_provider: LlmProvider | None = None
    backend_url: str | None = None
    quick_backend_url: str | None = None
    deep_backend_url: str | None = None
    deep_think_llm: str | None = None
    quick_think_llm: str | None = None
    max_debate_rounds: int | None = None
    max_risk_discuss_rounds: int | None = None
    google_thinking_level: str | None = None
    openai_reasoning_effort: str | None = None
    anthropic_effort: str | None = None
    debug: bool = False
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    xai_api_key: str | None = None
    openrouter_api_key: str | None = None
    moonshot_api_key: str | None = None
    kimi_quick_model_custom: str | None = None
    kimi_deep_model_custom: str | None = None


class AnalyzeResponse(BaseModel):
    decision: str
    final_trade_decision: str
    human_readable_report: str
    sections: dict[str, str]
    raw_state: dict[str, Any]
    pdf_filenames: list[str] | None = None
    pdf_download_urls: list[str] | None = None
    analysis_date: date | None = None


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    message: str
    analysis_date: date


class JobStatusResponse(BaseModel):
    job_id: str
    status: str  # pending | running | done | failed | cancelled
    ticker: str
    created_at: str
    completed_at: str | None = None
    decision: str | None = None
    pdf_filenames: list[str] | None = None
    pdf_download_urls: list[str] | None = None
    sections: dict[str, str] | None = None
    error: str | None = None
