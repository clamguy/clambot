"""Provider-backed clam generator — generates JavaScript clams via LLM.

Uses the primary LLM model to generate clam code (JavaScript) based on
the user's request, conversation history, and system prompt with
generation rules.
"""

from __future__ import annotations

import logging
from typing import Any

from clambot.providers.base import LLMProvider

from .generation_adapter import GenerationResult, normalize_generation_response

logger = logging.getLogger(__name__)


class ProviderBackedClamGenerator:
    """Generates JavaScript clam code via LLM.

    Uses the system prompt (which includes generation rules, tool schemas,
    and clam catalog) to instruct the LLM to produce valid JavaScript code
    that can run in the amla-sandbox WASM environment.
    """

    def __init__(
        self,
        provider: LLMProvider,
        max_tokens: int = 8192,
        temperature: float = 0.7,
    ) -> None:
        self._provider = provider
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def generate(
        self,
        message: str,
        history: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        link_context: str = "",
        self_fix_context: str = "",
    ) -> GenerationResult:
        """Generate a JavaScript clam for the given user request.

        Args:
            message: The user's request.
            history: Conversation history (LLM format).
            system_prompt: System prompt with generation rules.
            link_context: Pre-fetched link context.
            self_fix_context: Error context for self-fix attempts.

        Returns:
            A GenerationResult with the generated script and metadata.
        """
        messages = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Add history for context
        if history:
            messages.extend(history)

        # Build the user message
        user_content = message
        if link_context:
            user_content += f"\n\n--- LINK CONTEXT ---\n{link_context}"
        if self_fix_context:
            user_content = (
                "SELF_FIX_RUNTIME:\n"
                f"{self_fix_context}\n\n"
                f"Original request: {message}\n\n"
                "Regenerate the clam as VALID JAVASCRIPT CODE. "
                "Return ONLY a JSON object with keys: script, declared_tools, inputs, metadata. "
                "Do NOT return prose, summaries, or a direct user-facing answer."
            )

        messages.append({"role": "user", "content": user_content})

        try:
            response = await self._provider.acomplete(
                messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )

            result = normalize_generation_response(response.content)

            # Ensure language is javascript
            result.language = "javascript"

            return result

        except Exception as exc:
            logger.error("Generation failed: %s", exc)
            return GenerationResult(
                script="",
                metadata={"error": str(exc)},
            )
