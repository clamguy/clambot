"""Base channel interface for chat platforms.

Defines the :class:`BaseChannel` ABC that all channel implementations
(Telegram, Discord, etc.) must subclass.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from clambot.bus.events import InboundMessage, OutboundMessage
from clambot.bus.queue import MessageBus

logger = logging.getLogger(__name__)

__all__ = ["BaseChannel"]


class BaseChannel(ABC):
    """Abstract base class for chat channel implementations.

    Each channel integrates with a chat platform and communicates with
    the rest of the system via the :class:`MessageBus`.
    """

    name: str = "base"

    def __init__(self, config: Any, bus: MessageBus, *, workspace: Path | None = None) -> None:
        self.config = config
        self.bus = bus
        self.workspace = workspace
        self._running = False

    # ── Abstract interface ────────────────────────────────────

    @abstractmethod
    async def start(self) -> None:
        """Start the channel and begin listening for messages."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        ...

    @abstractmethod
    async def send(self, outbound: OutboundMessage) -> None:
        """Send an outbound message through this channel."""
        ...

    # ── Source filtering ──────────────────────────────────────

    def is_allowed_source(self, source: str) -> bool:
        """Check whether *source* is permitted to use this channel.

        Matching rules (evaluated in order):
        1. If ``allow_from`` is empty → allow everyone.
        2. Exact match of the full *source* string.
        3. Pipe-segment match: any segment of ``source.split("|")``
           appears in ``allow_from``.
        """
        allow_list: list[str] = getattr(self.config, "allow_from", [])
        if not allow_list:
            return True

        source_str = str(source)
        if source_str in allow_list:
            return True

        # Pipe-segment matching (e.g. "123456|alice" matches "123456" or "alice")
        if "|" in source_str:
            for segment in source_str.split("|"):
                if segment and segment in allow_list:
                    return True

        return False

    # ── Inbound helper ────────────────────────────────────────

    async def _handle_message(
        self,
        source: str,
        chat_id: str,
        content: str,
        correlation_id: str | None = None,
        media: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Check permissions and publish an :class:`InboundMessage` to the bus."""
        if not self.is_allowed_source(source):
            logger.warning(
                "Access denied for source %s on channel %s. "
                "Add them to allowFrom list in config to grant access.",
                source,
                self.name,
            )
            return

        kwargs: dict[str, Any] = {
            "channel": self.name,
            "source": str(source),
            "chat_id": str(chat_id),
            "content": content,
            "media": media,
            "metadata": metadata or {},
        }
        if correlation_id is not None:
            kwargs["correlation_id"] = correlation_id

        msg = InboundMessage(**kwargs)
        await self.bus.inbound.put(msg)

    # ── Properties ────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """Whether the channel is currently running."""
        return self._running
