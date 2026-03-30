"""Cron audit logging — append-only JSONL event log.

All cron execution events are logged to
``workspace/logs/gateway_cron_events.jsonl``.

Event types:
- ``cron_executor_started`` — job execution begins
- ``cron_orchestrator_invoked`` — orchestrator received the inbound message
- ``cron_orchestrator_completed`` — orchestrator finished processing
- ``cron_delivery_queued`` — outbound message queued for channel delivery
- ``cron_outbound_result`` — final delivery status
- ``cron_executor_error`` — job execution failed
"""

from __future__ import annotations

import json
import time
from pathlib import Path

__all__ = [
    "log_cron_event",
]


def log_cron_event(
    workspace: Path,
    event: str,
    job_id: str,
    job_name: str,
    *,
    error: str | None = None,
    extra: dict | None = None,
) -> None:
    """Append a cron audit event to the JSONL log.

    Args:
        workspace: Workspace root path.
        event: Event type string (e.g. ``"cron_executor_started"``).
        job_id: Job identifier.
        job_name: Job name.
        error: Error message (if applicable).
        extra: Additional metadata to include.
    """
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "gateway_cron_events.jsonl"

    entry: dict = {
        "ts": int(time.time() * 1000),
        "event": event,
        "job_id": job_id,
        "job_name": job_name,
    }
    if error is not None:
        entry["error"] = error
    if extra:
        entry.update(extra)

    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # Best-effort logging — never crash the scheduler
