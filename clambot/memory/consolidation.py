"""Memory consolidation — LLM-based session summarization for long-term memory.

When a session ends (``/new``), consolidates the conversation into:
  1. A HISTORY.md entry (append-only log)
  2. An updated MEMORY.md (overwrite with merged facts)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clambot.utils.text import strip_markdown_fences

from .store import memory_append_history, memory_recall, memory_save

logger = logging.getLogger(__name__)


@dataclass
class MemoryUpdate:
    """Result of memory consolidation."""

    history_entry: str = ""
    memory_update: str = ""
    changed: bool = False


CONSOLIDATION_PROMPT = """\
You are a memory consolidation agent. Analyze the conversation history and produce:

1. **history_entry**: A concise 2-3 sentence summary of this conversation for the log.
2. **memory_update**: Updated long-term memory. ONLY keep truly durable facts about the \
user that are unlikely to change and should be remembered permanently:
   - User's name, nickname, preferred name for the assistant
   - User's location, timezone, language preferences
   - Long-term interests, hobbies, profession, expertise
   - Communication style preferences
   - Important personal details

   REMOVE from memory (transient, does NOT belong):
   - Data values, numbers, balances, prices, statistics
   - Task results, API responses, computation outputs
   - Operational state (scheduled jobs, reminders, cron settings)
   - System configuration (API keys, secrets, tool settings)
   - Anything about what the assistant did or needs to do

Current MEMORY.md content:
{current_memory}

Respond with a JSON object (no markdown fences):
{{"history_entry": "<summary for history log>", "memory_update": "<updated memory content>"}}
"""


async def consolidate_session_memory(
    turns: list[dict[str, Any]],
    workspace: Path,
    provider: Any,
) -> MemoryUpdate:
    """Consolidate a session's conversation into long-term memory.

    Args:
        turns: List of conversation turns (role/content dicts).
        workspace: Workspace path for memory files.
        provider: LLM provider for consolidation.

    Returns:
        A MemoryUpdate with the history entry and memory update.
    """
    if not turns:
        return MemoryUpdate()

    # Read current memory
    current_memory = memory_recall(workspace)

    # Build conversation summary for LLM
    conversation = _format_turns(turns)

    prompt = CONSOLIDATION_PROMPT.format(
        current_memory=current_memory or "(empty)",
    )

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Conversation to consolidate:\n\n{conversation}"},
    ]

    try:
        response = await provider.acomplete(
            messages,
            max_tokens=2048,
            temperature=0.0,
        )

        # Strip markdown fences
        text = strip_markdown_fences(response.content)

        # Extract JSON object — find the outermost { ... }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # LLM may produce unescaped content — try to extract fields
            # via simple pattern matching as a fallback
            import re

            he_match = re.search(r'"history_entry"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            mu_match = re.search(r'"memory_update"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            data = {
                "history_entry": he_match.group(1) if he_match else "",
                "memory_update": mu_match.group(1) if mu_match else "",
            }
            if not data["history_entry"] and not data["memory_update"]:
                raise
        history_entry = data.get("history_entry", "")
        memory_update = data.get("memory_update", "")

        result = MemoryUpdate(
            history_entry=history_entry,
            memory_update=memory_update,
            changed=bool(memory_update and memory_update != current_memory),
        )

        # Apply updates
        if history_entry:
            memory_append_history(workspace, history_entry)

        if result.changed:
            memory_save(workspace, memory_update)

        return result

    except Exception as exc:
        logger.warning("Memory consolidation failed: %s", exc)
        return MemoryUpdate()


def _format_turns(turns: list[dict[str, Any]], max_chars: int = 10000) -> str:
    """Format conversation turns for the consolidation prompt."""
    parts: list[str] = []
    total = 0

    for turn in turns:
        role = turn.get("role", "unknown")
        content = turn.get("content", "")
        entry = f"[{role}]: {content}"

        if total + len(entry) > max_chars:
            parts.append("[... conversation truncated ...]")
            break

        parts.append(entry)
        total += len(entry)

    return "\n\n".join(parts)
