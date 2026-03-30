"""Durable facts extraction — extracts persistent facts from conversation turns.

Uses the LLM to identify facts worth remembering long-term from a
conversation turn (e.g., user preferences, project details, decisions).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from clambot.utils.text import strip_markdown_fences

logger = logging.getLogger(__name__)


@dataclass
class DurableFacts:
    """Facts extracted from a conversation turn."""

    facts: list[str]
    session_key: str = ""


EXTRACTION_PROMPT = """\
Extract any NEW durable facts about the USER from this conversation that should be \
remembered permanently across all future sessions.

INCLUDE (truly durable — unlikely to change):
- User's name, nickname, preferred name for the assistant
- User's location, timezone, language preferences
- Long-term interests, hobbies, profession, expertise
- Communication style preferences
- Important personal details (birthday, etc.)

EXCLUDE (transient — do NOT save):
- Data values, numbers, balances, prices, statistics
- Task results, API responses, computation outputs
- Operational state (scheduled jobs, reminders, cron settings)
- System configuration (API keys, secrets, tool settings)
- Greetings, acknowledgments, small talk
- Anything about what the assistant did or needs to do

{existing_memory}

Return a JSON object:
{{"facts": ["fact 1", "fact 2"]}}

If there are no NEW durable facts about the user, return:
{{"facts": []}}
"""


async def extract_durable_facts_for_turn(
    turn: dict[str, Any],
    session_key: str,
    provider: Any,
    existing_memory: str = "",
) -> DurableFacts | None:
    """Extract durable facts from a conversation turn.

    Args:
        turn: Dict with ``role`` and ``content``.
        session_key: The session key for context.
        provider: LLM provider for extraction.
        existing_memory: Current MEMORY.md content for dedup.

    Returns:
        DurableFacts if any were found, None otherwise.
    """
    content = turn.get("content", "")
    if not content or len(content) < 20:
        return None

    memory_context = (
        f"Existing memory:\n{existing_memory}" if existing_memory.strip() else "No existing memory."
    )
    system_prompt = EXTRACTION_PROMPT.format(existing_memory=memory_context)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Turn ({turn.get('role', 'unknown')}):\n{content}"},
    ]

    try:
        response = await provider.acomplete(
            messages,
            max_tokens=512,
            temperature=0.0,
        )

        # Strip markdown fences
        text = strip_markdown_fences(response.content)

        data = json.loads(text)
        facts = data.get("facts", [])

        if facts:
            return DurableFacts(facts=facts, session_key=session_key)

    except Exception as exc:
        logger.debug("Fact extraction failed: %s", exc)

    return None
