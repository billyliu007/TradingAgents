import inspect
import re
from typing import Any, Sequence

from langchain_core.messages import HumanMessage, RemoveMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news,
    get_sentiment_news,
)


def bind_llm_tools(llm: Any, tools: Sequence, **kwargs: Any) -> Any:
    """Bind tools for agent chains.

    Uses ``tool_choice="auto"`` when the model's ``bind_tools`` supports it,
    so OpenAI-compatible APIs (incl. Kimi) avoid unsupported ``required`` modes.
    Other providers only receive kwargs they accept.
    """
    if "tool_choice" in inspect.signature(llm.bind_tools).parameters:
        kwargs.setdefault("tool_choice", "auto")
    return llm.bind_tools(tools, **kwargs)


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`)."
    )


def sanitize_agent_output_text(text: str) -> str:
    """Drop synthetic ``<tool>...</tool>`` blocks some models emit in plain-text mode."""
    if not text or not isinstance(text, str):
        return text if isinstance(text, str) else ""
    out = re.sub(r"<tool\b[^>]*>[\s\S]*?</tool\s*>", "", text, flags=re.IGNORECASE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
