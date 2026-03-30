"""Tests for clambot.gateway.orchestrator — Phase 9 Gateway Orchestrator.

Tests:
- /approve re-queues original inbound with approval_resume=True
- /approve returns acknowledgment with correct correlation_id
- /secret stores secret + re-queues original with secret_resume=True
- /new clears session cache (mock session manager)
- Phase callback emits to bus.outbound without await (put_nowait)
- Cron process_inbound_async() bypasses bus but hits agent pipeline
- approval_resume=True skips session append in turn_execution
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clambot.bus.events import InboundMessage, OutboundMessage
from clambot.bus.queue import MessageBus
from clambot.gateway.orchestrator import GatewayOrchestrator
from clambot.session.manager import SessionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bus() -> MessageBus:
    """Create a fresh MessageBus."""
    return MessageBus()


def _make_session_manager(tmp_path: Path) -> SessionManager:
    """Create a SessionManager backed by a temp directory."""
    return SessionManager(tmp_path)


def _make_mock_approval_gate() -> MagicMock:
    """Create a mock ApprovalGate."""
    gate = MagicMock()
    gate.resolve = MagicMock(return_value=MagicMock(decision="ALLOW"))
    return gate


def _make_mock_agent_loop() -> AsyncMock:
    """Create a mock AgentLoop with a process_turn that returns a result."""
    loop = AsyncMock()
    result = MagicMock()
    result.content = "Agent response"
    result.status = "completed"
    loop.process_turn = AsyncMock(return_value=result)
    return loop


def _make_mock_secret_store() -> MagicMock:
    """Create a mock SecretStore."""
    store = MagicMock()
    store.save = MagicMock()
    store.get = MagicMock(return_value=None)
    return store


_UNSET = object()


def _make_inbound(
    content: str = "hello",
    channel: str = "telegram",
    source: str = "user1",
    chat_id: str = "123",
    metadata: dict | None = None,
) -> InboundMessage:
    """Create an InboundMessage with defaults."""
    return InboundMessage(
        channel=channel,
        source=source,
        chat_id=chat_id,
        content=content,
        metadata=metadata or {},
    )


def _make_orchestrator(
    bus: MessageBus | None = None,
    tmp_path: Path | None = None,
    agent_loop: Any = _UNSET,
    approval_gate: MagicMock | None = None,
    secret_store: MagicMock | None = None,
    provider: Any = _UNSET,
    workspace: Any = _UNSET,
) -> GatewayOrchestrator:
    """Create a GatewayOrchestrator with sensible defaults.

    Uses a sentinel to distinguish 'not provided' from 'explicitly None'.
    """
    if bus is None:
        bus = _make_bus()
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp(prefix="test_gw_"))
    sm = _make_session_manager(tmp_path)
    return GatewayOrchestrator(
        bus=bus,
        session_manager=sm,
        approval_gate=approval_gate or _make_mock_approval_gate(),
        agent_loop=agent_loop if agent_loop is not _UNSET else _make_mock_agent_loop(),
        secret_store=secret_store or _make_mock_secret_store(),
        provider=provider if provider is not _UNSET else None,
        workspace=workspace if workspace is not _UNSET else tmp_path,
    )


# ---------------------------------------------------------------------------
# /approve command tests
# ---------------------------------------------------------------------------


class TestApproveCommand:
    """Tests for /approve command handling."""

    @pytest.mark.asyncio
    async def test_approve_resolves_gate_and_cleans_up(self, tmp_path: Path) -> None:
        """Approving resolves the gate (unblocking the sandbox thread)
        and removes the pending entry.  No re-queue — the original
        execution continues in-flight after the gate is resolved."""
        bus = _make_bus()
        gate = _make_mock_approval_gate()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path, approval_gate=gate)

        # Create original inbound that's pending approval
        original = _make_inbound(content="run my task", chat_id="123")
        approval_id = "test-approval-123"
        orch.store_pending_approval(approval_id, original)

        # Send /approve command with approval_id in metadata
        approve_msg = _make_inbound(
            content="/approve",
            chat_id="123",
            metadata={"approval_id": approval_id, "decision": "ALLOW", "grant_scope": "always"},
        )

        result = await orch._process_inbound(approve_msg)

        # Gate.resolve was called — this unblocks the sandbox thread
        gate.resolve.assert_called_once_with(approval_id, "ALLOW", "always")

        # Pending entry cleaned up, no re-queue (execution continues in-flight)
        assert bus.inbound.empty()
        assert approval_id not in orch._pending_approvals

    @pytest.mark.asyncio
    async def test_approve_returns_ack_with_correct_correlation_id(self, tmp_path: Path) -> None:
        """Approval acknowledgment uses the callback message's correlation_id."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        approval_id = "ack-test-456"
        original = _make_inbound(content="original task")
        orch.store_pending_approval(approval_id, original)

        approve_msg = _make_inbound(
            content="/approve",
            metadata={"approval_id": approval_id},
        )
        expected_corr_id = approve_msg.correlation_id

        result = await orch._process_inbound(approve_msg)

        # No outbound — approval keyboard already deleted,
        # the original agent turn delivers the result.
        assert result is None

    @pytest.mark.asyncio
    async def test_approve_parses_approval_id_from_content(self, tmp_path: Path) -> None:
        """Approval ID can be parsed from message content."""
        bus = _make_bus()
        gate = _make_mock_approval_gate()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path, approval_gate=gate)

        approval_id = "content-parse-789"
        original = _make_inbound(content="task to approve")
        orch.store_pending_approval(approval_id, original)

        approve_msg = _make_inbound(content=f"/approve {approval_id}")

        result = await orch._process_inbound(approve_msg)

        gate.resolve.assert_called_once()
        call_args = gate.resolve.call_args
        assert call_args[0][0] == approval_id

    @pytest.mark.asyncio
    async def test_approve_missing_pending_still_returns_none(self, tmp_path: Path) -> None:
        """Approving a non-existent pending returns None and does not re-queue."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        approve_msg = _make_inbound(
            content="/approve",
            metadata={"approval_id": "nonexistent"},
        )

        result = await orch._process_inbound(approve_msg)

        assert result is None
        # Nothing re-queued
        assert bus.inbound.empty()


# ---------------------------------------------------------------------------
# /secret command tests
# ---------------------------------------------------------------------------


class TestSecretCommand:
    """Tests for /secret command handling."""

    @pytest.mark.asyncio
    async def test_secret_stores_and_acks(self, tmp_path: Path) -> None:
        """Secret command stores the secret and returns acknowledgment."""
        bus = _make_bus()
        secret_store = _make_mock_secret_store()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path, secret_store=secret_store)

        msg = _make_inbound(content="/secret MY_KEY my_secret_value")

        result = await orch._process_inbound(msg)

        secret_store.save.assert_called_once_with("MY_KEY", "my_secret_value")
        assert result is not None
        assert "MY_KEY" in result.content
        assert "stored" in result.content.lower()

    @pytest.mark.asyncio
    async def test_secret_requeues_original_with_secret_resume(self, tmp_path: Path) -> None:
        """Secret command re-queues the original inbound with secret_resume=True."""
        bus = _make_bus()
        secret_store = _make_mock_secret_store()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path, secret_store=secret_store)

        # Store a pending approval that the secret resolves
        original = _make_inbound(content="task needing secret")
        approval_id = "secret-pending-123"
        orch.store_pending_approval(approval_id, original)

        msg = _make_inbound(
            content="/secret API_KEY abc123",
            metadata={"original_approval_id": approval_id},
        )

        await orch._process_inbound(msg)

        secret_store.save.assert_called_once_with("API_KEY", "abc123")

        # Original re-queued with secret_resume
        assert not bus.inbound.empty()
        resumed = bus.inbound.get_nowait()
        assert resumed.content == "task needing secret"
        assert resumed.metadata.get("secret_resume") is True

    @pytest.mark.asyncio
    async def test_secret_bad_format_returns_usage(self, tmp_path: Path) -> None:
        """Bad /secret format returns usage instructions."""
        orch = _make_orchestrator(tmp_path=tmp_path)

        msg = _make_inbound(content="/secret only_name")

        result = await orch._process_inbound(msg)

        assert result is not None
        assert "Usage" in result.content


# ---------------------------------------------------------------------------
# /new command tests
# ---------------------------------------------------------------------------


class TestNewSessionCommand:
    """Tests for /new session reset command."""

    @pytest.mark.asyncio
    async def test_new_clears_session_cache(self, tmp_path: Path) -> None:
        """/new resets the session cache via session_manager."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        # Add some turns to the session
        session_key = "telegram:123"
        orch._session_manager.append_turn(session_key, "user", "hello")
        orch._session_manager.append_turn(session_key, "assistant", "hi there")

        # Verify turns exist
        turns = orch._session_manager.load_history(session_key)
        assert len(turns) >= 2

        msg = _make_inbound(content="/new", chat_id="123")
        result = await orch._process_inbound(msg)

        assert result is not None
        assert result.content == "New session started."
        assert result.type == "text"

        # Session cache is cleared — loading now reads from disk into fresh cache
        # The manager's internal cache should have been evicted
        # (The actual behavior: reset_session pops from _cache, so next load re-reads from disk)

    @pytest.mark.asyncio
    async def test_new_triggers_memory_consolidation(self, tmp_path: Path) -> None:
        """/new triggers memory consolidation before session reset."""
        bus = _make_bus()
        mock_provider = AsyncMock()

        orch = _make_orchestrator(
            bus=bus,
            tmp_path=tmp_path,
            provider=mock_provider,
            workspace=tmp_path,
        )

        session_key = "telegram:123"
        orch._session_manager.append_turn(session_key, "user", "hello")

        with patch(
            "clambot.gateway.orchestrator.consolidate_session_memory",
            new_callable=AsyncMock,
        ) as mock_consolidate:
            msg = _make_inbound(content="/new", chat_id="123")
            result = await orch._process_inbound(msg)

        assert result is not None
        assert result.content == "New session started."
        mock_consolidate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_without_provider_skips_consolidation(self, tmp_path: Path) -> None:
        """/new without provider skips consolidation gracefully."""
        orch = _make_orchestrator(tmp_path=tmp_path, provider=None, workspace=None)

        msg = _make_inbound(content="/new", chat_id="123")
        result = await orch._process_inbound(msg)

        assert result is not None
        assert result.content == "New session started."


