"""Base LLM provider types.

Defines the LLMResponse frozen dataclass and LLMProvider Protocol that all
provider implementations must satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMResponse:
    """Immutable response from an LLM provider.

    Attributes:
        content: The text content of the LLM response.
        usage: Optional token-usage dict with keys like
               ``prompt_tokens``, ``completion_tokens``, ``total_tokens``.
        tool_calls: Optional list of tool-call dicts when the model
                    uses function calling.  Each dict has ``id`` and
                    ``function`` (with ``name`` and ``arguments``).
    """

    content: str
    usage: dict[str, int] | None = None
    tool_calls: list[dict[str, Any]] | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol for LLM providers.

    Any class that exposes an ``acomplete`` async method with the right
    signature satisfies this protocol — no subclassing required.
    """

    async def acomplete(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a chat completion request and return an ``LLMResponse``.

        Args:
            messages: List of message dicts with ``role`` and ``content``.
            **kwargs: Provider-agnostic overrides such as ``max_tokens``,
                      ``temperature``, ``model``, etc.
        """
        ...
