"""Cron types — data models for scheduled jobs.

Mirrors the nanobot reference implementation but uses frozen/immutable state
where appropriate and follows clambot conventions (camelCase serialization,
dataclass-based models).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "CronSchedule",
    "CronPayload",
    "CronJobState",
    "CronJob",
    "CronStore",
]


@dataclass
class CronSchedule:
    """Schedule definition for a cron job.

    Exactly one of ``every_seconds``, ``cron_expr``, or ``at_ms`` should be
    set; the ``kind`` field indicates which variant is active.

    Attributes:
        kind: Schedule variant — ``"every"`` (interval), ``"cron"`` (5-field
              cron expression), or ``"at"`` (one-time epoch ms).
        every_seconds: Repeat interval in seconds (``kind="every"``).
        cron_expr: 5-field cron expression, e.g. ``"0 9 * * *"``
                   (``kind="cron"``).
        timezone: IANA timezone for cron expressions, e.g. ``"America/New_York"``.
                  Defaults to ``"UTC"`` when omitted.
        at_ms: Unix epoch timestamp in milliseconds (``kind="at"``).
    """

    kind: Literal["every", "cron", "at"]
    every_seconds: int | None = None
    cron_expr: str | None = None
    timezone: str | None = None
    at_ms: int | None = None


@dataclass
class CronPayload:
    """What to do when the job fires.

    Attributes:
        kind: Payload type — ``"agent_turn"`` runs an agent turn with
              ``message`` as the input.
        message: The user message or task description to execute.
        deliver: Whether to deliver the result to a channel.
        channel: Target channel name (e.g. ``"telegram"``).
        target: Target chat/user ID within the channel.
        clam_id: If set, execute this promoted clam directly on cron fire
                 instead of running the full agent pipeline.  Populated
                 automatically after the first successful agent-turn
                 execution, or set explicitly when scheduling a known clam.
        metadata: Arbitrary extra metadata forwarded to the inbound message.
    """

    kind: Literal["system_event", "agent_turn"] = "agent_turn"
    message: str = ""
    deliver: bool = False
    channel: str | None = None
    target: str | None = None
    clam_id: str | None = None
    metadata: dict | None = None


@dataclass
class CronJobState:
    """Mutable runtime state of a job.

    Attributes:
        next_run_at_ms: Next scheduled execution time (epoch ms), or ``None``
                        if the job is disabled or exhausted.
        last_run_at_ms: Time of the most recent execution (epoch ms).
        last_status: Outcome of the last execution.
        last_error: Error message from the last failed execution.
    """

    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass
class CronJob:
    """A scheduled job with its configuration, payload, and runtime state.

    Attributes:
        id: Short unique identifier (e.g. first 8 chars of a UUID).
        name: Human-readable job name.
        enabled: Whether the job is active.
        schedule: When the job fires.
        payload: What the job does.
        state: Mutable runtime state.
        created_at_ms: Creation timestamp (epoch ms).
        updated_at_ms: Last modification timestamp (epoch ms).
        delete_after_run: If ``True``, the job is removed after first execution
                          (only meaningful for ``kind="at"``).
    """

    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False


@dataclass
class CronStore:
    """Persistent store envelope for cron jobs.

    Attributes:
        version: Schema version (currently ``1``).
        schema: Schema identifier string.
        jobs: All stored jobs.
    """

    version: int = 1
    schema: str = "cron_store_v1"
    jobs: list[CronJob] = field(default_factory=list)
