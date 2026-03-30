"""Cron store — atomic JSON persistence for cron jobs.

Uses the same atomic rename pattern as :mod:`clambot.tools.secrets.store`:
write to a sibling temp file, ``os.rename()`` to the target path.  This
guarantees that a crash mid-write never corrupts the store.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

from clambot.cron.types import (  # noqa: E402
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronStore,
)

__all__ = [
    "load_cron_store",
    "save_cron_store",
]


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _job_to_dict(job: CronJob) -> dict:
    """Serialize a :class:`CronJob` to a JSON-safe dict (camelCase keys)."""
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        "schedule": {
            "kind": job.schedule.kind,
            "everySeconds": job.schedule.every_seconds,
            "cronExpr": job.schedule.cron_expr,
            "timezone": job.schedule.timezone,
            "atMs": job.schedule.at_ms,
        },
        "payload": {
            "kind": job.payload.kind,
            "message": job.payload.message,
            "deliver": job.payload.deliver,
            "channel": job.payload.channel,
            "target": job.payload.target,
            "clamId": job.payload.clam_id,
            "metadata": job.payload.metadata,
        },
        "state": {
            "nextRunAtMs": job.state.next_run_at_ms,
            "lastRunAtMs": job.state.last_run_at_ms,
            "lastStatus": job.state.last_status,
            "lastError": job.state.last_error,
        },
        "createdAtMs": job.created_at_ms,
        "updatedAtMs": job.updated_at_ms,
        "deleteAfterRun": job.delete_after_run,
    }


def _dict_to_job(data: dict) -> CronJob:
    """Deserialize a camelCase dict into a :class:`CronJob`."""
    sched = data.get("schedule", {})
    payload = data.get("payload", {})
    state = data.get("state", {})

    return CronJob(
        id=data["id"],
        name=data.get("name", ""),
        enabled=data.get("enabled", True),
        schedule=CronSchedule(
            kind=sched.get("kind", "every"),
            every_seconds=sched.get("everySeconds"),
            cron_expr=sched.get("cronExpr"),
            timezone=sched.get("timezone"),
            at_ms=sched.get("atMs"),
        ),
        payload=CronPayload(
            kind=payload.get("kind", "agent_turn"),
            message=payload.get("message", ""),
            deliver=payload.get("deliver", False),
            channel=payload.get("channel"),
            target=payload.get("target"),
            clam_id=payload.get("clamId"),
            metadata=payload.get("metadata"),
        ),
        state=CronJobState(
            next_run_at_ms=state.get("nextRunAtMs"),
            last_run_at_ms=state.get("lastRunAtMs"),
            last_status=state.get("lastStatus"),
            last_error=state.get("lastError"),
        ),
        created_at_ms=data.get("createdAtMs", 0),
        updated_at_ms=data.get("updatedAtMs", 0),
        delete_after_run=data.get("deleteAfterRun", False),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_cron_store(path: Path) -> CronStore:
    """Load the cron store from *path*.

    Returns an empty :class:`CronStore` if the file does not exist or
    contains invalid JSON.

    Args:
        path: Absolute path to ``jobs.json``.

    Returns:
        Populated or empty :class:`CronStore`.
    """
    if not path.exists():
        return CronStore()
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        jobs = [_dict_to_job(j) for j in data.get("jobs", [])]
        return CronStore(
            version=data.get("version", 1),
            schema=data.get("schema", "cron_store_v1"),
            jobs=jobs,
        )
    except Exception as exc:
        logger.warning("Failed to load cron store from %s (returning empty): %s", path, exc)
        return CronStore()


def save_cron_store(path: Path, store: CronStore) -> None:
    """Atomically persist the cron store to *path*.

    Uses the write-temp-then-rename pattern for crash safety.

    Args:
        path: Absolute path to ``jobs.json``.
        store: The cron store to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "version": store.version,
        "schema": store.schema,
        "jobs": [_job_to_dict(j) for j in store.jobs],
    }

    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.rename(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
