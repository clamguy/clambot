import asyncio

from clambot.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """Async message bus with inbound and outbound queues."""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
