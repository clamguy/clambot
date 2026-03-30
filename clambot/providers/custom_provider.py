"""Custom provider — wraps LiteLLMProvider with ``openai/`` prefix.

Used for direct OpenAI-compatible endpoints (e.g. a local server or third-
party gateway).  The model string is prefixed with ``openai/`` so that
LiteLLM routes the call through its OpenAI-compatible code path.
"""

from __future__ import annotations

from typing import Any

from clambot.providers.base import LLMResponse
from clambot.providers.litellm_provider import LiteLLMProvider


class CustomProvider:
    """Provider for OpenAI-compatible endpoints via LiteLLM's ``openai/`` prefix.

    Delegates entirely to :class:`LiteLLMProvider` after ensuring the model
    carries the ``openai/`` prefix that LiteLLM expects.

    Parameters:
        model: Model identifier — ``openai/`` prefix is added if absent.
        api_key: Optional API key.
        api_base: Base URL for the OpenAI-compatible endpoint.
        extra_headers: Optional extra HTTP headers.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        # Ensure the model has the openai/ prefix for LiteLLM routing.
        prefixed = model if model.startswith("openai/") else f"openai/{model}"
        self._provider = LiteLLMProvider(
            model=prefixed,
            api_key=api_key,
            api_base=api_base,
            extra_headers=extra_headers,
            **kwargs,
        )

    async def acomplete(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        """Delegate to the underlying :class:`LiteLLMProvider`."""
        return await self._provider.acomplete(messages, **kwargs)
