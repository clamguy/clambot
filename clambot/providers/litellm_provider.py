"""LiteLLM provider — wraps ``litellm.acompletion()`` for multi-provider support.

All standard LLM calls in ClamBot flow through this provider.  The model
string is passed directly to LiteLLM, which handles prefix-based routing
(``anthropic/``, ``openrouter/``, ``ollama/``, etc.) internally.
"""

from __future__ import annotations

import json as _json
import logging
from pathlib import Path
from typing import Any

import litellm

from clambot.providers.base import LLMResponse

logger = logging.getLogger(__name__)

# Module-level cache: models that require think=False.
# Populated from config on first load + at runtime on detection.
_THINK_DISABLED_MODELS: set[str] = set()


def load_think_disabled_models(config_path: Path | None = None) -> None:
    """Pre-populate the think-disabled cache from config.json."""
    if config_path is None:
        config_path = Path("~/.clambot/config.json").expanduser()
    try:
        if config_path.exists():
            data = _json.loads(config_path.read_text(encoding="utf-8"))
            models = data.get("providers", {}).get("thinkDisabledModels", [])
            _THINK_DISABLED_MODELS.update(models)
            if models:
                logger.debug("Loaded think-disabled models: %s", models)
    except Exception:
        pass  # best-effort


def _persist_think_disabled_model(model: str, config_path: Path | None = None) -> None:
    """Add a model to thinkDisabledModels in config.json."""
    if config_path is None:
        config_path = Path("~/.clambot/config.json").expanduser()
    try:
        data: dict[str, Any] = {}
        if config_path.exists():
            data = _json.loads(config_path.read_text(encoding="utf-8"))
        providers = data.setdefault("providers", {})
        models: list[str] = providers.get("thinkDisabledModels", [])
        if model not in models:
            models.append(model)
            providers["thinkDisabledModels"] = models
            config_path.write_text(
                _json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            logger.info("Cached think=False for model %s in config", model)
    except Exception as exc:
        logger.debug("Failed to persist think-disabled model: %s", exc)


class LiteLLMProvider:
    """LLM provider backed by LiteLLM ``acompletion``.

    Parameters:
        model: Default model identifier (e.g. ``anthropic/claude-sonnet-4-20250514``).
        api_key: Optional API key passed directly to LiteLLM.
        api_base: Optional base URL for custom endpoints.
        extra_headers: Optional headers forwarded on every request.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.extra_headers = extra_headers or {}

        # Suppress LiteLLM debug noise and drop unsupported params.
        litellm.suppress_debug_info = True
        litellm.drop_params = True

    # ------------------------------------------------------------------
    # Public interface (satisfies LLMProvider Protocol)
    # ------------------------------------------------------------------

    async def acomplete(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a chat completion request via ``litellm.acompletion``.

        Recognised ``kwargs``: ``model``, ``max_tokens``, ``temperature``.
        Any remaining kwargs are forwarded to LiteLLM as-is.
        """
        model = kwargs.pop("model", self.model)
        max_tokens = max(1, kwargs.pop("max_tokens", 4096))
        temperature = kwargs.pop("temperature", 0.7)

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if self.api_key:
            call_kwargs["api_key"] = self.api_key
        if self.api_base:
            call_kwargs["api_base"] = self.api_base
        if self.extra_headers:
            call_kwargs["extra_headers"] = self.extra_headers

        # Forward any remaining caller kwargs (e.g. tools, tool_choice).
        call_kwargs.update(kwargs)

        # Apply cached think=False for known thinking models.
        if model in _THINK_DISABLED_MODELS and "think" not in call_kwargs:
            call_kwargs["think"] = False

        try:
            response = await litellm.acompletion(**call_kwargs)
            result = self._parse_response(response)

            # Thinking-model detection: if content is empty but tokens
            # were spent, the model burned them on <think> blocks.
            # Retry with think=False and cache for future calls.
            if (
                not result.content.strip()
                and not result.tool_calls
                and result.usage
                and result.usage.get("completion_tokens", 0) > 10
                and "think" not in kwargs  # caller didn't set it
                and model not in _THINK_DISABLED_MODELS
            ):
                logger.info("Empty content from thinking model %s — retrying with think=False", model)
                call_kwargs["think"] = False
                response = await litellm.acompletion(**call_kwargs)
                result = self._parse_response(response)

                # Cache so we skip the retry on every future call.
                _THINK_DISABLED_MODELS.add(model)
                _persist_think_disabled_model(model)

            return result
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(response: Any) -> LLMResponse:
        """Extract content, usage, and tool_calls from a LiteLLM response.

        Thinking models (Qwen, DeepSeek-R1, etc.) may put all useful text
        in ``reasoning_content`` while leaving ``content`` empty.  When that
        happens we fall back to ``reasoning_content``.
        """
        choice = response.choices[0]
        msg = choice.message
        content = msg.content or ""

        # Fallback: thinking models may return empty content with all
        # substance in reasoning_content.
        if not content.strip():
            reasoning = getattr(msg, "reasoning_content", None) or ""
            if reasoning:
                content = reasoning

        usage: dict[str, int] | None = None
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        tool_calls: list[dict[str, Any]] | None = None
        if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in choice.message.tool_calls
            ]

        return LLMResponse(content=content, usage=usage, tool_calls=tool_calls)
