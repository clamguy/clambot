"""Gateway main — ordered startup and shutdown for the full ClamBot daemon.

``gateway_main()`` is the entry-point coroutine that wires together all
subsystems in the correct order, then blocks until a shutdown signal
(``SIGINT`` / ``SIGTERM``) is received.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from pathlib import Path
from typing import Any

from clambot.agent.approval_gate import ApprovalGate
from clambot.agent.bootstrap import build_provider_backed_agent_loop_from_config
from clambot.agent.runtime import ClamRuntime
from clambot.agent.runtime_backend_amla_sandbox import AmlaSandboxRuntimeBackend
from clambot.bus.events import InboundMessage
from clambot.bus.queue import MessageBus
from clambot.channels.manager import ChannelManager
from clambot.config.loader import load_config, resolve_config_path
from clambot.config.schema import ClamBotConfig
from clambot.cron.service import (
    InMemoryCronService,
    NotConfiguredCronService,
    configure_cron_tool_runtime_sync_hook,
)
from clambot.gateway.orchestrator import GatewayOrchestrator
from clambot.heartbeat.service import (
    InMemoryHeartbeatService,
    NotConfiguredHeartbeatService,
)
from clambot.providers.factory import create_provider
from clambot.session.manager import SessionManager
from clambot.tools import build_tool_registry
from clambot.tools.secrets.store import SecretStore
from clambot.utils.tasks import tracked_task
from clambot.workspace.bootstrap import bootstrap_workspace
from clambot.workspace.retention import prune_session_logs

__all__ = ["gateway_main"]

logger = logging.getLogger(__name__)


def _build_cron_executor(
    agent_loop: Any,
    orchestrator: GatewayOrchestrator,
    bus: MessageBus,
    *,
    default_channel: str | None = None,
    default_target: str | None = None,
) -> Any:
    """Build the async executor callback for the cron service.

    Three execution modes, tried in order:

    1. **Direct clam execution** (``payload.clam_id`` is set):
       Load the promoted clam and run it via
       ``agent_loop.execute_clam_direct()`` — fast, no LLM calls.

    2. **Generate-and-cache** (no ``clam_id``, ``message`` is set):
       Run the full agent pipeline once to find or generate a clam.
       If the pipeline produces a ``clam_name``, it is cached in the
       job so that future fires use the fast path (1).

    3. **Error** (neither ``clam_id`` nor ``message``): skip with a
       log error.

    Args:
        agent_loop: The configured ``AgentLoop`` (direct execution).
        orchestrator: Gateway orchestrator (full pipeline for
            first-run generation).
        bus: Message bus for delivering direct-execution results.
        default_channel: Fallback channel name.
        default_target: Fallback target (chat_id).
    """

    async def executor(job: Any) -> str | None:
        from clambot.bus.events import OutboundMessage

        payload = job.payload
        channel = payload.channel or default_channel or "cron"
        target = payload.target or default_target or "cron"

        # ── 1. Direct clam execution (fast path) ─────────────
        if payload.clam_id:
            result = await agent_loop.execute_clam_direct(
                clam_id=payload.clam_id,
            )
            content = result.content if result else None

            if content:
                bus.outbound.put_nowait(
                    OutboundMessage(
                        channel=channel,
                        target=target,
                        content=content,
                        type="text",
                    )
                )
            return content

        # ── 2. Generate clam via full pipeline (first run) ────
        if payload.message:
            inbound = InboundMessage(
                channel=channel,
                source="cron",
                chat_id=target,
                content=payload.message,
                metadata={"cron_job_id": job.id, "cron_job_name": job.name},
            )
            result = await orchestrator.process_inbound_async(inbound)

            # Cache the promoted clam so future runs use the fast
            # direct-execution path.
            if result is not None:
                clam_name = (result.metadata or {}).get("clam_name")
                if clam_name:
                    payload.clam_id = clam_name
                    logger.info(
                        "Cron job '%s': resolved clam '%s' — future runs will use direct execution",
                        job.name,
                        clam_name,
                    )

            return result.content if result else None

        # ── 3. Nothing to do ─────────────────────────────────
        logger.error(
            "Cron job '%s' (%s) has neither clam_id nor message — skipping.",
            job.name,
            job.id,
        )
        return None

    return executor


async def gateway_main(config: ClamBotConfig | None = None, config_path: str | None = None) -> None:
    """Start the full ClamBot gateway daemon.

    Ordered startup:
        1. Bootstrap workspace
        2. Build tool registry
        3. Create approval gate
        4. Create session manager
        5. Create cron service + load jobs
        6. Wire cron tool sync hook
        7. Create channel manager + Telegram (if enabled)
        8. Build agent loop
        9. Create and start orchestrator
        10. Start channel manager
        11. Start cron service
        12. Start heartbeat service (if enabled)
        13. Block until shutdown signal

    Shutdown: reverse order, each step suppresses exceptions.
    """
    # Resolve config
    if config is None:
        resolved_path = resolve_config_path(config_path)
        config = load_config(resolved_path)
    else:
        resolved_path = resolve_config_path(config_path)

    workspace = Path(config.agents.defaults.workspace).expanduser()

    # -- 1. Bootstrap workspace -----------------------------------------
    bootstrap_workspace(workspace)
    logger.info("Workspace bootstrapped at %s", workspace)

    # -- 2. Build tool registry ----------------------------------------
    secrets_dir = workspace / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secret_store = SecretStore(secrets_dir / "secrets.json")

    tool_registry = build_tool_registry(
        workspace=workspace,
        config=config,
        secret_store=secret_store,
        disabled_tools=config.agents.defaults.disabled_tools,
        available_tools=config.agents.defaults.available_tools,
    )
    logger.info("Tool registry built with %d tools", len(tool_registry))

    # -- 3. Create approval gate ----------------------------------------
    approval_gate = ApprovalGate(
        approvals_config=config.agents.approvals,
        config_path=resolved_path,
    )

    # -- 4. Create session manager --------------------------------------
    session_manager = SessionManager(workspace)

    # -- 5. Create cron service -----------------------------------------
    cron_path = workspace / "cron" / "jobs.json"
    cron_path.parent.mkdir(parents=True, exist_ok=True)

    if config.cron.enabled:
        cron_service = InMemoryCronService(
            store_path=cron_path,
            workspace=workspace,
        )
        await cron_service.start()
    else:
        cron_service = NotConfiguredCronService()

    # -- 6. Determine primary channel for cron/background job output -----
    primary_channel_name: str | None = None
    primary_channel_target: str | None = None
    if config.channels.telegram.enabled and config.channels.telegram.token:
        primary_channel_name = "telegram"
        # For private chats the user-id equals the chat-id, so the first
        # numeric entry in allow_from is a usable default target.
        for entry in config.channels.telegram.allow_from:
            if entry.lstrip("-").isdigit():
                primary_channel_target = entry
                break

    # -- 6b. Wire cron tool sync hook -----------------------------------
    cron_tool = tool_registry.get_tool("cron")
    if cron_tool is not None and isinstance(cron_service, InMemoryCronService):
        configure_cron_tool_runtime_sync_hook(
            cron_tool,
            cron_service,
            default_channel=primary_channel_name,
            default_target=primary_channel_target,
        )

    # -- 7. Create message bus + channel manager + Telegram -------------
    bus = MessageBus()
    channel_manager = ChannelManager(bus)

    telegram_channel = None
    if config.channels.telegram.enabled and config.channels.telegram.token:
        try:
            from clambot.channels.telegram import TelegramChannel

            telegram_channel = TelegramChannel(config.channels.telegram, bus, workspace=workspace)
            channel_manager.register(telegram_channel)
            logger.info("Telegram channel registered")
        except Exception as exc:
            logger.warning("Failed to create Telegram channel: %s", exc)

    # -- 8. Build agent loop -------------------------------------------
    primary_provider = create_provider(config)

    # Build runtime
    def _tool_handler(tool_name: str, args: dict) -> Any:
        tool = tool_registry.get_tool(tool_name)
        if tool is None:
            return {"error": f"Unknown tool: {tool_name}"}
        return tool.execute(args)

    runtime_backend = AmlaSandboxRuntimeBackend(tool_handler=_tool_handler)
    runtime = ClamRuntime(
        backend=runtime_backend,
        approval_gate=approval_gate,
        tool_registry=tool_registry,
        config=config,
    )

    agent_loop = build_provider_backed_agent_loop_from_config(
        config=config,
        tool_registry=tool_registry,
        runtime=runtime,
        workspace=workspace,
    )

    # Inject cron service so the agent loop can schedule jobs directly
    if isinstance(cron_service, InMemoryCronService):
        agent_loop.set_cron_service(cron_service)

    # -- 9. Create and start orchestrator --------------------------------
    orchestrator = GatewayOrchestrator(
        bus=bus,
        session_manager=session_manager,
        approval_gate=approval_gate,
        tool_registry=tool_registry,
        config=config,
        agent_loop=agent_loop,
        secret_store=secret_store,
        provider=primary_provider,
        workspace=workspace,
    )
    await orchestrator.start()

    # -- 10. Start channel manager --------------------------------------
    await channel_manager.start()

    # -- 11. Wire and start cron executor -------------------------------
    if isinstance(cron_service, InMemoryCronService):
        cron_service.set_executor(
            _build_cron_executor(
                agent_loop,
                orchestrator,
                bus,
                default_channel=primary_channel_name,
                default_target=primary_channel_target,
            )
        )
        cron_task = tracked_task(cron_service._run(), name="cron-service")
    else:
        cron_task = None

    # -- 12. Start heartbeat service ------------------------------------
    if config.heartbeat.enabled:
        heartbeat_service = InMemoryHeartbeatService(
            config=config.heartbeat,
            workspace=workspace,
        )
        heartbeat_service.set_executor(orchestrator.process_inbound_async)
        await heartbeat_service.start()
    else:
        heartbeat_service = NotConfiguredHeartbeatService()

    # -- Prune old session logs -----------------------------------------
    sessions_dir = workspace / "sessions"
    if sessions_dir.is_dir():
        prune_session_logs(sessions_dir)

    # -- Keep alive until shutdown signal --------------------------------
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    logger.info("Gateway started — waiting for messages")
    await stop_event.wait()

    # -- Shutdown (reverse order) ----------------------------------------
    logger.info("Shutting down gateway...")

    with suppress(Exception):
        if isinstance(heartbeat_service, InMemoryHeartbeatService):
            await heartbeat_service.stop()

    with suppress(Exception):
        if cron_task is not None:
            cron_task.cancel()
            try:
                await cron_task
            except asyncio.CancelledError:
                pass

    with suppress(Exception):
        if isinstance(cron_service, InMemoryCronService):
            await cron_service.stop()

    with suppress(Exception):
        await channel_manager.stop()

    with suppress(Exception):
        await orchestrator.stop()

    logger.info("Gateway shutdown complete")
