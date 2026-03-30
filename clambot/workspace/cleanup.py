"""Workspace cleanup — periodic housekeeping for workspace artefacts.

Called by the heartbeat service on each tick.  Each helper is independent
and logs what it removes so operators can audit cleanup activity.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clambot.workspace.retention import prune_session_logs

__all__ = ["run_cleanup", "CleanupStats"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CleanupStats:
    """Summary of a single cleanup run."""

    stale_clams_removed: list[str] = field(default_factory=list)
    orphan_builds_removed: list[str] = field(default_factory=list)
    disabled_cron_jobs_removed: int = 0
    uploads_removed: int = 0
    cron_log_lines_trimmed: int = 0
    sessions_pruned: int = 0


# ---------------------------------------------------------------------------
# Individual cleanup helpers
# ---------------------------------------------------------------------------


def _prune_stale_clams(
    workspace: Path,
    max_age_days: int,
) -> list[str]:
    """Remove promoted clams whose ``last_used`` exceeds *max_age_days*.

    Clams with ``usage_count == 0`` (never executed) are skipped — they
    may have just been promoted and deserve a chance to be used.
    """
    clams_dir = workspace / "clams"
    usage_path = clams_dir / ".usage.json"
    if not clams_dir.is_dir() or not usage_path.exists():
        return []

    try:
        usage: dict[str, Any] = json.loads(
            usage_path.read_text(encoding="utf-8"),
        )
    except Exception:
        return []

    cutoff = time.time() - max_age_days * 86_400
    removed: list[str] = []

    for clam_dir in list(clams_dir.iterdir()):
        if not clam_dir.is_dir():
            continue

        name = clam_dir.name
        entry = usage.get(name)
        if entry is None:
            # No usage record at all — never executed, skip.
            continue

        count = entry.get("usage_count", 0)
        last = entry.get("last_used", 0.0)

        if count > 0 and last < cutoff:
            try:
                shutil.rmtree(clam_dir)
                usage.pop(name, None)
                removed.append(name)
                logger.info(
                    "Cleanup: removed stale clam '%s' (last used %.0f days ago)",
                    name,
                    (time.time() - last) / 86_400,
                )
            except Exception as exc:
                logger.warning("Cleanup: failed to remove clam '%s': %s", name, exc)

    # Persist updated usage file
    if removed:
        try:
            usage_path.write_text(json.dumps(usage, indent=2), encoding="utf-8")
        except Exception:
            pass

    return removed


def _prune_orphan_builds(
    workspace: Path,
    max_age_hours: int,
) -> list[str]:
    """Remove build directories that were never promoted and are older than *max_age_hours*."""
    build_dir = workspace / "build"
    clams_dir = workspace / "clams"
    if not build_dir.is_dir():
        return []

    cutoff = time.time() - max_age_hours * 3600
    removed: list[str] = []

    for entry in list(build_dir.iterdir()):
        if not entry.is_dir():
            continue

        # Skip if already promoted
        if (clams_dir / entry.name).is_dir():
            continue

        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue

        if mtime < cutoff:
            try:
                shutil.rmtree(entry)
                removed.append(entry.name)
                logger.info("Cleanup: removed orphan build '%s'", entry.name)
            except Exception as exc:
                logger.warning("Cleanup: failed to remove orphan build '%s': %s", entry.name, exc)

    return removed


def _prune_disabled_cron_jobs(workspace: Path) -> int:
    """Remove disabled (completed one-shot) cron jobs from ``jobs.json``."""
    jobs_path = workspace / "cron" / "jobs.json"
    if not jobs_path.exists():
        return 0

    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    jobs = data.get("jobs", [])
    before = len(jobs)
    active = [j for j in jobs if j.get("enabled", True)]
    removed = before - len(active)

    if removed > 0:
        data["jobs"] = active
        try:
            jobs_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            logger.info("Cleanup: removed %d disabled cron job(s)", removed)
        except Exception as exc:
            logger.warning("Cleanup: failed to update jobs.json: %s", exc)
            return 0

    return removed


def _prune_old_uploads(workspace: Path, max_age_days: int) -> int:
    """Remove uploaded files older than *max_age_days*."""
    upload_dir = workspace / "upload"
    if not upload_dir.is_dir():
        return 0

    cutoff = time.time() - max_age_days * 86_400
    removed = 0

    for entry in list(upload_dir.iterdir()):
        if not entry.is_file():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue

        if mtime < cutoff:
            try:
                entry.unlink()
                removed += 1
                logger.debug("Cleanup: removed old upload '%s'", entry.name)
            except Exception as exc:
                logger.warning("Cleanup: failed to remove upload '%s': %s", entry.name, exc)

    if removed:
        logger.info("Cleanup: removed %d old upload(s)", removed)
    return removed


def _trim_cron_log(workspace: Path, max_lines: int) -> int:
    """Truncate the cron audit log to the last *max_lines* lines."""
    log_path = workspace / "logs" / "gateway_cron_events.jsonl"
    if not log_path.exists():
        return 0

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0

    if len(lines) <= max_lines:
        return 0

    trimmed = len(lines) - max_lines
    try:
        log_path.write_text(
            "\n".join(lines[-max_lines:]) + "\n",
            encoding="utf-8",
        )
        logger.info("Cleanup: trimmed %d line(s) from cron audit log", trimmed)
    except Exception as exc:
        logger.warning("Cleanup: failed to trim cron log: %s", exc)
        return 0

    return trimmed


# ---------------------------------------------------------------------------
# Main entry-point
# ---------------------------------------------------------------------------


def run_cleanup(workspace: Path, config: Any) -> CleanupStats:
    """Run all workspace cleanup tasks.

    Args:
        workspace: Root workspace path (e.g. ``~/.clambot/workspace``).
        config: A :class:`~clambot.config.schema.CleanupConfig` instance
                (or any object with the same attributes).

    Returns:
        A :class:`CleanupStats` summarising what was cleaned.
    """
    stats = CleanupStats()

    stats.stale_clams_removed = _prune_stale_clams(
        workspace,
        max_age_days=config.stale_clam_days,
    )

    stats.orphan_builds_removed = _prune_orphan_builds(
        workspace,
        max_age_hours=config.orphan_build_hours,
    )

    if config.prune_disabled_cron:
        stats.disabled_cron_jobs_removed = _prune_disabled_cron_jobs(workspace)

    stats.uploads_removed = _prune_old_uploads(
        workspace,
        max_age_days=config.upload_retention_days,
    )

    stats.cron_log_lines_trimmed = _trim_cron_log(
        workspace,
        max_lines=config.cron_log_max_lines,
    )

    sessions_dir = workspace / "sessions"
    if sessions_dir.is_dir():
        stats.sessions_pruned = prune_session_logs(
            sessions_dir,
            max_files=config.session_max_files,
        )

    return stats
