"""Cron service — asyncio-based job scheduler.

``InMemoryCronService`` is the production implementation: it loads jobs from
disk, maintains an in-memory list, and uses an ``asyncio.Event`` to wake the
scheduler loop immediately when jobs are added or removed.

``NotConfiguredCronService`` is a safe no-op stub returned when the ``cron``
config section is missing or disabled.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from clambot.cron.audit import log_cron_event
from clambot.cron.schedule import calculate_next_run_ms, parse_schedule
from clambot.cron.store import load_cron_store, save_cron_store
from clambot.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronStore,
)

__all__ = [
    "InMemoryCronService",
    "NotConfiguredCronService",
    "configure_cron_tool_runtime_sync_hook",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Current time in epoch milliseconds."""
    return int(time.time() * 1000)


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Pre-add validation — rejects bad configurations early."""
    if schedule.timezone and schedule.kind != "cron":
        raise ValueError("timezone can only be used with cron schedules")
    if schedule.kind == "cron" and schedule.timezone:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(schedule.timezone)
        except Exception:
            raise ValueError(f"Unknown timezone: {schedule.timezone!r}") from None
    if schedule.kind == "every" and (not schedule.every_seconds or schedule.every_seconds <= 0):
        raise ValueError("every_seconds must be a positive integer")


# ---------------------------------------------------------------------------
# InMemoryCronService
# ---------------------------------------------------------------------------


class InMemoryCronService:
    """Production cron scheduler backed by an in-memory job list and on-disk
    JSON persistence.

    The scheduler loop sleeps until the next job is due, or until
    ``_change_event`` is set (on add / remove / enable / disable).  This
    avoids polling entirely.

    Args:
        store_path: Path to ``jobs.json``.
        workspace: Workspace root (for audit logging).
    """

    def __init__(self, store_path: Path, workspace: Path | None = None) -> None:
        self._store_path = store_path
        self._workspace = workspace
        self._store: CronStore = CronStore()
        self._change_event = asyncio.Event()
        self._running = False
        self._executor: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load store from disk and recompute next-run times."""
        self._store = load_cron_store(self._store_path)
        self._recompute_next_runs()
        save_cron_store(self._store_path, self._store)
        self._running = True
        logger.info("Cron service started with %d jobs", len(self._store.jobs))

    def stop(self) -> None:
        """Signal the scheduler loop to terminate."""
        self._running = False
        self._change_event.set()

    def set_executor(
        self,
        fn: Callable[[CronJob], Coroutine[Any, Any, str | None]],
    ) -> None:
        """Set the async callback that actually executes a job.

        Typically ``orchestrator.process_inbound_async`` wrapped to build
        an :class:`InboundMessage` from the job payload.
        """
        self._executor = fn

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Main scheduler loop — runs until :meth:`stop` is called.

        1. Find due jobs and execute them.
        2. Compute time until next due job.
        3. Sleep until that time, or until ``_change_event`` wakes us.
        4. Repeat.
        """
        while self._running:
            now = _now_ms()

            # Find and execute due jobs
            due_jobs = [
                j
                for j in self._store.jobs
                if j.enabled
                and j.state.next_run_at_ms is not None
                and j.state.next_run_at_ms <= now
            ]

            for job in due_jobs:
                await self._execute_job(job)

            if due_jobs:
                save_cron_store(self._store_path, self._store)

            # Compute sleep duration
            sleep_s = self._compute_sleep_seconds()

            # Wait for either the sleep duration or a change event
            self._change_event.clear()
            try:
                await asyncio.wait_for(
                    self._change_event.wait(),
                    timeout=sleep_s,
                )
            except TimeoutError:
                pass  # Timer expired — loop back to check due jobs

    def _compute_sleep_seconds(self) -> float:
        """Compute seconds until the next due job, or 60s default."""
        times = [
            j.state.next_run_at_ms
            for j in self._store.jobs
            if j.enabled and j.state.next_run_at_ms is not None
        ]
        if not times:
            return 60.0
        next_ms = min(times)
        delay_ms = max(0, next_ms - _now_ms())
        return delay_ms / 1000.0

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    async def _execute_job(self, job: CronJob) -> None:
        """Execute a single due job."""
        start_ms = _now_ms()
        logger.info("Cron: executing job '%s' (%s)", job.name, job.id)

        self._audit("cron_executor_started", job)

        try:
            if self._executor:
                await self._executor(job)

            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info("Cron: job '%s' completed", job.name)
            self._audit("cron_orchestrator_completed", job)

        except Exception as exc:
            job.state.last_status = "error"
            job.state.last_error = str(exc)
            logger.error("Cron: job '%s' failed: %s", job.name, exc)
            self._audit("cron_executor_error", job, error=str(exc))

        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = _now_ms()

        # Handle post-execution lifecycle
        if job.delete_after_run:
            # One-shot: remove from store regardless of schedule kind
            self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
        elif job.schedule.kind == "at":
            # One-time schedule without deletion: disable, keep for record
            job.enabled = False
            job.state.next_run_at_ms = None
        else:
            # Recurring: compute next run
            job.state.next_run_at_ms = calculate_next_run_ms(job.schedule, _now_ms())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """Return all jobs, optionally including disabled ones."""
        if include_disabled:
            jobs = list(self._store.jobs)
        else:
            jobs = [j for j in self._store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float("inf"))

    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        target: str | None = None,
        clam_id: str | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        """Create and persist a new job.

        Wakes the scheduler loop immediately via ``_change_event``.
        """
        _validate_schedule_for_add(schedule)
        now = _now_ms()

        next_run = calculate_next_run_ms(schedule, now)
        # For "at" jobs already past due, treat them as immediately due
        if next_run is None and schedule.kind == "at" and schedule.at_ms is not None:
            next_run = schedule.at_ms

        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                target=target,
                clam_id=clam_id,
            ),
            state=CronJobState(
                next_run_at_ms=next_run,
            ),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )

        self._store.jobs.append(job)
        save_cron_store(self._store_path, self._store)
        self._change_event.set()

        logger.info("Cron: added job '%s' (%s)", name, job.id)
        return job

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID.  Returns ``True`` if found and removed."""
        before = len(self._store.jobs)
        self._store.jobs = [j for j in self._store.jobs if j.id != job_id]
        removed = len(self._store.jobs) < before

        if removed:
            save_cron_store(self._store_path, self._store)
            self._change_event.set()
            logger.info("Cron: removed job %s", job_id)

        return removed

    def enable_job(self, job_id: str) -> CronJob | None:
        """Enable a job and recalculate its next run time."""
        for job in self._store.jobs:
            if job.id == job_id:
                job.enabled = True
                job.updated_at_ms = _now_ms()
                job.state.next_run_at_ms = calculate_next_run_ms(job.schedule, _now_ms())
                save_cron_store(self._store_path, self._store)
                self._change_event.set()
                return job
        return None

    def disable_job(self, job_id: str) -> CronJob | None:
        """Disable a job."""
        for job in self._store.jobs:
            if job.id == job_id:
                job.enabled = False
                job.updated_at_ms = _now_ms()
                job.state.next_run_at_ms = None
                save_cron_store(self._store_path, self._store)
                self._change_event.set()
                return job
        return None

    async def run_job(self, job_id: str) -> bool:
        """Manually run a job regardless of schedule."""
        for job in self._store.jobs:
            if job.id == job_id:
                await self._execute_job(job)
                save_cron_store(self._store_path, self._store)
                self._change_event.set()
                return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _recompute_next_runs(self) -> None:
        """Recompute ``next_run_at_ms`` for all enabled jobs.

        For one-shot ``"at"`` jobs whose target time has already passed
        (e.g. the service restarted after the scheduled time), the job
        is treated as immediately due so it fires on the next scheduler
        tick rather than being silently lost.
        """
        now = _now_ms()
        for job in self._store.jobs:
            if job.enabled:
                next_run = calculate_next_run_ms(job.schedule, now)
                # Preserve past-due one-shot jobs — same fallback as add_job()
                if (
                    next_run is None
                    and job.schedule.kind == "at"
                    and job.schedule.at_ms is not None
                ):
                    next_run = job.schedule.at_ms
                job.state.next_run_at_ms = next_run

    def _audit(
        self,
        event: str,
        job: CronJob,
        *,
        error: str | None = None,
    ) -> None:
        """Write an audit event to the cron events log."""
        if self._workspace is None:
            return
        log_cron_event(
            workspace=self._workspace,
            event=event,
            job_id=job.id,
            job_name=job.name,
            error=error,
        )


