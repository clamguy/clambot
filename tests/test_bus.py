"""Tests for clambot.bus — Phase 2 Core Primitives."""

import dataclasses

import pytest

from clambot.bus.events import InboundMessage, OutboundMessage
from clambot.bus.queue import MessageBus

# ---------------------------------------------------------------------------
# InboundMessage
# ---------------------------------------------------------------------------


def test_inbound_message_is_frozen() -> None:
    """Assigning to any attribute of InboundMessage raises FrozenInstanceError."""
    msg = InboundMessage(channel="telegram", source="user1", chat_id="42", content="hi")

    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = "mutated"  # type: ignore[misc]


def test_inbound_message_replace_produces_new_instance() -> None:
    """dataclasses.replace() returns a new instance with the changed field."""
    original = InboundMessage(channel="telegram", source="user1", chat_id="42", content="hello")
    updated = dataclasses.replace(original, content="world")

    assert updated is not original
    assert updated.content == "world"
    # Unchanged fields are preserved
    assert updated.channel == original.channel
    assert updated.source == original.source
    assert updated.chat_id == original.chat_id


def test_inbound_message_auto_sets_session_key() -> None:
    """session_key is automatically set to 'channel:chat_id' when not provided."""
    msg = InboundMessage(channel="cli", source="system", chat_id="room99", content="ping")

    assert msg.session_key == "cli:room99"


def test_inbound_message_explicit_session_key_preserved() -> None:
    """An explicitly provided session_key is not overwritten."""
    msg = InboundMessage(
        channel="telegram",
        source="user1",
        chat_id="42",
        content="hi",
        session_key="custom:key",
    )

    assert msg.session_key == "custom:key"


# ---------------------------------------------------------------------------
# MessageBus
# ---------------------------------------------------------------------------


def test_message_bus_has_inbound_and_outbound_queues() -> None:
    """MessageBus exposes inbound and outbound asyncio.Queue instances."""
    import asyncio

    bus = MessageBus()

    assert isinstance(bus.inbound, asyncio.Queue)
    assert isinstance(bus.outbound, asyncio.Queue)


@pytest.mark.asyncio
async def test_message_bus_queues_are_independent() -> None:
    """Items placed on inbound do not appear on outbound and vice-versa."""
    bus = MessageBus()

    inbound_msg = InboundMessage(channel="telegram", source="user1", chat_id="1", content="in")
    outbound_msg = OutboundMessage(channel="telegram", target="1", content="out")

    await bus.inbound.put(inbound_msg)
    await bus.outbound.put(outbound_msg)

    assert bus.inbound.qsize() == 1
    assert bus.outbound.qsize() == 1
    assert await bus.inbound.get() is inbound_msg
    assert await bus.outbound.get() is outbound_msg
