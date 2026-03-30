"""
Async runner — persistent daemon thread with event loop for CLI→async bridge.

Usage:
    from clambot.async_runner import run_sync
    result = run_sync(some_async_function())
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _start_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Run the event loop forever in a background thread."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def get_event_loop() -> asyncio.AbstractEventLoop:
    """Get or create the singleton event loop on a daemon thread.

    The first call creates a new event loop and starts a daemon thread.
    Subsequent calls return the same loop. Includes a spin-wait to
    ensure the loop is running before returning.
    """
    global _loop, _thread
    if _loop is not None and _loop.is_running():
        return _loop
    with _lock:
        if _loop is not None and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()
        _thread = threading.Thread(target=_start_loop, args=(_loop,), daemon=True)
        _thread.start()
        # Spin-wait until the loop is actually running
        deadline = time.monotonic() + 5.0
        while not _loop.is_running():
            if time.monotonic() > deadline:
                raise RuntimeError("Event loop failed to start within 5 seconds")
            time.sleep(0.001)
    return _loop


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine from synchronous code and return its result.

    Submits the coroutine to the background event loop and blocks
    until the result is available.
    """
    loop = get_event_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()
