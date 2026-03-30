"""Channel manager — lifecycle orchestrator for all registered channels.

Manages startup/shutdown of channels and dispatches outbound messages
from the bus to the correct channel.
"""

from __future__ import annotations

import asyncio
import logging

from clambot.bus.events import OutboundMessage
from clambot.bus.queue import MessageBus
from clambot.channels.base import BaseChannel
from clambot.utils.tasks import tracked_task

logger = logging.getLogger(__name__)

__all__ = ["ChannelManager"]


class ChannelManager:
    """Registers channels, manages their lifecycle, and dispatches outbound messages."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._channels: dict[str, BaseChannel] = {}
        self._dispatch_task: asyncio.Task[None] | None = None
        self._channel_tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False

    # ── Registration ──────────────────────────────────────────

    def register(self, channel: BaseChannel) -> None:
        """Register a channel to be managed."""
        self._channels[channel.name] = channel
        logger.info("Registered channel: %s", channel.name)

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        """Start all registered channels and the outbound dispatcher."""
        self._running = True

        # Start each channel as a background task
        for name, channel in self._channels.items():
            task = tracked_task(self._start_channel(name, channel), name=f"channel-{name}")
            self._channel_tasks[name] = task

        # Start the outbound dispatch loop
        self._dispatch_task = tracked_task(self._dispatch_outbound(), name="outbound-dispatch")
        logger.info("ChannelManager started with %d channel(s)", len(self._channels))

    async def stop(self) -> None:
        """Stop the outbound dispatcher and all channels."""
        self._running = False

        # Stop the dispatch loop
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None

        # Stop all channels
        for name, channel in self._channels.items():
            try:
                await channel.stop()
            except Exception as exc:
                logger.debug("Error stopping channel %s: %s", name, exc)

        # Cancel all channel tasks
        for _name, task in self._channel_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._channel_tasks.clear()

        logger.info("ChannelManager stopped")

    # ── Internal ──────────────────────────────────────────────

    async def _start_channel(self, name: str, channel: BaseChannel) -> None:
        """Start a single channel, catching and logging errors."""
        try:
            await channel.start()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Channel %s crashed", name)

    async def _dispatch_outbound(self) -> None:
        """Dequeue outbound messages and route them to the matching channel."""
        while self._running:
            try:
                msg: OutboundMessage = await self._bus.outbound.get()
                channel = self._channels.get(msg.channel)
                if channel is None:
                    logger.warning("No channel registered for %r, dropping message", msg.channel)
                    continue
                try:
                    await channel.send(msg)
                except Exception:
                    logger.exception("Failed to send outbound on channel %s", msg.channel)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Dispatch loop error")

    # ── Accessors ─────────────────────────────────────────────

    def get_channel(self, name: str) -> BaseChannel | None:
        """Return a registered channel by name, or ``None``."""
        return self._channels.get(name)
