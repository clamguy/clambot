"""Heartbeat service — proactive periodic wakeup.

``InMemoryHeartbeatService`` reads HEARTBEAT.md on a configurable interval
and triggers the agent pipeline when actionable content is found.

Before processing HEARTBEAT.md each tick, the service runs workspace
cleanup (stale clams, orphaned builds, old uploads, etc.).

``NotConfiguredHeartbeatService`` is a safe no-op stub used when heartbeat
is disabled or not configured.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from clambot.bus.events import InboundMessage
from clambot.utils.tasks import tracked_task
from clambot.workspace.cleanup import run_cleanup

__all__ = [
    "InMemoryHeartbeatService",
    "NotConfiguredHeartbeatService",
]

logger = logging.getLogger(__name__)

# Regex patterns for "non-actionable" lines in HEARTBEAT.md
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_EMPTY_CHECKBOX_RE = re.compile(r"^\s*-\s*\[\s*\]\s*$")
_WHITESPACE_RE = re.compile(r"^\s*$")


def _is_actionable(content: str) -> bool:
    """Return True if HEARTBEAT.md contains actionable content.

    Skip logic: headings, empty checkboxes, and whitespace-only lines
    are NOT considered actionable.  At least one line must contain
    something beyond those patterns.
    """
    if not content or not content.strip():
        return False

    for line in content.splitlines():
        if _WHITESPACE_RE.match(line):
            continue
        if _HEADING_RE.match(line):
            continue
        if _EMPTY_CHECKBOX_RE.match(line):
            continue
        # Found a line that is NOT heading, empty checkbox, or whitespace
        return True

    return False


class InMemoryHeartbeatService:
    """Production heartbeat service that periodically checks HEARTBEAT.md.

    Args:
        config: HeartbeatConfig with ``enabled`` and ``interval`` fields.
        workspace: Root workspace path containing ``memory/HEARTBEAT.md``.
    """

    def __init__(self, config: Any, workspace: Path) -> None:
        self._config = config
        self._workspace = workspace
        self._executor: Callable[[InboundMessage], Coroutine[Any, Any, Any]] | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def set_executor(self, fn: Callable[[InboundMessage], Coroutine[Any, Any, Any]]) -> None:
        """Set the async executor callback for processing heartbeat messages."""
        self._executor = fn

    async def start(self) -> None:
        """Start the heartbeat loop as an asyncio task."""
        self._running = True
        self._task = tracked_task(self._run(), name="heartbeat-service")
        logger.info("HeartbeatService started (interval=%ds)", self._config.interval)

    async def stop(self) -> None:
        """Stop the heartbeat loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("HeartbeatService stopped")

    async def _run(self) -> None:
        """Main heartbeat loop.

        Waits ``config.interval`` seconds between checks.  On each tick:
        1. Run workspace cleanup (stale clams, orphan builds, etc.).
        2. Read HEARTBEAT.md — if actionable content is found, trigger
           the executor.
        """
        while self._running:
            try:
                await asyncio.sleep(self._config.interval)

                # ── 1. Workspace cleanup ──────────────────────────
                try:
                    cleanup_cfg = getattr(self._config, "cleanup", None)
                    if cleanup_cfg is not None:
                        stats = run_cleanup(self._workspace, cleanup_cfg)
                        if (
                            stats.stale_clams_removed
                            or stats.orphan_builds_removed
                            or stats.disabled_cron_jobs_removed
                            or stats.uploads_removed
                            or stats.cron_log_lines_trimmed
                            or stats.sessions_pruned
                        ):
                            logger.info(
                                "Heartbeat cleanup: clams=%d, builds=%d, cron=%d, "
                                "uploads=%d, log_lines=%d, sessions=%d",
                                len(stats.stale_clams_removed),
                                len(stats.orphan_builds_removed),
                                stats.disabled_cron_jobs_removed,
                                stats.uploads_removed,
                                stats.cron_log_lines_trimmed,
                                stats.sessions_pruned,
                            )
                except Exception as exc:
                    logger.warning("Heartbeat cleanup failed: %s", exc)

                # ── 2. HEARTBEAT.md processing ────────────────────
                heartbeat_path = self._resolve_heartbeat_path()
                if not heartbeat_path.exists():
                    continue

                content = heartbeat_path.read_text(encoding="utf-8")
                if not _is_actionable(content):
                    logger.debug("Heartbeat: HEARTBEAT.md has no actionable content, skipping")
                    continue

                if self._executor is None:
                    logger.warning("Heartbeat: no executor set, skipping")
                    continue

                logger.info("Heartbeat: triggering with actionable content")
                inbound = InboundMessage(
                    channel="heartbeat",
                    source="system",
                    chat_id="heartbeat",
                    content=content.strip(),
                )
                await self._executor(inbound)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Heartbeat loop error: %s", exc)

    def _resolve_heartbeat_path(self) -> Path:
        """Resolve the HEARTBEAT.md path in the workspace."""
        memory_dir = self._workspace / "memory"
        return memory_dir / "HEARTBEAT.md"


class NotConfiguredHeartbeatService:
    """No-op heartbeat stub used when heartbeat is disabled."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def set_executor(self, fn: Any) -> None:
        """No-op."""

    async def start(self) -> None:
        """No-op."""

    async def stop(self) -> None:
        """No-op."""

    async def _run(self) -> None:
        """Block forever (never wakes)."""
        await asyncio.Event().wait()
