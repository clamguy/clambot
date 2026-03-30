"""Cron tool — built-in tool stub for scheduling recurring or one-time tasks.

Re-exports :class:`CronTool` as the primary public symbol for this
sub-package.  Internal helpers (``approval``, ``contract``) are importable
directly from their respective modules when needed.

Full cron service integration is wired in Phase 11.
"""

from __future__ import annotations

from clambot.tools.cron.operations import CronTool

__all__ = [
    "CronTool",
]
