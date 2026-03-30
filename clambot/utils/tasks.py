"""Tracked asyncio task helper — prevents silent exception loss.

Fire-and-forget ``asyncio.create_task()`` calls silently lose exceptions
unless the returned ``Task`` is awaited or has a done callback.  This
module provides :func:`tracked_task` which:

1. Stores a strong reference in a module-level ``WeakSet`` so the task
   is not garbage-collected prematurely.
2. Attaches a ``done_callback`` that logs any unhandled exception at
   ``logger.error`` level.

Usage::

    from clambot.utils.tasks import tracked_task

    tracked_task(some_coroutine(), name="my-background-job")
"""

from __future__ import annotations

import asyncio
import logging
from weakref import WeakSet

__all__ = ["tracked_task"]

logger = logging.getLogger(__name__)

# Strong-enough reference to keep fire-and-forget tasks alive until
# completion.  WeakSet allows the event loop to remain the primary
# owner — once the task finishes and the loop drops it, the weak
# reference is cleaned up automatically.
_background_tasks: WeakSet[asyncio.Task] = WeakSet()


def tracked_task(
    coro: object,
    *,
    name: str | None = None,
) -> asyncio.Task:
    """Create an :class:`asyncio.Task` with exception logging.

    The task reference is held in a module-level :class:`~weakref.WeakSet`
    to prevent garbage-collection before completion.  A ``done_callback``
    logs unhandled exceptions at ``error`` level.

    Args:
        coro: The coroutine to schedule.
        name: Optional task name (appears in log messages and
              :func:`asyncio.all_tasks` output).

    Returns:
        The created :class:`asyncio.Task`.
    """
    task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
    _background_tasks.add(task)
    task.add_done_callback(_task_done_callback)
    return task


def _task_done_callback(task: asyncio.Task) -> None:
    """Log unhandled exceptions from completed background tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        task_name = task.get_name() if hasattr(task, "get_name") else "unnamed"
        logger.error(
            "Background task %r raised an unhandled exception: %s",
            task_name,
            exc,
            exc_info=exc,
        )