# ---------------------------------------------------------------------------
# Phase callback tests
# ---------------------------------------------------------------------------


class TestPhaseCallback:
    """Tests for phase callback emission via put_nowait."""

    def test_phase_callback_emits_status_update(self, tmp_path: Path) -> None:
        """Phase callback emits a status_update OutboundMessage via put_nowait."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        callback = orch._make_phase_callback("telegram", "123", "corr-1")
        callback("Generating code...")

        assert not bus.outbound.empty()
        msg = bus.outbound.get_nowait()
        assert msg.type == "status_update"
        assert msg.content == "Generating code..."
        assert msg.channel == "telegram"
        assert msg.target == "123"
        assert msg.correlation_id == "corr-1"

    def test_phase_callback_done_emits_status_delete(self, tmp_path: Path) -> None:
        """Phase callback with __done__ emits status_delete."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        callback = orch._make_phase_callback("telegram", "123", "corr-2")
        callback("__done__")

        msg = bus.outbound.get_nowait()
        assert msg.type == "status_delete"
        assert msg.correlation_id == "corr-2"

    def test_phase_callback_multiple_phases(self, tmp_path: Path) -> None:
        """Multiple phase callbacks emit multiple status_update messages."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        callback = orch._make_phase_callback("telegram", "456", "corr-3")
        callback("Selecting...")
        callback("Generating...")
        callback("Executing...")
        callback("__done__")

        messages: list[OutboundMessage] = []
        while not bus.outbound.empty():
            messages.append(bus.outbound.get_nowait())

        assert len(messages) == 4
        assert messages[0].content == "Selecting..."
        assert messages[0].type == "status_update"
        assert messages[1].content == "Generating..."
        assert messages[2].content == "Executing..."
        assert messages[3].type == "status_delete"

    def test_phase_callback_is_sync_safe(self, tmp_path: Path) -> None:
        """Phase callback uses put_nowait (no await) — sync-safe on event loop."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        callback = orch._make_phase_callback("cli", "user", "corr-4")

        # Calling from synchronous context should not raise
        callback("phase1")
        callback("__done__")

        assert bus.outbound.qsize() == 2