# ---------------------------------------------------------------------------
# NotConfiguredCronService
# ---------------------------------------------------------------------------


class NotConfiguredCronService:
    """No-op cron service returned when the ``cron`` config section is missing
    or ``enabled=False``.

    All methods are safe to call and return empty/no-op values.
    """

    async def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_executor(self, fn: Any) -> None:
        pass

    async def _run(self) -> None:
        # Block forever — matching InMemoryCronService API so callers can
        # ``create_task(cron_service._run())`` unconditionally.
        await asyncio.Event().wait()

    def list_jobs(self, include_disabled: bool = False) -> list:
        return []

    def add_job(self, **kwargs: Any) -> None:
        return None

    def remove_job(self, job_id: str) -> bool:
        return False

    def enable_job(self, job_id: str) -> None:
        return None

    def disable_job(self, job_id: str) -> None:
        return None

    async def run_job(self, job_id: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# Sync hook wiring
# ---------------------------------------------------------------------------


def configure_cron_tool_runtime_sync_hook(
    cron_tool: Any,
    cron_service: InMemoryCronService,
    *,
    default_channel: str | None = None,
    default_target: str | None = None,
) -> None:
    """Wire the cron tool to the live cron service.

    Replaces the stub responses in :class:`~clambot.tools.cron.operations.CronTool`
    with real add/list/remove operations backed by *cron_service*.

    Args:
        cron_tool: The ``CronTool`` instance from the tool registry.
        cron_service: The live ``InMemoryCronService``.
        default_channel: Channel to auto-fill when not specified.
        default_target: Target to auto-fill when not specified.
    """

    def sync_hook(args: dict) -> Any:
        action = args.get("action", "")

        if action == "list":
            import datetime as _dt

            jobs = cron_service.list_jobs(include_disabled=True)

            def _fmt_ms(ms: int | None) -> str:
                if ms is None:
                    return "N/A"
                return _dt.datetime.fromtimestamp(
                    ms / 1000,
                    tz=_dt.UTC,
                ).strftime("%Y-%m-%d %H:%M:%S UTC")

            def _describe_schedule(j: Any) -> str:
                s = j.schedule
                if j.delete_after_run:
                    return f"one-shot (fires once, next: {_fmt_ms(j.state.next_run_at_ms)})"
                if s.kind == "cron" and s.cron_expr:
                    tz = f" ({s.timezone})" if s.timezone else ""
                    return f"cron: {s.cron_expr}{tz}"
                if s.kind == "every" and s.every_seconds:
                    return f"every {s.every_seconds}s"
                if s.kind == "at" and s.at_ms:
                    return f"once at {_fmt_ms(s.at_ms)}"
                return s.kind

            return {
                "jobs": [
                    {
                        "id": j.id,
                        "name": j.name,
                        "enabled": j.enabled,
                        "schedule": _describe_schedule(j),
                        "next_run": _fmt_ms(j.state.next_run_at_ms),
                        "last_status": j.state.last_status,
                        "delete_after_run": j.delete_after_run,
                    }
                    for j in jobs
                ],
            }

        if action == "add":
            message = args.get("message")
            if not message:
                return {"ok": False, "message": "Missing 'message' parameter."}

            try:
                schedule = parse_schedule(args)
            except ValueError as exc:
                return {"ok": False, "message": str(exc)}

            # Resolve channel/target — prefer explicit args, then the
            # live conversation context (via contextvars), then static
            # defaults from config.
            from clambot.bus.context import current_channel, current_chat_id

            channel = args.get("channel") or current_channel.get("") or default_channel
            target = args.get("target") or current_chat_id.get("") or default_target

            name = args.get("name", message[:40])
            clam_id = args.get("clam_id")
            delete_after_run = args.get("delete_after_run", False)

            try:
                job = cron_service.add_job(
                    name=name,
                    schedule=schedule,
                    message=message,
                    deliver=bool(channel),
                    channel=channel,
                    target=target,
                    clam_id=clam_id,
                    delete_after_run=delete_after_run,
                )
            except ValueError as exc:
                return {"ok": False, "message": str(exc)}

            return {
                "ok": True,
                "job_id": job.id,
                "name": job.name,
                "next_run_at_ms": job.state.next_run_at_ms,
                "message": f"Job '{job.name}' scheduled.",
            }

        if action == "remove":
            job_id = args.get("job_id")
            if not job_id:
                return {"ok": False, "message": "Missing 'job_id' parameter."}
            removed = cron_service.remove_job(job_id)
            if removed:
                return {"ok": True, "message": f"Job '{job_id}' removed."}
            return {"ok": False, "message": f"Job '{job_id}' not found."}

        return {"ok": False, "message": f"Unknown cron action: '{action}'."}

    cron_tool.set_sync_hook(sync_hook)
