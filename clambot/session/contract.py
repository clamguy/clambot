"""Compaction contract — prompt structure for compaction LLM calls.

The compaction LLM call summarises older conversation turns into a single
system message so that the active context window stays within budget.
"""

from __future__ import annotations

from clambot.session.types import SessionTurn

SYSTEM_PROMPT: str = (
    "You are a conversation summariser. "
    "Your task is to produce a concise but complete summary of the conversation "
    "history provided below. "
    "The summary will replace the original turns in the active context window, "
    "so it must preserve all facts, decisions, and action outcomes that may be "
    "relevant to future turns. "
    "Write the summary in third-person narrative form. "
    "Do not include any preamble or meta-commentary — output only the summary text."
)


def build_compaction_prompt(turns: list[SessionTurn]) -> list[dict]:
    """Assemble the messages list for a compaction LLM call.

    The returned list starts with the compaction system prompt followed by a
    single user message that contains the serialised conversation turns.

    Args:
        turns: The older turns that should be summarised (i.e. everything
               *except* the most-recent turns that will be kept verbatim).

    Returns:
        A ``list[dict]`` ready to pass to
        :meth:`~clambot.providers.base.LLMProvider.acomplete`.
    """
    conversation_text = "\n".join(f"[{turn.role.upper()}]: {turn.content}" for turn in turns)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Please summarise the following conversation history:\n\n" + conversation_text
            ),
        },
    ]
