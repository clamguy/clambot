"""Tests for clambot.async_runner — Phase 2 Core Primitives."""

import asyncio

from clambot.async_runner import get_event_loop, run_sync

# ---------------------------------------------------------------------------
# run_sync
# ---------------------------------------------------------------------------


def test_run_sync_returns_coroutine_result() -> None:
    """run_sync executes a coroutine and returns its result synchronously."""

    async def add(a: int, b: int) -> int:
        return a + b

    result = run_sync(add(3, 4))

    assert result == 7


def test_run_sync_with_async_sleep() -> None:
    """run_sync works correctly with coroutines that await (e.g. asyncio.sleep)."""

    async def delayed_value() -> str:
        await asyncio.sleep(0)
        return "done"

    assert run_sync(delayed_value()) == "done"


# ---------------------------------------------------------------------------
# get_event_loop
# ---------------------------------------------------------------------------


def test_get_event_loop_returns_running_loop() -> None:
    """get_event_loop returns a loop that is actively running."""
    loop = get_event_loop()

    assert loop.is_running()


def test_get_event_loop_is_singleton() -> None:
    """Repeated calls to get_event_loop return the same loop instance."""
    loop1 = get_event_loop()
    loop2 = get_event_loop()

    assert loop1 is loop2
