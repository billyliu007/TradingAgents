"""Deploy mode, config merge, and ephemeral LLM key validation."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException

from service.app_config import clear_deploy_mode_cache, get_deploy_mode
from service.llm_config_validate import assert_ephemeral_llm_keys
from service.schemas import AnalyzeRequest
from service.settings_ops import build_graph_config


def test_get_deploy_mode_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADINGAGENTS_DEPLOY_MODE", raising=False)
    clear_deploy_mode_cache()
    assert get_deploy_mode() == "hosted"


def test_get_deploy_mode_ephemeral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_DEPLOY_MODE", "ephemeral")
    clear_deploy_mode_cache()
    assert get_deploy_mode() == "ephemeral"


def test_build_graph_config_payload_overrides_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "service.settings_ops.db.get_app_settings",
        lambda: {"deep_think_llm": "from-db", "quick_think_llm": "q-db"},
    )
    p = AnalyzeRequest(
        ticker="AAPL",
        analysis_date=date(2025, 1, 1),
        deep_think_llm="from-request",
    )
    cfg = build_graph_config(p)
    assert cfg["deep_think_llm"] == "from-request"
    assert cfg["quick_think_llm"] == "q-db"


def test_ephemeral_llm_keys_raises_when_missing() -> None:
    with pytest.raises(HTTPException) as excinfo:
        assert_ephemeral_llm_keys(
            {
                "llm_provider": "openai",
                "quick_llm_provider": "openai",
                "deep_llm_provider": "openai",
            }
        )
    assert "openai_api_key" in str(excinfo.value.detail).lower()


def test_ephemeral_llm_keys_ok_with_moonshot() -> None:
    assert_ephemeral_llm_keys(
        {
            "llm_provider": "kimi_cn",
            "quick_llm_provider": "kimi_cn",
            "deep_llm_provider": "kimi_cn",
            "moonshot_api_key": "sk-test",
        }
    )


def test_build_graph_config_sets_forbid_in_ephemeral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADINGAGENTS_DEPLOY_MODE", "ephemeral")
    clear_deploy_mode_cache()
    monkeypatch.setattr("service.settings_ops.db.get_app_settings", lambda: {})
    p = AnalyzeRequest(ticker="AAPL", analysis_date=date(2025, 1, 1), moonshot_api_key="x")
    cfg = build_graph_config(p)
    assert cfg.get("forbid_llm_env_keys") is True
    monkeypatch.delenv("TRADINGAGENTS_DEPLOY_MODE", raising=False)
    clear_deploy_mode_cache()