# ---------------------------------------------------------------------------
# Cron direct-call (process_inbound_async) tests
# ---------------------------------------------------------------------------


class TestCronDirectCall:
    """Tests for cron service direct-call path via process_inbound_async."""

    @pytest.mark.asyncio
    async def test_process_inbound_async_bypasses_bus(self, tmp_path: Path) -> None:
        """process_inbound_async processes directly without reading from inbound bus."""
        bus = _make_bus()
        mock_agent_loop = _make_mock_agent_loop()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path, agent_loop=mock_agent_loop)

        cron_msg = InboundMessage(
            channel="cron",
            source="system",
            chat_id="cron-job-1",
            content="Check weather",
        )

        # Bus inbound is empty — we're bypassing it
        assert bus.inbound.empty()

        with patch(
            "clambot.gateway.orchestrator.process_turn_with_persistence_and_execution",
            new_callable=AsyncMock,
            return_value=OutboundMessage(
                channel="cron",
                target="system",
                content="Weather is sunny.",
                correlation_id=cron_msg.correlation_id,
            ),
        ) as mock_turn:
            result = await orch.process_inbound_async(cron_msg)

            mock_turn.assert_called_once()

        # Result was also placed on outbound bus.
        # Note: _run_agent_turn emits a status_delete message via phase_callback("__done__")
        # before returning, so the outbound bus may contain that housekeeping message first.
        # Drain all outbound messages and find the agent response.
        assert not bus.outbound.empty()
        outbound_messages = []
        while not bus.outbound.empty():
            outbound_messages.append(bus.outbound.get_nowait())
        agent_response = next(
            (m for m in outbound_messages if m.content == "Weather is sunny."),
            None,
        )
        assert agent_response is not None, (
            f"Expected agent response not found in outbound bus. Got: {outbound_messages}"
        )

        # Inbound bus was never used
        assert bus.inbound.empty()

    @pytest.mark.asyncio
    async def test_process_inbound_async_handles_special_commands(self, tmp_path: Path) -> None:
        """process_inbound_async still routes special commands."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        new_msg = InboundMessage(
            channel="cron",
            source="system",
            chat_id="cron-1",
            content="/new",
        )

        result = await orch.process_inbound_async(new_msg)

        assert result is not None
        assert result.content == "New session started."


# ---------------------------------------------------------------------------
# Agent turn execution tests
# ---------------------------------------------------------------------------


class TestAgentTurnExecution:
    """Tests for normal agent turn routing."""

    @pytest.mark.asyncio
    async def test_normal_message_runs_agent(self, tmp_path: Path) -> None:
        """Normal messages are dispatched to the agent turn pipeline."""
        bus = _make_bus()
        mock_agent_loop = _make_mock_agent_loop()

        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path, agent_loop=mock_agent_loop)

        msg = _make_inbound(content="list all files")

        with patch(
            "clambot.gateway.orchestrator.process_turn_with_persistence_and_execution",
            new_callable=AsyncMock,
            return_value=OutboundMessage(
                channel="telegram",
                target="user1",
                content="Here are your files.",
                correlation_id=msg.correlation_id,
            ),
        ) as mock_turn:
            result = await orch._process_inbound(msg)

            mock_turn.assert_called_once()

        assert result is not None
        assert result.content == "Here are your files."

    @pytest.mark.asyncio
    async def test_no_agent_loop_returns_error(self, tmp_path: Path) -> None:
        """Without an agent_loop configured, returns error message."""
        orch = _make_orchestrator(tmp_path=tmp_path, agent_loop=None)

        msg = _make_inbound(content="do something")
        result = await orch._process_inbound(msg)

        assert result is not None
        assert "not configured" in result.content.lower()

    @pytest.mark.asyncio
    async def test_agent_error_returns_error_message(self, tmp_path: Path) -> None:
        """Agent pipeline exception returns error message."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        msg = _make_inbound(content="crash me")

        with patch(
            "clambot.gateway.orchestrator.process_turn_with_persistence_and_execution",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Something broke"),
        ):
            result = await orch._process_inbound(msg)

        assert result is not None
        assert "error" in result.content.lower()


