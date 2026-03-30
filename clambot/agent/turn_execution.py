"""Turn execution — session persistence + agent loop execution.

Wraps the agent loop with session management:
  1. Load session history
  2. Run auto-compaction if needed
  3. Execute agent turn
  4. Persist turns to session
  5. Fire-and-forget durable fact extraction
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clambot.bus.events import InboundMessage, OutboundMessage
from clambot.session.manager import SessionManager
from clambot.utils.tasks import tracked_task

from .loop import AgentLoop

logger = logging.getLogger(__name__)

# Guards read-modify-write on MEMORY.md in background fact extraction
_memory_write_lock = asyncio.Lock()


async def process_turn_with_persistence_and_execution(
    inbound: InboundMessage,
    agent_loop: AgentLoop,
    session_manager: SessionManager,
    config: Any | None = None,
    provider: Any | None = None,
    workspace: Path | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    secret_prompt_callback: Callable[[list[str]], dict[str, str]] | None = None,
    secret_store: Any | None = None,
) -> OutboundMessage:
    """Execute an agent turn with full session persistence.

    Steps:
        1. Load session history
        2. Run auto-compaction if threshold exceeded
        3. Execute agent turn via AgentLoop
        3b. If secret_pending and secret_prompt_callback provided,
            prompt for secrets, store them, and re-run the turn
        4. Append user + assistant turns to session (skip if approval_resume)
        5. Schedule background durable fact extraction
        6. Return OutboundMessage

    Args:
        inbound: The inbound message to process.
        agent_loop: The configured AgentLoop instance.
        session_manager: Session manager for history persistence.
        config: ClamBot config.
        provider: LLM provider for compaction/fact extraction.
        workspace: Workspace path for memory operations.
        on_event: Optional callback for progress events.
        secret_prompt_callback: Optional callback that receives a list of
            missing secret names and returns a dict of ``{name: value}``
            pairs.  Used by the CLI to interactively prompt via getpass.
        secret_store: Optional SecretStore to persist prompted secrets.

    Returns:
        An OutboundMessage with the agent's response.
    """
    session_key = inbound.session_key or f"{inbound.channel}:{inbound.source}"
    metadata = inbound.metadata or {}
    is_approval_resume = metadata.get("approval_resume", False)
    is_secret_resume = metadata.get("secret_resume", False)

    # ── 1. Load session history ───────────────────────────────
    turns = session_manager.load_history(session_key)

    # Convert to LLM format
    from clambot.session.history import turns_to_llm_history

    history = turns_to_llm_history(turns)

    # ── 2. Auto-compaction ────────────────────────────────────
    if config and provider:
        try:
            from clambot.session.compaction import maybe_auto_compact_session

            compaction_config = getattr(config, "agents", None)
            if compaction_config:
                compaction_config = getattr(compaction_config, "compaction", None)

            if compaction_config and getattr(compaction_config, "enabled", False):
                await maybe_auto_compact_session(
                    session_manager=session_manager,
                    key=session_key,
                    config=compaction_config,
                    provider=provider,
                )
                # Reload after compaction
                turns = session_manager.load_history(session_key)
                history = turns_to_llm_history(turns)
        except Exception as exc:
            logger.debug("Auto-compaction failed: %s", exc)

    # ── 3. Execute agent turn ─────────────────────────────────
    # Publish the originating channel/chat_id so tools (e.g. cron) can
    # read them via contextvars without explicit parameter threading.
    from clambot.bus.context import current_channel, current_chat_id

    current_channel.set(inbound.channel)
    current_chat_id.set(inbound.chat_id)

    result = await agent_loop.process_turn(
        message=inbound.content,
        session_key=session_key,
        history=history,
        config=config,
        on_event=on_event,
    )

    # ── 3b. Interactive secret prompt (CLI) ───────────────────
    if (
        result.status == "secret_pending"
        and result.missing_secrets
        and secret_prompt_callback is not None
        and secret_store is not None
    ):
        provided = secret_prompt_callback(result.missing_secrets)
        if provided:
            for name, value in provided.items():
                if value:
                    secret_store.save(name, value)

            # Re-run the same turn now that secrets are available
            result = await agent_loop.process_turn(
                message=inbound.content,
                session_key=session_key,
                history=history,
                config=config,
                on_event=on_event,
            )

    # ── 4. Persist turns (skip if resume) ─────────────────────
    if not is_approval_resume and not is_secret_resume:
        # Append user turn
        session_manager.append_turn(
            key=session_key,
            role="user",
            content=inbound.content,
        )

        # Append assistant turn
        if result.content:
            session_manager.append_turn(
                key=session_key,
                role="assistant",
                content=result.content,
            )

    # ── 5. Background fact extraction ─────────────────────────
    # Extract from BOTH user and assistant messages so facts like
    # "my name is Alex" are captured even when the bot's reply
    # doesn't explicitly repeat them.
    if provider and workspace and not is_approval_resume:
        combined_turn = (
            f"User: {inbound.content}\n\nAssistant: {result.content}"
            if result.content
            else f"User: {inbound.content}"
        )
        tracked_task(
            _background_extract_durable_facts(
                turn={"role": "conversation", "content": combined_turn},
                session_key=session_key,
                provider=provider,
                workspace=workspace,
            ),
            name="background-fact-extraction",
        )

    # ── 6. Build outbound ─────────────────────────────────────
    outbound_type = "secret_pending" if result.status == "secret_pending" else "text"
    outbound_metadata: dict[str, Any] = {}
    if result.status == "secret_pending":
        outbound_metadata["missing_secrets"] = result.missing_secrets
    if result.clam_name:
        outbound_metadata["clam_name"] = result.clam_name

    return OutboundMessage(
        channel=inbound.channel,
        target=inbound.chat_id,
        content=result.content,
        type=outbound_type,
        correlation_id=inbound.correlation_id,
        metadata=outbound_metadata,
    )


async def _background_extract_durable_facts(
    turn: dict[str, Any],
    session_key: str,
    provider: Any,
    workspace: Path,
) -> None:
    """Fire-and-forget background durable fact extraction.

    Never blocks the response — errors are silently logged.
    The ``_memory_write_lock`` serialises concurrent read-modify-write
    operations on MEMORY.md so two simultaneous extractions cannot
    overwrite each other's facts.
    """
    try:
        from clambot.memory.facts import extract_durable_facts_for_turn
        from clambot.memory.store import memory_recall, memory_save

        # Read existing memory under lock so extraction can skip duplicates
        async with _memory_write_lock:
            current = memory_recall(workspace)

        facts = await extract_durable_facts_for_turn(
            turn,
            session_key,
            provider,
            existing_memory=current,
        )
        if facts and facts.facts:
            new_facts = "\n".join(f"- {f}" for f in facts.facts)

            async with _memory_write_lock:
                # Re-read in case another task updated it
                current = memory_recall(workspace)

                if current.strip():
                    updated = f"{current.rstrip()}\n{new_facts}\n"
                else:
                    updated = f"### Recent Facts\n{new_facts}\n"

                memory_save(workspace, updated)
    except Exception as exc:
        logger.debug("Background fact extraction failed: %s", exc)
