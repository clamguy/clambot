"""Tests for Phase 10 — Telegram Channel.

Covers:
- ``chunk_text``: no split, newline split, space split, hard split, exactly 4096
- ``convert_to_markdownv2``: bold, italic, code, code block, link, blockquote
- ``is_allowed_source``: exact user_id, user_id|username, username only, not in list
- Typing indicator: started on message receive, stopped before send
- ChannelManager: dispatch outbound routing, lifecycle
- BaseChannel: source filtering
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from clambot.bus.events import OutboundMessage
from clambot.bus.queue import MessageBus
from clambot.channels.base import BaseChannel
from clambot.channels.manager import ChannelManager
from clambot.channels.telegram import TelegramChannel
from clambot.channels.telegram_utils import chunk_text, convert_to_markdownv2
from clambot.config.schema import TelegramConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DummyChannel(BaseChannel):
    """Minimal concrete channel for testing BaseChannel."""

    name = "dummy"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, outbound: OutboundMessage) -> None:
        pass


def _make_telegram_config(**overrides) -> TelegramConfig:
    defaults = {"enabled": True, "token": "fake-token", "allow_from": []}
    defaults.update(overrides)
    return TelegramConfig(**defaults)


# ===================================================================
# chunk_text
# ===================================================================


class TestChunkText:
    """Tests for the chunk_text utility."""

    def test_no_split_needed(self):
        """Short text returns as single chunk."""
        result = chunk_text("hello world", max_len=4096)
        assert result == ["hello world"]

    def test_empty_string(self):
        """Empty string returns empty list."""
        assert chunk_text("") == []

    def test_split_on_newline(self):
        """Prefer splitting at newline boundaries."""
        text = "line one\nline two\nline three"
        result = chunk_text(text, max_len=18)
        assert len(result) >= 2
        # First chunk should end at a newline boundary
        assert "\n" not in result[0] or len(result[0]) <= 18

    def test_split_on_space(self):
        """Fall back to space boundary when no newlines."""
        text = "word " * 20  # 100 chars
        result = chunk_text(text.strip(), max_len=30)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 30

    def test_hard_split(self):
        """Hard split when no newline or space found."""
        text = "a" * 100
        result = chunk_text(text, max_len=30)
        assert len(result) >= 4
        for chunk in result:
            assert len(chunk) <= 30
        assert "".join(result) == text

    def test_exactly_max_len(self):
        """Text exactly at max_len should be a single chunk."""
        text = "x" * 4096
        result = chunk_text(text, max_len=4096)
        assert result == [text]

    def test_one_over_max_len(self):
        """Text one char over max_len should produce two chunks."""
        text = "x" * 4097
        result = chunk_text(text, max_len=4096)
        assert len(result) == 2


# ===================================================================
# convert_to_markdownv2
# ===================================================================


class TestConvertToMarkdownV2:
    """Tests for MarkdownV2 conversion."""

    def test_empty_string(self):
        assert convert_to_markdownv2("") == ""

    def test_bold(self):
        result = convert_to_markdownv2("**bold text**")
        assert "*bold text*" in result

    def test_italic(self):
        result = convert_to_markdownv2("_italic text_")
        assert "_italic text_" in result

    def test_inline_code(self):
        result = convert_to_markdownv2("use `code` here")
        assert "`code`" in result

    def test_code_block(self):
        result = convert_to_markdownv2("```python\nprint('hi')\n```")
        assert "```python" in result
        assert "print('hi')" in result

    def test_link(self):
        result = convert_to_markdownv2("[click](https://example.com)")
        assert "https://example.com" in result
        assert "[" in result

    def test_blockquote(self):
        result = convert_to_markdownv2("> quoted text")
        assert ">" in result
        assert "quoted" in result

    def test_header_becomes_bold(self):
        result = convert_to_markdownv2("# My Header")
        # Header should be converted to bold (wrapped in *)
        assert "*My Header*" in result

    def test_bullet_list(self):
        result = convert_to_markdownv2("- item one\n- item two")
        assert "•" in result

    def test_strikethrough(self):
        result = convert_to_markdownv2("~~deleted~~")
        assert "~deleted~" in result

    def test_special_chars_escaped(self):
        """Special characters outside formatting should be escaped."""
        result = convert_to_markdownv2("price is 5.99")
        assert "5\\.99" in result


# ===================================================================
# is_allowed_source (BaseChannel)
# ===================================================================


class TestIsAllowedSource:
    """Tests for BaseChannel.is_allowed_source."""

    def _make_channel(self, allow_from: list[str]) -> _DummyChannel:
        config = _make_telegram_config(allow_from=allow_from)
        bus = MessageBus()
        ch = _DummyChannel(config, bus)
        return ch

    def test_empty_allow_list_allows_everyone(self):
        ch = self._make_channel([])
        assert ch.is_allowed_source("12345") is True
        assert ch.is_allowed_source("anyone") is True

    def test_exact_user_id_match(self):
        ch = self._make_channel(["12345"])
        assert ch.is_allowed_source("12345") is True
        assert ch.is_allowed_source("99999") is False

    def test_user_id_pipe_username(self):
        """Source '12345|alice' matches allow_from=['12345']."""
        ch = self._make_channel(["12345"])
        assert ch.is_allowed_source("12345|alice") is True

    def test_username_only_match(self):
        """Source '12345|alice' matches allow_from=['alice']."""
        ch = self._make_channel(["alice"])
        assert ch.is_allowed_source("12345|alice") is True

    def test_not_in_list(self):
        ch = self._make_channel(["99999"])
        assert ch.is_allowed_source("12345") is False
        assert ch.is_allowed_source("12345|alice") is False


# ===================================================================
# _parse_approval_action
# ===================================================================


class TestParseApprovalAction:
    """Tests for TelegramChannel._parse_approval_action."""

    def test_allow_once(self):
        decision, scope = TelegramChannel._parse_approval_action("allow_once")
        assert decision == "ALLOW"
        assert scope == ""

    def test_allow_always_no_option(self):
        decision, scope = TelegramChannel._parse_approval_action("allow_always")
        assert decision == "ALLOW"
        assert scope == "always"

    def test_allow_always_with_option(self):
        decision, scope = TelegramChannel._parse_approval_action("allow_always", "host:example.com")
        assert decision == "ALLOW"
        assert scope == "host:example.com"

    def test_reject(self):
        decision, scope = TelegramChannel._parse_approval_action("reject")
        assert decision == "DENY"
        assert scope == ""


# ===================================================================
# Typing indicator lifecycle
# ===================================================================


class TestTypingIndicator:
    """Tests for typing indicator start/stop."""

    @pytest.mark.asyncio
    async def test_typing_started_on_message(self):
        """Typing indicator is started when a message is received."""
        config = _make_telegram_config(allow_from=[])
        bus = MessageBus()
        channel = TelegramChannel(config, bus)

        # Mock the _start_typing_indicator and _handle_message
        started_corr_ids: list[str] = []
        original_start = channel._start_typing_indicator

        def mock_start(corr_id, chat_id):
            started_corr_ids.append(corr_id)

        channel._start_typing_indicator = mock_start

        # Build a fake Update
        update = MagicMock()
        update.message.text = "hello"
        update.message.caption = None
        update.message.chat_id = 12345
        update.message.message_id = 1
        update.message.chat.type = "private"
        update.effective_user.id = 999
        update.effective_user.username = "testuser"
        update.effective_user.first_name = "Test"

        # Patch _handle_message to avoid bus interaction
        channel._handle_message = AsyncMock()

        await channel._on_message(update, MagicMock())

        assert len(started_corr_ids) == 1

    @pytest.mark.asyncio
    async def test_typing_stopped_before_send(self):
        """Typing indicator is stopped before text is sent."""
        config = _make_telegram_config()
        bus = MessageBus()
        channel = TelegramChannel(config, bus)

        stopped_corr_ids: list[str] = []
        original_stop = channel._stop_typing_indicator

        def mock_stop(corr_id):
            stopped_corr_ids.append(corr_id)

        channel._stop_typing_indicator = mock_stop

        # Mock the bot's send_message
        mock_bot = AsyncMock()
        mock_app = MagicMock()
        mock_app.bot = mock_bot
        channel._app = mock_app

        outbound = OutboundMessage(
            channel="telegram",
            target="12345",
            content="hello response",
            type="text",
            correlation_id="corr-123",
        )

        await channel._send_text(outbound)
        assert "corr-123" in stopped_corr_ids


# ===================================================================
# ChannelManager
# ===================================================================


class TestChannelManager:
    """Tests for ChannelManager dispatch and lifecycle."""

    @pytest.mark.asyncio
    async def test_register_and_get(self):
        bus = MessageBus()
        manager = ChannelManager(bus)
        config = _make_telegram_config()
        ch = _DummyChannel(config, bus)
        manager.register(ch)
        assert manager.get_channel("dummy") is ch

    @pytest.mark.asyncio
    async def test_dispatch_routes_to_correct_channel(self):
        """Outbound messages are dispatched to the channel matching msg.channel."""
        bus = MessageBus()
        manager = ChannelManager(bus)
        config = _make_telegram_config()

        sent_messages: list[OutboundMessage] = []

        class _TrackingChannel(BaseChannel):
            name = "tracking"

            async def start(self):
                self._running = True

            async def stop(self):
                self._running = False

            async def send(self, outbound):
                sent_messages.append(outbound)

        ch = _TrackingChannel(config, bus)
        manager.register(ch)

        # Start manager (includes dispatch loop)
        await manager.start()

        # Put a message on the outbound bus
        outbound = OutboundMessage(channel="tracking", target="123", content="hi")
        bus.outbound.put_nowait(outbound)

        # Give the dispatch loop time to process
        await asyncio.sleep(0.1)

        await manager.stop()

        assert len(sent_messages) == 1
        assert sent_messages[0].content == "hi"

    @pytest.mark.asyncio
    async def test_dispatch_drops_unknown_channel(self):
        """Messages for unregistered channels are dropped gracefully."""
        bus = MessageBus()
        manager = ChannelManager(bus)

        await manager.start()

        outbound = OutboundMessage(channel="nonexistent", target="123", content="hi")
        bus.outbound.put_nowait(outbound)

        await asyncio.sleep(0.1)
        await manager.stop()

        # Should not raise — just logs a warning


# ===================================================================
# BaseChannel._handle_message
# ===================================================================


class TestBaseChannelHandleMessage:
    """Tests for the _handle_message helper."""

    @pytest.mark.asyncio
    async def test_allowed_source_publishes(self):
        """Allowed source produces an InboundMessage on the bus."""
        config = _make_telegram_config(allow_from=["123"])
        bus = MessageBus()
        ch = _DummyChannel(config, bus)

        await ch._handle_message(source="123", chat_id="456", content="hello")

        msg = await asyncio.wait_for(bus.inbound.get(), timeout=1.0)
        assert msg.source == "123"
        assert msg.chat_id == "456"
        assert msg.content == "hello"
        assert msg.channel == "dummy"

    @pytest.mark.asyncio
    async def test_denied_source_does_not_publish(self):
        """Denied source does NOT put a message on the bus."""
        config = _make_telegram_config(allow_from=["999"])
        bus = MessageBus()
        ch = _DummyChannel(config, bus)

        await ch._handle_message(source="123", chat_id="456", content="hello")

        assert bus.inbound.empty()

    @pytest.mark.asyncio
    async def test_custom_correlation_id(self):
        """Custom correlation_id is passed through."""
        config = _make_telegram_config(allow_from=[])
        bus = MessageBus()
        ch = _DummyChannel(config, bus)

        await ch._handle_message(
            source="123",
            chat_id="456",
            content="hello",
            correlation_id="custom-corr",
        )

        msg = await asyncio.wait_for(bus.inbound.get(), timeout=1.0)
        assert msg.correlation_id == "custom-corr"


# ===================================================================
# TelegramChannel send routing
# ===================================================================


class TestTelegramChannelSendRouting:
    """Tests for the send() dispatch method."""

    @pytest.mark.asyncio
    async def test_send_routes_text(self):
        config = _make_telegram_config()
        bus = MessageBus()
        channel = TelegramChannel(config, bus)

        channel._send_text = AsyncMock()
        channel._app = MagicMock()

        outbound = OutboundMessage(channel="telegram", target="123", content="hi", type="text")
        await channel.send(outbound)
        channel._send_text.assert_called_once_with(outbound)

    @pytest.mark.asyncio
    async def test_send_routes_approval(self):
        config = _make_telegram_config()
        bus = MessageBus()
        channel = TelegramChannel(config, bus)

        channel._send_approval_keyboard = AsyncMock()
        channel._app = MagicMock()

        outbound = OutboundMessage(
            channel="telegram", target="123", content="", type="approval_pending"
        )
        await channel.send(outbound)
        channel._send_approval_keyboard.assert_called_once_with(outbound)

    @pytest.mark.asyncio
    async def test_send_routes_status_update(self):
        config = _make_telegram_config()
        bus = MessageBus()
        channel = TelegramChannel(config, bus)

        channel._send_or_edit_status = AsyncMock()
        channel._app = MagicMock()

        outbound = OutboundMessage(
            channel="telegram", target="123", content="status", type="status_update"
        )
        await channel.send(outbound)
        channel._send_or_edit_status.assert_called_once_with(outbound)

    @pytest.mark.asyncio
    async def test_send_routes_status_delete(self):
        config = _make_telegram_config()
        bus = MessageBus()
        channel = TelegramChannel(config, bus)

        channel._delete_status = AsyncMock()
        channel._app = MagicMock()

        outbound = OutboundMessage(
            channel="telegram", target="123", content="", type="status_delete"
        )
        await channel.send(outbound)
        channel._delete_status.assert_called_once_with(outbound)

    @pytest.mark.asyncio
    async def test_send_noop_when_no_app(self):
        """send() is a no-op when bot is not running (app is None)."""
        config = _make_telegram_config()
        bus = MessageBus()
        channel = TelegramChannel(config, bus)
        channel._app = None

        outbound = OutboundMessage(channel="telegram", target="123", content="hi")
        # Should not raise
        await channel.send(outbound)


# ===================================================================
# TelegramChannel._build_source
# ===================================================================


class TestBuildSource:
    """Tests for the _build_source static method."""

    def test_with_username(self):
        user = MagicMock()
        user.id = 12345
        user.username = "alice"
        assert TelegramChannel._build_source(user) == "12345|alice"

    def test_without_username(self):
        user = MagicMock()
        user.id = 12345
        user.username = None
        assert TelegramChannel._build_source(user) == "12345"
