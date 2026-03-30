"""Gateway orchestrator — central coordinator for message routing, approvals, and agent execution.

Routes inbound messages from channels to the agent pipeline:
  - Special commands: ``/approve``, ``/secret``, ``/new``
  - Normal messages: run through the full agent turn pipeline
  - Phase callbacks: status updates emitted via ``put_nowait`` to the outbound bus
  - Cron: ``process_inbound_async()`` called directly (bypasses bus)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from clambot.agent.protocols import SecretStoreProtocol, ToolRegistryProtocol
from clambot.agent.turn_execution import process_turn_with_persistence_and_execution
from clambot.bus.events import InboundMessage, OutboundMessage
from clambot.bus.queue import MessageBus
from clambot.memory.consolidation import consolidate_session_memory
from clambot.providers.base import LLMProvider
from clambot.session.history import turns_to_llm_history
from clambot.session.manager import SessionManager
from clambot.utils.tasks import tracked_task

logger = logging.getLogger(__name__)


class GatewayOrchestrator:
    """Central message coordinator for the gateway.

    Dequeues inbound messages, routes special commands, and dispatches
    normal messages to the agent pipeline via ``process_turn_with_persistence_and_execution``.
    """

    def __init__(
        self,
        bus: MessageBus,
        session_manager: SessionManager,
        approval_gate: Any,
        tool_registry: ToolRegistryProtocol | None = None,
        config: Any | None = None,
        agent_loop: Any | None = None,
        secret_store: SecretStoreProtocol | None = None,
        provider: LLMProvider | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._bus = bus
        self._session_manager = session_manager
        self._approval_gate = approval_gate
        self._tool_registry = tool_registry
        self._config = config
        self._agent_loop = agent_loop
        self._secret_store = secret_store
        self._provider = provider
        self._workspace = workspace

        # Pending approvals: approval_id -> original InboundMessage
        self._pending_approvals: dict[str, InboundMessage] = {}

        # Main loop task
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Start the orchestrator main loop."""
        self._running = True
        self._task = tracked_task(self._run(), name="orchestrator-run")
        logger.info("GatewayOrchestrator started")

    async def stop(self) -> None:
        """Stop the orchestrator main loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("GatewayOrchestrator stopped")

    # ── Main loop ─────────────────────────────────────────────

    async def _run(self) -> None:
        """Main loop: dequeue inbound → dispatch concurrently.

        Processing is concurrent so that ``/approve`` and ``/secret``
        commands (which resolve pending operations) are never blocked
        behind an in-progress agent turn.
        """
        while self._running:
            try:
                msg = await self._bus.inbound.get()
                tracked_task(self._process_and_emit(msg), name="process-inbound")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Error processing inbound message: %s", exc)

    async def _process_and_emit(self, msg: InboundMessage) -> None:
        """Process one inbound message and emit the outbound result."""
        try:
            outbound = await self._process_inbound(msg)
            if outbound is not None:
                self._bus.outbound.put_nowait(outbound)
        except Exception as exc:
            logger.exception("Error processing inbound message: %s", exc)

    # ── Public entry point (cron bypass) ──────────────────────

    async def process_inbound_async(self, inbound: InboundMessage) -> OutboundMessage | None:
        """Process an inbound message directly (bypasses bus).

        Called by cron service to inject messages without queueing.
        The result is placed onto the outbound bus automatically.
        """
        outbound = await self._process_inbound(inbound)
        if outbound is not None:
            self._bus.outbound.put_nowait(outbound)
        return outbound

    # ── Command routing ───────────────────────────────────────

    async def _process_inbound(self, inbound: InboundMessage) -> OutboundMessage | None:
        """Route an inbound message to the appropriate handler."""
        content = inbound.content.strip()
        metadata = inbound.metadata or {}

        # /approve command — resolve a pending approval
        if content.startswith("/approve") or metadata.get("approval_id"):
            return await self._handle_approval_command(inbound)

        # /secret command — store a secret and resume
        if content.startswith("/secret "):
            return await self._handle_secret_command(inbound)

        # /new command — consolidate memory and reset session
        if content.strip() == "/new":
            return await self._handle_new_session(inbound)

        # Plain message while a secret is pending — treat as the secret value
        if not content.startswith("/"):
            pending_secret = self._find_pending_secret()
            if pending_secret is not None:
                secret_name, _ = pending_secret
                # Rewrite as a /secret command so the handler does all the work
                rewritten = replace(
                    inbound,
                    content=f"/secret {secret_name} {content}",
                )
                return await self._handle_secret_command(rewritten)

        # Normal message — run agent turn
        return await self._run_agent_turn(inbound)

    # ── /approve handler ──────────────────────────────────────

    async def _handle_approval_command(self, msg: InboundMessage) -> OutboundMessage | None:
        """Handle an approval command.

        Resolves the pending approval gate so the original execution
        continues in-flight.  No response message is sent — the
        approval keyboard has already been deleted by the Telegram
        channel, and the original turn will deliver the final result.
        """
        metadata = msg.metadata or {}
        approval_id = metadata.get("approval_id", "")
        grant_scope = metadata.get("grant_scope", "")

        # Parse from content if not in metadata
        if not approval_id:
            parts = msg.content.strip().split()
            # /approve <approval_id> <decision> [grant_scope]
            if len(parts) >= 2:
                approval_id = parts[1]

        # Determine decision
        decision = metadata.get("decision", "ALLOW")

        # Resolve the approval gate — this unblocks the sandbox thread
        # that is waiting on wait_for_resolution(), so the original
        # execution continues automatically.  No re-queue needed.
        if self._approval_gate is not None:
            self._approval_gate.resolve(approval_id, decision, grant_scope)

        # Clean up the stored inbound (no re-queue — execution continues
        # in-flight after the gate is resolved).
        self._pending_approvals.pop(approval_id, None)

        # No outbound — the original agent turn delivers the result.
        return None

    # ── /secret handler ───────────────────────────────────────

    async def _handle_secret_command(self, msg: InboundMessage) -> OutboundMessage | None:
        """Handle a secret storage command.

        Stores the secret, looks up any pending request that was waiting for
        this secret, re-queues it with ``secret_resume=True``, and returns
        an acknowledgment.
        """
        parts = msg.content.strip().split(maxsplit=2)
        # /secret <name> <value>
        if len(parts) < 3:
            return OutboundMessage(
                channel=msg.channel,
                target=msg.chat_id,
                content="Usage: /secret <name> <value>",
                type="text",
                correlation_id=msg.correlation_id,
            )

        name = parts[1]
        value = parts[2]

        # Store the secret
        if self._secret_store is not None:
            self._secret_store.save(name, value)

        # Look up a pending request that was waiting for this secret.
        # Try: exact key from metadata, then scan for any key containing
        # this secret name (set by the secret_pending event handler).
        original: InboundMessage | None = None
        metadata = msg.metadata or {}
        original_key = metadata.get("original_approval_id", "")
        if original_key:
            original = self._pending_approvals.pop(original_key, None)

        if original is None:
            # Scan pending approvals for a secret key that includes this name
            for key in list(self._pending_approvals):
                if key.startswith("secret:") and name in key:
                    original = self._pending_approvals.pop(key)
                    break

        if original is not None:
            import uuid

            resumed = replace(
                original,
                correlation_id=str(uuid.uuid4()),
                metadata={**original.metadata, "secret_resume": True},
            )
            self._bus.inbound.put_nowait(resumed)

        ack = f"Secret '{name}' stored."
        if original is not None:
            ack += " Re-running your request..."

        return OutboundMessage(
            channel=msg.channel,
            target=msg.chat_id,
            content=ack,
            type="text",
            correlation_id=msg.correlation_id,
        )

    def _find_pending_secret(self) -> tuple[str, InboundMessage] | None:
        """Return ``(secret_name, original_inbound)`` if a secret request
        is pending, else ``None``."""
        for key, inbound in self._pending_approvals.items():
            if key.startswith("secret:"):
                # key is "secret:NAME1:NAME2:..." — extract first name
                name = key.removeprefix("secret:").split(":")[0]
                return (name, inbound)
        return None

    # ── /new handler ──────────────────────────────────────────

    async def _handle_new_session(self, msg: InboundMessage) -> OutboundMessage | None:
        """Handle the /new command.

        Triggers memory consolidation, then starts a fresh session.

        - **Success path**: facts consolidated into MEMORY.md / HISTORY.md,
          then ``clear_session()`` truncates the JSONL so the next
          conversation starts empty.
        - **Failure path**: the JSONL is left intact (``reset_session()``
          evicts only the cache) so no data is lost, and the user is warned.
        """
        session_key = msg.session_key or f"{msg.channel}:{msg.chat_id}"

        # Trigger memory consolidation before reset
        consolidation_ok = True
        if self._provider and self._workspace:
            try:
                turns = self._session_manager.load_history(session_key)
                llm_turns = turns_to_llm_history(turns)
                await consolidate_session_memory(llm_turns, self._workspace, self._provider)
            except Exception as exc:
                logger.warning("Memory consolidation failed: %s", exc)
                consolidation_ok = False

        if consolidation_ok:
            # Safe to wipe — facts are persisted in MEMORY.md / HISTORY.md
            self._session_manager.clear_session(session_key)
            reply = "New session started."
        else:
            # Keep the JSONL intact so no conversation data is lost
            self._session_manager.reset_session(session_key)
            reply = (
                "New session started, but memory consolidation failed — "
                "some context from the previous session may not have been "
                "saved to long-term memory."
            )

        return OutboundMessage(
            channel=msg.channel,
            target=msg.chat_id,
            content=reply,
            type="text",
            correlation_id=msg.correlation_id,
        )

    # ── Agent turn execution ──────────────────────────────────

    async def _run_agent_turn(self, inbound: InboundMessage) -> OutboundMessage | None:
        """Run a full agent turn for a normal message."""
        if self._agent_loop is None:
            return OutboundMessage(
                channel=inbound.channel,
                target=inbound.chat_id,
                content="Agent not configured.",
                type="text",
                correlation_id=inbound.correlation_id,
            )

        phase_callback = self._make_phase_callback(
            inbound.channel, inbound.chat_id, inbound.correlation_id
        )

        # Track tool calls that were approved during this turn so
        # re-queued runs (after /secret) don't need re-approval.
        approved_tool_calls: list[dict[str, Any]] = []

        def on_event(event: dict[str, Any]) -> None:
            """Handle events from the agent pipeline."""
            event_type = event.get("type", "")

            # Emit approval pending to outbound
            if event_type == "approval_pending":
                approval_id = event.get("approval_id", "")
                self._pending_approvals[approval_id] = inbound

                # Record tool call for potential re-approval in re-queued runs
                approved_tool_calls.append(
                    {
                        "tool": event.get("tool_name", ""),
                        "scope": "*",
                    }
                )

                self._bus.outbound.put_nowait(
                    OutboundMessage(
                        channel=inbound.channel,
                        target=inbound.chat_id,
                        content=event.get("message", "Approval required."),
                        type="approval_pending",
                        correlation_id=inbound.correlation_id,
                        metadata=event,
                    )
                )

            # Secret pending — store the original inbound for /secret re-queue.
            # The outbound message is handled by turn_execution's return value
            # to avoid duplicate messages.
            elif event_type == "secret_pending":
                missing = event.get("missing_secrets", [])
                secret_key = f"secret:{':'.join(missing)}"
                self._pending_approvals[secret_key] = inbound

                # Persist tool approvals from this turn so re-queued
                # runs don't need the user to approve again.
                if approved_tool_calls and self._approval_gate is not None:
                    for grant in approved_tool_calls:
                        self._approval_gate.store.add_always_grant(
                            grant["tool"],
                            scope=grant["scope"],
                            fingerprint="",
                        )

            elif event_type == "progress":
                phase_callback(event.get("state", ""))

        try:
            outbound = await process_turn_with_persistence_and_execution(
                inbound=inbound,
                agent_loop=self._agent_loop,
                session_manager=self._session_manager,
                config=self._config,
                provider=self._provider,
                workspace=self._workspace,
                on_event=on_event,
            )

            # Emit final phase callback to delete status message
            phase_callback("__done__")

            return outbound

        except Exception as exc:
            logger.exception("Agent turn failed: %s", exc)
            phase_callback("__done__")
            return OutboundMessage(
                channel=inbound.channel,
                target=inbound.chat_id,
                content=f"An error occurred: {exc}",
                type="text",
                correlation_id=inbound.correlation_id,
            )

    # ── Phase callback factory ────────────────────────────────

    def _make_phase_callback(
        self,
        channel: str,
        chat_id: str,
        correlation_id: str,
    ) -> Callable[[str], None]:
        """Create a phase callback that emits status updates to the outbound bus.

        Uses ``put_nowait`` (sync-safe) — no ``await`` needed.
        Tracks a status_message_id per correlation_id for edit-in-place.
        """
        status_tracker: dict[str, str] = {}

        def callback(phase: str) -> None:
            if phase == "__done__":
                # Emit delete-status message
                self._bus.outbound.put_nowait(
                    OutboundMessage(
                        channel=channel,
                        target=chat_id,
                        content="",
                        type="status_delete",
                        correlation_id=correlation_id,
                        metadata={"status_tracker": status_tracker},
                    )
                )
            else:
                # Emit status_update message
                self._bus.outbound.put_nowait(
                    OutboundMessage(
                        channel=channel,
                        target=chat_id,
                        content=phase,
                        type="status_update",
                        correlation_id=correlation_id,
                        metadata={"status_tracker": status_tracker},
                    )
                )

        return callback

    # ── Pending approvals access ──────────────────────────────

    def store_pending_approval(self, approval_id: str, inbound: InboundMessage) -> None:
        """Store an inbound message as pending approval (used by tests and external callers)."""
        self._pending_approvals[approval_id] = inbound

    def get_pending_approval(self, approval_id: str) -> InboundMessage | None:
        """Get a pending approval's original inbound message."""
        return self._pending_approvals.get(approval_id)
