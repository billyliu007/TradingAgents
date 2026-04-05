import os
from typing import Any, Optional

from langchain_openai import ChatOpenAI

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output.

    The Responses API returns content as a list of typed blocks
    (reasoning, text, etc.). This normalizes to string for consistent
    downstream handling.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

# Kwargs forwarded from user config to ChatOpenAI
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort",
    "api_key", "callbacks", "http_client", "http_async_client",
    "extra_body",
)

# DEFAULT_CONFIG sets backend_url to OpenAI; that value must not override
# third-party API bases (Moonshot FAQ: wrong/missing base_url → model_not_found).
_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"


def _normalize_base(url: Optional[str]) -> str:
    if not url:
        return ""
    return url.strip().rstrip("/")


# Provider base URLs and API key env vars
_PROVIDER_CONFIG = {
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    # International vs China keys are not interchangeable (Moonshot FAQ).
    "kimi": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    "kimi_cn": ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
}


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI, Ollama, OpenRouter, xAI, and Kimi (Moonshot) providers.

    For native OpenAI models, uses the Responses API (/v1/responses) which
    supports reasoning_effort with function tools across all model families
    (GPT-4.1, GPT-5). Third-party compatible providers (xAI, OpenRouter,
    Kimi, Ollama) use standard Chat Completions.
    """

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """Return configured ChatOpenAI instance."""
        llm_kwargs = {"model": self.model}

        # Provider-specific base URL and auth (optional backend_url overrides default)
        if self.provider in _PROVIDER_CONFIG:
            default_url, api_key_env = _PROVIDER_CONFIG[self.provider]
            if self.provider in ("kimi", "kimi_cn"):
                env_base = os.environ.get("MOONSHOT_API_BASE", "").strip().rstrip("/")
                if env_base:
                    default_url = env_base
            want = _normalize_base(self.base_url)
            if want and want != _normalize_base(_OPENAI_DEFAULT_BASE):
                llm_kwargs["base_url"] = self.base_url.strip()
            else:
                llm_kwargs["base_url"] = default_url
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if api_key:
                    llm_kwargs["api_key"] = api_key
            else:
                llm_kwargs["api_key"] = "ollama"
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # Forward user-provided kwargs
        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # Native OpenAI: use Responses API for consistent behavior across
        # all model families. Third-party providers use Chat Completions.
        if self.provider == "openai":
            llm_kwargs["use_responses_api"] = True

        # Kimi K2.5+ defaults to thinking on; multi-step tool calls require each
        # assistant turn to include reasoning_content. LangChain does not round-trip
        # that field, so Moonshot returns 400. Disable thinking for K2 models that
        # allow it (see Moonshot "Disable Thinking" / tool-use docs). Skip legacy
        # moonshot-v1-* and dedicated kimi-k2-thinking* models.
        if self.provider in ("kimi", "kimi_cn"):
            # Moonshot often returns 429 engine_overloaded; OpenAI client retries with backoff.
            llm_kwargs.setdefault("max_retries", 8)
            mid = self.model.lower()
            if not mid.startswith("moonshot-v1") and "kimi-k2-thinking" not in mid:
                extra = dict(llm_kwargs.get("extra_body") or {})
                extra.setdefault("thinking", {"type": "disabled"})
                llm_kwargs["extra_body"] = extra

        return NormalizedChatOpenAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)
