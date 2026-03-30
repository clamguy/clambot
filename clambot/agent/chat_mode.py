"""Chat mode — pure conversational LLM responses.

When the selector routes to chat mode, this module generates a
conversational response using the LLM.  No tools are passed — if the
user needs tool execution, the selector routes to ``generate_new``
or ``select_existing`` instead.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any

from clambot.providers.base import LLMProvider

logger = logging.getLogger(__name__)


class ChatModeFallbackResponder:
    """Generates conversational responses without tool calling.

    The selector has already decided that the message is conversational
    (greeting, question, small talk).  This responder calls the LLM
    with system prompt + history + user message and returns the text.
    """

    def __init__(
        self,
        provider: LLMProvider,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tool_registry: Any | None = None,
        agent_tools: set[str] | None = None,
    ) -> None:
        self._provider = provider
        self._max_tokens = max_tokens
        self._temperature = temperature
        # tool_registry and agent_tools kept for API compatibility
        # but are no longer used — chat mode is tool-free.

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def respond(
        self,
        message: str,
        history: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
    ) -> str:
        """Generate a conversational response."""
        messages: list[dict[str, Any]] = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if history:
            messages.extend(history)

        messages.append({"role": "user", "content": message})

        try:
            response = await self._provider.acomplete(
                messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )

            return self._unwrap_json_chat(response.content)

        except Exception as exc:
            logger.error("Chat mode response failed: %s", exc)
            return "I'm having trouble responding right now. Please try again."

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _unwrap_json_chat(content: str) -> str:
        """Unwrap JSON-wrapped chat responses.

        Some models (especially coding-oriented ones) wrap plain-text
        chat replies in a JSON object like ``{"response": "..."}``.
        This extracts the inner string when a recognised wrapper key is
        found; otherwise the original content is returned unchanged.
        """
        text = content.strip()
        if not text.startswith("{"):
            return content
        try:
            data = _json.loads(text)
            if isinstance(data, dict):
                for key in ("response", "content", "message", "text", "answer", "reply"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        return val
        except (_json.JSONDecodeError, ValueError):
            pass
        return content
