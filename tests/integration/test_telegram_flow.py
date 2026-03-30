"""Integration tests for Telegram approval → secret → re-queue flows.

Tests:
- Approval keyboard: compact callback_data ≤64 bytes, short ID mapping,
  callback parsing resolves full approval_id, inbound placed on bus
- Secret request: message with /secret usage instructions
- Full flow: approval → secret_pending → /secret → re-queue with auto-approval
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from clambot.bus.events import InboundMessage, OutboundMessage
from clambot.bus.queue import MessageBus
from clambot.channels.telegram import TelegramChannel
from clambot.config.schema import TelegramConfig
from clambot.gateway.orchestrator import GatewayOrchestrator
from clambot.session.manager import SessionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> TelegramConfig:
    defaults = {"enabled": True, "token": "fake-token", "allow_from": []}
    defaults.update(overrides)
    return TelegramConfig(**defaults)


def _make_channel(bus: MessageBus | None = None) -> TelegramChannel:
    """Create a TelegramChannel with a mock PTB app/bot."""
    bus = bus or MessageBus()
    channel = TelegramChannel(_make_config(), bus)
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    mock_bot.delete_message = AsyncMock()
    mock_app = MagicMock()
    mock_app.bot = mock_bot
    channel._app = mock_app
    return channel


def _make_inbound(
    content: str = "hello",
    channel: str = "telegram",
    source: str = "999|testuser",
    chat_id: str = "12345",
    metadata: dict | None = None,
) -> InboundMessage:
    return InboundMessage(
        channel=channel,
        source=source,
        chat_id=chat_id,
        content=content,
        metadata=metadata or {},
    )


def _make_callback_query(data: str, chat_id: int = 12345) -> MagicMock:
    """Build a mock PTB Update with a callback_query."""
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message.chat_id = chat_id
    query.message.message_id = 100

    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = 999
    update.effective_user.username = "testuser"
    update.effective_user.first_name = "Test"
    return update


def _make_orchestrator(
    bus: MessageBus,
    tmp_path: Path,
    approval_gate: Any = None,
    agent_loop: Any = None,
    secret_store: Any = None,
) -> GatewayOrchestrator:
    return GatewayOrchestrator(
        bus=bus,
        session_manager=SessionManager(tmp_path),
        approval_gate=approval_gate,
        agent_loop=agent_loop,
        secret_store=secret_store,
        workspace=tmp_path,
    )


# ===================================================================
# Approval keyboard flow
# ===================================================================


class TestApprovalKeyboard:
    """Tests for _send_approval_keyboard and _on_callback_query."""

    @pytest.mark.asyncio
    async def test_callback_data_fits_64_bytes(self) -> None:
        """All approval buttons have callback_data ≤ 64 bytes."""
        channel = _make_channel()

        outbound = OutboundMessage(
            channel="telegram",
            target="12345",
            content="Approval required.",
            type="approval_pending",
            correlation_id="corr-1",
            metadata={
                "approval_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "tool_name": "http_request",
                "args": {"method": "GET", "url": "https://openrouter.ai/api/v1/credits"},
                "options": [
                    {
                        "id": "host:openrouter.ai",
                        "label": "host openrouter.ai",
                        "scope": "host:openrouter.ai",
                    },
                ],
            },
        )

        await channel._send_approval_keyboard(outbound)

        # Inspect the call to bot.send_message
        call_kwargs = channel._app.bot.send_message.call_args
        keyboard = call_kwargs.kwargs.get("reply_markup") or call_kwargs[1].get("reply_markup")
        assert keyboard is not None

        for row in keyboard.inline_keyboard:
            for button in row:
                assert len(button.callback_data) <= 64, (
                    f"callback_data too long ({len(button.callback_data)} bytes): "
                    f"{button.callback_data!r}"
                )

    @pytest.mark.asyncio
    async def test_short_id_mapping_stored(self) -> None:
        """_send_approval_keyboard stores short→full approval_id mapping."""
        channel = _make_channel()
        full_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

        outbound = OutboundMessage(
            channel="telegram",
            target="12345",
            content="Approval required.",
            type="approval_pending",
            metadata={
                "approval_id": full_id,
                "tool_name": "http_request",
                "args": {},
                "options": [],
            },
        )

        await channel._send_approval_keyboard(outbound)

        # Short ID should be in the mapping
        assert len(channel._short_to_full_approval_id) == 1
        short_id = next(iter(channel._short_to_full_approval_id))
        assert channel._short_to_full_approval_id[short_id] == full_id

    @pytest.mark.asyncio
    async def test_callback_resolves_full_approval_id(self) -> None:
        """_on_callback_query maps compact short_id back to full UUID."""
        bus = MessageBus()
        channel = _make_channel(bus)

        # Pre-populate the short→full mapping
        short_id = "a1b2c3d4e5f67890abcdef12"
        full_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        channel._short_to_full_approval_id[short_id] = full_id
        channel._approval_messages[full_id] = 100

        # Simulate "Allow Once" callback
        update = _make_callback_query(f"a:o|id:{short_id}")
        await channel._on_callback_query(update, MagicMock())

        # Should have placed an inbound with the FULL approval_id
        assert not bus.inbound.empty()
        msg = bus.inbound.get_nowait()
        assert full_id in msg.content
        assert msg.metadata["approval_id"] == full_id
        assert msg.metadata["decision"] == "ALLOW"

    @pytest.mark.asyncio
    async def test_callback_allow_always_with_option(self) -> None:
        """Allow-always callback includes the option_id and 'always' scope."""
        bus = MessageBus()
        channel = _make_channel(bus)

        short_id = "abcdef1234567890abcdef12"
        full_id = "abcdef12-3456-7890-abcd-ef1234567890"
        channel._short_to_full_approval_id[short_id] = full_id
        channel._approval_messages[full_id] = 100

        update = _make_callback_query(f"a:A|id:{short_id}|o:host:openrouter")
        await channel._on_callback_query(update, MagicMock())

        msg = bus.inbound.get_nowait()
        assert msg.metadata["approval_id"] == full_id
        assert msg.metadata["decision"] == "ALLOW"
        assert msg.metadata["grant_scope"] == "host:openrouter"

    @pytest.mark.asyncio
    async def test_callback_reject(self) -> None:
        """Reject callback sends DENY decision."""
        bus = MessageBus()
        channel = _make_channel(bus)

        short_id = "1234567890abcdef12345678"
        full_id = "12345678-90ab-cdef-1234-567890abcdef"
        channel._short_to_full_approval_id[short_id] = full_id
        channel._approval_messages[full_id] = 100

        update = _make_callback_query(f"a:r|id:{short_id}")
        await channel._on_callback_query(update, MagicMock())

        msg = bus.inbound.get_nowait()
        assert msg.metadata["decision"] == "DENY"


# ===================================================================
# Secret request flow
# ===================================================================


class TestSecretRequest:
    """Tests for _send_secret_request."""

    @pytest.mark.asyncio
    async def test_send_secret_request_message(self) -> None:
        """Secret request sends message with /secret usage instructions."""
        channel = _make_channel()

        outbound = OutboundMessage(
            channel="telegram",
            target="12345",
            content="Secret required: OPENROUTER_API_KEY",
            type="secret_pending",
            metadata={"missing_secrets": ["OPENROUTER_API_KEY"]},
        )

        await channel._send_secret_request(outbound)

        channel._app.bot.send_message.assert_called_once()
        call_kwargs = channel._app.bot.send_message.call_args
        text = call_kwargs.kwargs.get("text") or call_kwargs[1].get("text")
        assert "OPENROUTER_API_KEY" in text
        assert "Reply with the value" in text

    @pytest.mark.asyncio
    async def test_send_routes_secret_pending(self) -> None:
        """send() dispatches secret_pending to _send_secret_request."""
        channel = _make_channel()
        channel._send_secret_request = AsyncMock()

        outbound = OutboundMessage(
            channel="telegram",
            target="12345",
            content="",
            type="secret_pending",
        )

        await channel.send(outbound)
        channel._send_secret_request.assert_called_once_with(outbound)

    @pytest.mark.asyncio
    async def test_multiple_secrets(self) -> None:
        """Secret request lists all missing secret names."""
        channel = _make_channel()

        outbound = OutboundMessage(
            channel="telegram",
            target="12345",
            content="Secrets required",
            type="secret_pending",
            metadata={"missing_secrets": ["API_KEY", "API_SECRET"]},
        )

        await channel._send_secret_request(outbound)

        call_kwargs = channel._app.bot.send_message.call_args
        text = call_kwargs.kwargs.get("text") or call_kwargs[1].get("text")
        assert "API_KEY" in text
        assert "API_SECRET" in text
        assert "Reply with the value" in text


# ===================================================================
# Full approval → secret → re-queue integration
# ===================================================================


class TestApprovalSecretRequeueFlow:
    """Integration: approval_pending → secret_pending → /secret → re-queue."""

    @pytest.mark.asyncio
    async def test_secret_pending_stores_pending_no_duplicate_outbound(
        self,
        tmp_path: Path,
    ) -> None:
        """on_event for secret_pending stores the inbound for re-queue
        but does NOT emit a duplicate outbound message.  The outbound
        comes from turn_execution's return value only."""
        bus = MessageBus()
        mock_result = MagicMock()
        mock_result.content = "Secret required: MY_KEY."
        mock_result.status = "secret_pending"
        mock_result.missing_secrets = ["MY_KEY"]

        mock_loop = AsyncMock()
        mock_loop.process_turn = AsyncMock(return_value=mock_result)

        orch = _make_orchestrator(
            bus=bus,
            tmp_path=tmp_path,
            agent_loop=mock_loop,
        )

        inbound = _make_inbound(content="do something needing MY_KEY")
        outbound = await orch._process_inbound(inbound)

        # turn_execution returns the secret_pending outbound
        assert outbound is not None
        assert outbound.type == "secret_pending"

        # Drain the bus and check: no secret_pending outbounds should have
        # been emitted by on_event (only status_delete from phase_callback
        # is expected as a side-effect).
        secret_outbounds = []
        while not bus.outbound.empty():
            msg = bus.outbound.get_nowait()
            if msg.type == "secret_pending":
                secret_outbounds.append(msg)

        assert len(secret_outbounds) == 0, (
            "on_event should not emit a duplicate secret_pending outbound"
        )

    @pytest.mark.asyncio
    async def test_secret_command_requeues_original(self, tmp_path: Path) -> None:
        """After /secret, the original inbound is re-queued with secret_resume."""
        bus = MessageBus()
        mock_store = MagicMock()
        mock_store.save = MagicMock()

        orch = _make_orchestrator(
            bus=bus,
            tmp_path=tmp_path,
            secret_store=mock_store,
        )

        # Simulate a pending secret request
        original = _make_inbound(content="show my credits")
        orch._pending_approvals["secret:OPENROUTER_API_KEY"] = original

        # User provides the secret
        secret_msg = _make_inbound(content="/secret OPENROUTER_API_KEY sk-or-123")
        result = await orch._process_inbound(secret_msg)

        # Secret was stored
        mock_store.save.assert_called_once_with("OPENROUTER_API_KEY", "sk-or-123")

        # Ack returned
        assert result is not None
        assert "stored" in result.content.lower()
        assert "Re-running" in result.content

        # Original re-queued with secret_resume
        assert not bus.inbound.empty()
        resumed = bus.inbound.get_nowait()
        assert resumed.content == "show my credits"
        assert resumed.metadata.get("secret_resume") is True

    @pytest.mark.asyncio
    async def test_approval_grants_persisted_for_requeue(
        self,
        tmp_path: Path,
    ) -> None:
        """When secret_pending follows an approval, wildcard always_grants
        are registered so the re-queued run auto-approves."""
        from clambot.agent.approval_gate import ApprovalGate

        bus = MessageBus()
        gate = ApprovalGate(
            approvals_config=MagicMock(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            )
        )

        # Build a mock agent loop that:
        # 1. Emits approval_pending via on_event
        # 2. Emits secret_pending via on_event
        # 3. Returns secret_pending result
        async def fake_process_turn(*, message, session_key, history, config, on_event):
            # Simulate approval_pending event
            if on_event:
                on_event(
                    {
                        "type": "approval_pending",
                        "approval_id": "test-approval-1",
                        "tool_name": "http_request",
                        "args": {"url": "https://openrouter.ai/api/v1/credits"},
                        "options": [],
                    }
                )
                # Simulate secret_pending event (after approval resolved)
                on_event(
                    {
                        "type": "secret_pending",
                        "missing_secrets": ["OPENROUTER_API_KEY"],
                    }
                )

            result = MagicMock()
            result.content = "Secret required: OPENROUTER_API_KEY."
            result.status = "secret_pending"
            result.missing_secrets = ["OPENROUTER_API_KEY"]
            return result

        mock_loop = AsyncMock()
        mock_loop.process_turn = AsyncMock(side_effect=fake_process_turn)

        orch = _make_orchestrator(
            bus=bus,
            tmp_path=tmp_path,
            approval_gate=gate,
            agent_loop=mock_loop,
        )

        inbound = _make_inbound(content="show credits")
        await orch._process_inbound(inbound)

        # The gate should now have a wildcard always_grant for http_request
        grants = gate.store.get_always_grants()
        http_grants = [g for g in grants if g["tool"] == "http_request"]
        assert len(http_grants) >= 1, f"Expected always_grant for http_request, got: {grants}"
        assert http_grants[0]["scope"] == "*"

        # Verify: a new evaluation for http_request should be ALLOW
        from clambot.agent.approvals import compute_scope_fingerprint

        fp = compute_scope_fingerprint("http_request", {"url": "https://openrouter.ai"})
        assert gate.store.check_always_grant("http_request", fp) is True

    @pytest.mark.asyncio
    async def test_requeued_turn_auto_approves_via_always_grant(
        self,
        tmp_path: Path,
    ) -> None:
        """After /secret re-queue, the next turn auto-approves via the
        wildcard always_grant — no second approval prompt needed.

        This test wires a real ApprovalGate through the orchestrator to
        verify the grant is checked on the re-queued turn, NOT just
        that it was registered.
        """
        from clambot.agent.approval_gate import ApprovalDecision, ApprovalGate

        bus = MessageBus()
        gate = ApprovalGate(
            approvals_config=MagicMock(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            )
        )
        mock_store = MagicMock()
        mock_store.save = MagicMock()

        turn_count = [0]

        async def fake_process_turn(*, message, session_key, history, config, on_event):
            turn_count[0] += 1

            if turn_count[0] == 1:
                # First turn: approval + secret_pending
                if on_event:
                    on_event(
                        {
                            "type": "approval_pending",
                            "approval_id": "ap-1",
                            "tool_name": "http_request",
                            "args": {"url": "https://api.example.com"},
                            "options": [],
                        }
                    )
                    on_event(
                        {
                            "type": "secret_pending",
                            "missing_secrets": ["API_KEY"],
                        }
                    )
                result = MagicMock()
                result.content = "Secret required: API_KEY."
                result.status = "secret_pending"
                result.missing_secrets = ["API_KEY"]
                return result
            else:
                # Second turn: verify the gate would auto-approve
                eval_result = gate.evaluate_request(
                    "http_request",
                    {"url": "https://api.example.com"},
                    run_id="run-2",
                )
                assert eval_result.decision == ApprovalDecision.ALLOW, (
                    f"Expected ALLOW from always_grant, got {eval_result.decision}. "
                    f"Grants: {gate.store.get_always_grants()}"
                )
                result = MagicMock()
                result.content = "Credits: 256"
                result.status = "completed"
                result.missing_secrets = []
                return result

        mock_loop = AsyncMock()
        mock_loop.process_turn = AsyncMock(side_effect=fake_process_turn)

        orch = _make_orchestrator(
            bus=bus,
            tmp_path=tmp_path,
            approval_gate=gate,
            agent_loop=mock_loop,
            secret_store=mock_store,
        )

        # Turn 1: user message → approval + secret_pending
        inbound = _make_inbound(content="show my credits")
        await orch._process_inbound(inbound)

        # Verify always_grant was registered
        grants = gate.store.get_always_grants()
        assert any(g["tool"] == "http_request" and g["scope"] == "*" for g in grants), (
            f"Expected wildcard always_grant for http_request, got: {grants}"
        )

        # Turn 2: /secret → re-queue → auto-approved turn
        secret_msg = _make_inbound(content="/secret API_KEY sk-123")
        await orch._process_inbound(secret_msg)

        # Re-queued message should be on the bus
        assert not bus.inbound.empty(), "No re-queued message after /secret"
        resumed = bus.inbound.get_nowait()

        # Process the re-queued message
        out = await orch._process_inbound(resumed)

        # Second turn completed successfully (the assert inside fake_process_turn
        # verified the gate evaluation returned ALLOW)
        assert out is not None
        assert turn_count[0] == 2
        assert out.content == "Credits: 256"

    @pytest.mark.asyncio
    async def test_plain_message_as_secret_value(self, tmp_path: Path) -> None:
        """When a secret is pending, a plain (non-command) message is
        treated as the secret value — no /secret prefix needed."""
        bus = MessageBus()
        mock_store = MagicMock()
        mock_store.save = MagicMock()

        orch = _make_orchestrator(
            bus=bus,
            tmp_path=tmp_path,
            secret_store=mock_store,
        )

        # Simulate a pending secret request
        original = _make_inbound(content="show my credits")
        orch._pending_approvals["secret:OPENROUTER_API_KEY"] = original

        # User just sends the raw value (no /secret command)
        plain_msg = _make_inbound(content="sk-or-v1-abc123xyz")
        result = await orch._process_inbound(plain_msg)

        # Secret was stored with the correct name and value
        mock_store.save.assert_called_once_with("OPENROUTER_API_KEY", "sk-or-v1-abc123xyz")

        # Ack mentions the secret name
        assert result is not None
        assert "OPENROUTER_API_KEY" in result.content
        assert "stored" in result.content.lower()

        # Original was re-queued
        assert not bus.inbound.empty()
        resumed = bus.inbound.get_nowait()
        assert resumed.content == "show my credits"
        assert resumed.metadata.get("secret_resume") is True

    @pytest.mark.asyncio
    async def test_plain_message_not_captured_without_pending_secret(
        self,
        tmp_path: Path,
    ) -> None:
        """When NO secret is pending, plain messages go to the agent
        turn as normal — not misinterpreted as a secret value."""
        bus = MessageBus()

        mock_loop = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "Hello!"
        mock_result.status = "completed"
        mock_result.missing_secrets = []
        mock_loop.process_turn = AsyncMock(return_value=mock_result)

        orch = _make_orchestrator(
            bus=bus,
            tmp_path=tmp_path,
            agent_loop=mock_loop,
        )

        # No pending secrets — plain message should go to agent
        msg = _make_inbound(content="sk-or-v1-abc123xyz")
        result = await orch._process_inbound(msg)

        # Agent loop was called (not the secret handler)
        mock_loop.process_turn.assert_called_once()
        assert result.content == "Hello!"

    @pytest.mark.asyncio
    async def test_command_messages_not_captured_as_secret(
        self,
        tmp_path: Path,
    ) -> None:
        """Messages starting with / are never captured as secret values,
        even when a secret is pending."""
        bus = MessageBus()

        mock_loop = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "New session started."
        mock_result.status = "completed"
        mock_result.missing_secrets = []
        mock_loop.process_turn = AsyncMock(return_value=mock_result)

        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path, agent_loop=mock_loop)

        # Secret is pending
        original = _make_inbound(content="show credits")
        orch._pending_approvals["secret:API_KEY"] = original

        # User sends a command — should NOT be treated as a secret value
        cmd_msg = _make_inbound(content="/new")
        result = await orch._process_inbound(cmd_msg)

        # The /new handler ran, not the secret handler
        # (pending secret should still be there)
        assert "secret:API_KEY" in orch._pending_approvals

    @pytest.mark.asyncio
    async def test_no_duplicate_secret_messages_end_to_end(
        self,
        tmp_path: Path,
    ) -> None:
        """Full flow: only ONE secret_pending message reaches the outbound bus."""
        bus = MessageBus()

        async def fake_process_turn(*, message, session_key, history, config, on_event):
            if on_event:
                on_event(
                    {
                        "type": "secret_pending",
                        "missing_secrets": ["MY_KEY"],
                    }
                )
            result = MagicMock()
            result.content = "Secret required: MY_KEY."
            result.status = "secret_pending"
            result.missing_secrets = ["MY_KEY"]
            return result

        mock_loop = AsyncMock()
        mock_loop.process_turn = AsyncMock(side_effect=fake_process_turn)

        orch = _make_orchestrator(
            bus=bus,
            tmp_path=tmp_path,
            agent_loop=mock_loop,
        )

        inbound = _make_inbound(content="need a key")

        # Run through _run_agent_turn (which wires on_event)
        outbound = await orch._run_agent_turn(inbound)

        # The returned outbound is the one from turn_execution
        assert outbound is not None

        # Drain the bus: only status_delete from phase_callback is expected,
        # NOT a duplicate secret_pending from on_event.
        secret_outbounds = []
        while not bus.outbound.empty():
            msg = bus.outbound.get_nowait()
            if msg.type == "secret_pending":
                secret_outbounds.append(msg)

        assert len(secret_outbounds) == 0, (
            "on_event for secret_pending should not emit outbound — "
            "only turn_execution's return value should"
        )
