"""Tests for LLM fake-tool artifact stripping."""

from service.content_sanitize import (
    sanitize_event_payload,
    sanitize_log_event,
    strip_llm_fake_tool_artifacts,
)


def test_strip_tool_and_query_blocks() -> None:
    raw = """我需要调用工具来获取TSLA的实时数据。

<tool>web_search</tool>
<query>TSLA Tesla stock price</query>

<tool name="x">web_search</tool>
<query>
multi
line
</query>

结论：持有。
"""
    out = strip_llm_fake_tool_artifacts(raw)
    assert "<tool" not in out.lower()
    assert "<query" not in out.lower()
    assert "结论" in out
    assert "持有" in out


def test_strip_orphan_single_line_tags() -> None:
    raw = "Intro\n  <tool>web_search</tool>  \nRest"
    out = strip_llm_fake_tool_artifacts(raw)
    assert "web_search" not in out
    assert "Intro" in out
    assert "Rest" in out


def test_sanitize_event_payload_debate() -> None:
    data = {"role": "bull", "content": "See <tool>t</tool> ok"}
    out = sanitize_event_payload("debate_message", data)
    assert out["content"] == "See  ok"


def test_sanitize_event_payload_unchanged() -> None:
    data = {"preview": "<tool>x</tool>"}
    out = sanitize_event_payload("data_fetched", data)
    assert out is data


def test_sanitize_log_event() -> None:
    ev = {"type": "analyst_complete", "content": "A <query>q</query> B", "timestamp": "t"}
    out = sanitize_log_event(ev)
    assert out["content"] == "A  B"
    assert out["timestamp"] == "t"
