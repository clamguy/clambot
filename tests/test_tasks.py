"""Tests for clambot.utils.tasks — tracked_task helper."""

from __future__ import annotations

import asyncio
import logging

import pytest

from clambot.utils.tasks import tracked_task


class TestTrackedTask:
    """Tests for the tracked_task helper."""

    @pytest.mark.asyncio
    async def test_tracked_task_logs_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify that an unhandled exception in a tracked task is logged."""

        async def _failing_coro() -> None:
            raise RuntimeError("deliberate test failure")

        with caplog.at_level(logging.ERROR, logger="clambot.utils.tasks"):
            task = tracked_task(_failing_coro(), name="test-failing")
            # Wait for the task to complete (it will raise internally)
            with pytest.raises(RuntimeError):
                await task

        assert any("deliberate test failure" in record.message for record in caplog.records)
        assert any("test-failing" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_tracked_task_success_no_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify clean completion doesn't log errors."""

        async def _ok_coro() -> str:
            return "done"

        with caplog.at_level(logging.ERROR, logger="clambot.utils.tasks"):
            task = tracked_task(_ok_coro(), name="test-ok")
            result = await task

        assert result == "done"
        assert not any(
            record.levelno >= logging.ERROR
            for record in caplog.records
            if record.name == "clambot.utils.tasks"
        )

    @pytest.mark.asyncio
    async def test_tracked_task_returns_asyncio_task(self) -> None:
        """Verify tracked_task returns an asyncio.Task."""

        async def _noop() -> None:
            pass

        task = tracked_task(_noop(), name="test-type")
        assert isinstance(task, asyncio.Task)
        await task

    @pytest.mark.asyncio
    async def test_tracked_task_cancelled_no_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify cancelled tasks don't produce error logs."""

        async def _forever() -> None:
            await asyncio.Event().wait()

        with caplog.at_level(logging.ERROR, logger="clambot.utils.tasks"):
            task = tracked_task(_forever(), name="test-cancel")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert not any(
            record.levelno >= logging.ERROR
            for record in caplog.records
            if record.name == "clambot.utils.tasks"
        )

    @pytest.mark.asyncio
    async def test_tracked_task_with_name(self) -> None:
        """Verify task name is set correctly."""

        async def _noop() -> None:
            pass

        task = tracked_task(_noop(), name="my-custom-name")
        assert task.get_name() == "my-custom-name"
        await task

    @pytest.mark.asyncio
    async def test_tracked_task_without_name(self) -> None:
        """Verify tracked_task works without explicit name."""

        async def _noop() -> None:
            pass

        task = tracked_task(_noop())
        assert isinstance(task, asyncio.Task)
        await task