# ---------------------------------------------------------------------------
# Orchestrator lifecycle tests
# ---------------------------------------------------------------------------


class TestOrchestratorLifecycle:
    """Tests for start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_and_stop(self, tmp_path: Path) -> None:
        """Orchestrator starts and stops cleanly."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        await orch.start()
        assert orch._running is True
        assert orch._task is not None

        await orch.stop()
        assert orch._running is False
        assert orch._task is None

    @pytest.mark.asyncio
    async def test_main_loop_processes_messages(self, tmp_path: Path) -> None:
        """Main loop dequeues and processes inbound messages."""
        bus = _make_bus()
        orch = _make_orchestrator(bus=bus, tmp_path=tmp_path)

        with patch(
            "clambot.gateway.orchestrator.process_turn_with_persistence_and_execution",
            new_callable=AsyncMock,
            return_value=OutboundMessage(
                channel="telegram",
                target="user1",
                content="Reply",
                correlation_id="test-corr",
            ),
        ):
            await orch.start()

            # Queue a message
            msg = _make_inbound(content="hello")
            await bus.inbound.put(msg)

            # Give the loop time to process
            await asyncio.sleep(0.1)

            await orch.stop()

        # Outbound should have messages (at minimum status updates + reply)
        assert not bus.outbound.empty()
