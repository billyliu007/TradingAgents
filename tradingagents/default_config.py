import os

DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", "./results"),
    "data_cache_dir": os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
        "dataflows/data_cache",
    ),
    # LLM settings (default OpenAI — set OPENAI_API_KEY or store keys in app_settings)
    "llm_provider": "openai",
    # When None, quick/deep use llm_provider. Set to mix vendors (e.g. OpenAI quick + Anthropic deep).
    "quick_llm_provider": None,
    "deep_llm_provider": None,
    "deep_think_llm": "gpt-5.2",
    "quick_think_llm": "gpt-5-mini",
    "backend_url": "https://api.openai.com/v1",
    # Optional per-track API base; when None, backend_url is used.
    "quick_backend_url": None,
    "deep_backend_url": None,
    # Language settings
    "language": "en",  # "en" | "zh" (简体) | "zh-hant" (繁體) | "es" | "ja"
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # API keys (optional, override .env if provided)
    "openai_api_key": None,
    "anthropic_api_key": None,
    "google_api_key": None,
    "xai_api_key": None,
    "openrouter_api_key": None,
    "moonshot_api_key": None,
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
}
