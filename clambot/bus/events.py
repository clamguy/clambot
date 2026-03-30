from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InboundMessage:
    """Message received from a chat channel. Immutable — use dataclasses.replace() for copies."""

    channel: str  # "telegram", "cli", "cron", "heartbeat"
    source: str  # User identifier (e.g. user_id or "system")
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    session_key: str = ""  # "channel:chat_id" — set at creation
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    media: tuple[str, ...] = ()  # Media URLs (tuple for immutability)
    metadata: dict[str, Any] = field(
        default_factory=dict
    )  # Channel-specific data; NOTE: dict is mutable but frozen only prevents reassignment

    def __post_init__(self) -> None:
        # Auto-set session_key if not provided
        if not self.session_key:
            object.__setattr__(self, "session_key", f"{self.channel}:{self.chat_id}")


@dataclass(frozen=True)
class OutboundMessage:
    """Message to send to a chat channel."""

    channel: str  # Target channel
    target: str  # Target chat_id
    content: str  # Message text
    type: str = "text"  # "text", "approval_pending", "status_update", "status_delete"
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    media: tuple[str, ...] = ()
    reply_to: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
