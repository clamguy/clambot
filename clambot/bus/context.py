"""Request-scoped context variables for the active inbound message.

Set by the turn-execution layer *before* running the agent loop so that
tools (e.g. the cron sync hook) can discover the originating channel and
chat_id without explicit parameter threading.

These are :class:`contextvars.ContextVar` instances — safe for concurrent
asyncio tasks because each task inherits its own copy of the context.
"""

from __future__ import annotations

from contextvars import ContextVar

__all__ = ["current_channel", "current_chat_id"]

#: Channel name of the inbound message being processed (e.g. ``"telegram"``).
current_channel: ContextVar[str] = ContextVar("current_channel", default="")

#: Chat / conversation id of the inbound message (e.g. a Telegram chat-id).
current_chat_id: ContextVar[str] = ContextVar("current_chat_id", default="")
