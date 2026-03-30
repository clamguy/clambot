"""Utilities for converting session turns to LLM-compatible message dicts."""

from __future__ import annotations

from clambot.session.types import SessionTurn


def turns_to_llm_history(turns: list[SessionTurn]) -> list[dict[str, str]]:
    """Convert a list of session turns to the message format expected by LLM APIs.

    Each turn becomes a ``{"role": ..., "content": ...}`` dict. For ``"tool"``
    role turns, ``tool_call_id`` and ``name`` are included from the turn's
    metadata when present.

    Args:
        turns: Ordered list of :class:`~clambot.session.types.SessionTurn`
               objects representing the conversation history.

    Returns:
        List of message dicts suitable for passing to
        :meth:`~clambot.providers.base.LLMProvider.acomplete`.
    """
    messages: list[dict[str, str]] = []
    for turn in turns:
        msg: dict[str, str] = {"role": turn.role, "content": turn.content}
        if turn.role == "tool":
            if "tool_call_id" in turn.metadata:
                msg["tool_call_id"] = turn.metadata["tool_call_id"]
            if "name" in turn.metadata:
                msg["name"] = turn.metadata["name"]
        messages.append(msg)
    return messages
