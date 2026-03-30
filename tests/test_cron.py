"""Tests for Phase 11 — Cron System.

Covers:
- cron/types.py — dataclass construction
- cron/schedule.py — parse_schedule, validate_cron_expression, calculate_next_run_ms
- cron/store.py — load_cron_store, save_cron_store roundtrip + atomic write
- cron/service.py — InMemoryCronService lifecycle, add/remove/enable/disable
- cron/audit.py — log_cron_event writes JSONL
- configure_cron_tool_runtime_sync_hook — wiring integration
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from clambot.cron.audit import log_cron_event
from clambot.cron.schedule import (
    calculate_next_run_ms,
    parse_duration_to_seconds,
    parse_iso8601_to_epoch_ms,
    parse_schedule,
    validate_cron_expression,
)
from clambot.cron.service import (
    InMemoryCronService,
    NotConfiguredCronService,
    configure_cron_tool_runtime_sync_hook,
)
from clambot.cron.store import load_cron_store, save_cron_store
from clambot.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronSchedule,
    CronStore,
)
from clambot.tools.cron.operations import CronTool

# ---------------------------------------------------------------------------
# cron/schedule.py — validate_cron_expression
# ---------------------------------------------------------------------------


class TestValidateCronExpression:
    """Validate 5-field cron expression parsing."""

    def test_valid_every_minute(self) -> None:
        assert validate_cron_expression("* * * * *") is True

    def test_valid_weekday_morning(self) -> None:
        assert validate_cron_expression("0 9 * * 1-5") is True

    def test_valid_step(self) -> None:
        assert validate_cron_expression("*/2 * * * *") is True

    def test_valid_list(self) -> None:
        assert validate_cron_expression("0 0 1,15 * *") is True

    def test_valid_range_with_step(self) -> None:
        assert validate_cron_expression("0-30/5 * * * *") is True

    def test_invalid_too_few_fields(self) -> None:
        assert validate_cron_expression("* * *") is False

    def test_invalid_too_many_fields(self) -> None:
        assert validate_cron_expression("* * * * * *") is False

    def test_invalid_out_of_range_minute(self) -> None:
        assert validate_cron_expression("60 * * * *") is False

    def test_invalid_out_of_range_hour(self) -> None:
        assert validate_cron_expression("* 25 * * *") is False

    def test_invalid_garbage(self) -> None:
        assert validate_cron_expression("foo bar baz qux quux") is False

    def test_empty_string(self) -> None:
        assert validate_cron_expression("") is False


# ---------------------------------------------------------------------------
# cron/schedule.py — calculate_next_run_ms
# ---------------------------------------------------------------------------


class TestCalculateNextRunMs:
    """Test next-run computation for every/cron/at schedules."""

    def test_every_schedule_correct_interval(self) -> None:
        """'every' schedule returns after_ms + interval."""
        sched = CronSchedule(kind="every", every_seconds=60)
        after = 1000000
        result = calculate_next_run_ms(sched, after)
        assert result == 1000000 + 60 * 1000

    def test_every_schedule_zero_returns_none(self) -> None:
        """'every' schedule with 0 seconds returns None."""
        sched = CronSchedule(kind="every", every_seconds=0)
        assert calculate_next_run_ms(sched, 1000000) is None

    def test_cron_schedule_next_match_utc(self) -> None:
        """'cron' schedule returns a future timestamp (UTC)."""
        # Every hour at minute 0
        sched = CronSchedule(kind="cron", cron_expr="0 * * * *", timezone="UTC")
        now_ms = int(time.time() * 1000)
        result = calculate_next_run_ms(sched, now_ms)
        assert result is not None
        assert result > now_ms

    def test_cron_schedule_with_timezone(self) -> None:
        """'cron' schedule respects timezone."""
        sched = CronSchedule(kind="cron", cron_expr="0 9 * * *", timezone="America/New_York")
        now_ms = int(time.time() * 1000)
        result = calculate_next_run_ms(sched, now_ms)
        assert result is not None
        assert result > now_ms

    def test_at_schedule_future_returns_at_ms(self) -> None:
        """'at' schedule returns at_ms if it's in the future."""
        future_ms = int(time.time() * 1000) + 3600_000
        sched = CronSchedule(kind="at", at_ms=future_ms)
        result = calculate_next_run_ms(sched, int(time.time() * 1000))
        assert result == future_ms

    def test_at_schedule_past_returns_none(self) -> None:
        """'at' schedule returns None if at_ms is in the past."""
        past_ms = int(time.time() * 1000) - 3600_000
        sched = CronSchedule(kind="at", at_ms=past_ms)
        result = calculate_next_run_ms(sched, int(time.time() * 1000))
        assert result is None


# ---------------------------------------------------------------------------
# cron/schedule.py — parse_schedule
# ---------------------------------------------------------------------------


class TestParseSchedule:
    """Test schedule detection from raw dicts."""

    def test_parse_every(self) -> None:
        result = parse_schedule({"every_seconds": 120})
        assert result.kind == "every"
        assert result.every_seconds == 120

    def test_parse_cron(self) -> None:
        result = parse_schedule({"cron_expr": "0 9 * * *", "timezone": "UTC"})
        assert result.kind == "cron"
        assert result.cron_expr == "0 9 * * *"
        assert result.timezone == "UTC"

    def test_parse_cron_defaults_to_utc(self) -> None:
        result = parse_schedule({"cron_expr": "0 9 * * *"})
        assert result.timezone == "UTC"

    def test_parse_at(self) -> None:
        result = parse_schedule({"at_ms": 1700000000000})
        assert result.kind == "at"
        assert result.at_ms == 1700000000000

    def test_parse_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot determine schedule kind"):
            parse_schedule({"foo": "bar"})


# ---------------------------------------------------------------------------
# cron/schedule.py — duration/time helpers
# ---------------------------------------------------------------------------


class TestDurationHelpers:
    """Test CLI duration parsing helpers."""

    def test_parse_seconds(self) -> None:
        assert parse_duration_to_seconds("60s") == 60

    def test_parse_minutes(self) -> None:
        assert parse_duration_to_seconds("5m") == 300

    def test_parse_hours(self) -> None:
        assert parse_duration_to_seconds("2h") == 7200

    def test_parse_days(self) -> None:
        assert parse_duration_to_seconds("1d") == 86400

    def test_parse_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_duration_to_seconds("abc")

    def test_parse_iso8601(self) -> None:
        result = parse_iso8601_to_epoch_ms("2025-01-01T00:00:00+00:00")
        assert isinstance(result, int)
        assert result > 0


# ---------------------------------------------------------------------------
# cron/store.py — roundtrip persistence
# ---------------------------------------------------------------------------


class TestCronStore:
    """Test save_cron_store + load_cron_store roundtrip."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Save and load a store with jobs; data matches."""
        store_path = tmp_path / "jobs.json"
        job = CronJob(
            id="abc12345",
            name="test-job",
            enabled=True,
            schedule=CronSchedule(kind="every", every_seconds=60),
            payload=CronPayload(message="hello"),
            state=CronJobState(next_run_at_ms=9999999),
            created_at_ms=1000,
            updated_at_ms=2000,
        )
        store = CronStore(jobs=[job])
        save_cron_store(store_path, store)

        loaded = load_cron_store(store_path)
        assert len(loaded.jobs) == 1
        j = loaded.jobs[0]
        assert j.id == "abc12345"
        assert j.name == "test-job"
        assert j.schedule.kind == "every"
        assert j.schedule.every_seconds == 60
        assert j.payload.message == "hello"
        assert j.state.next_run_at_ms == 9999999

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Loading from a missing file returns an empty store."""
        store = load_cron_store(tmp_path / "nonexistent.json")
        assert len(store.jobs) == 0

    def test_load_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        """Loading from a corrupt file returns an empty store."""
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        store = load_cron_store(bad)
        assert len(store.jobs) == 0

    def test_atomic_write_creates_parent(self, tmp_path: Path) -> None:
        """save_cron_store creates parent dirs if missing."""
        store_path = tmp_path / "sub" / "dir" / "jobs.json"
        save_cron_store(store_path, CronStore())
        assert store_path.exists()

    def test_all_schedule_types_roundtrip(self, tmp_path: Path) -> None:
        """All three schedule kinds survive serialization."""
        store_path = tmp_path / "jobs.json"
        jobs = [
            CronJob(
                id="a",
                name="every",
                schedule=CronSchedule(kind="every", every_seconds=300),
            ),
            CronJob(
                id="b",
                name="cron",
                schedule=CronSchedule(kind="cron", cron_expr="0 9 * * *", timezone="UTC"),
            ),
            CronJob(
                id="c",
                name="at",
                schedule=CronSchedule(kind="at", at_ms=1700000000000),
            ),
        ]
        save_cron_store(store_path, CronStore(jobs=jobs))
        loaded = load_cron_store(store_path)
        assert len(loaded.jobs) == 3
        assert loaded.jobs[0].schedule.kind == "every"
        assert loaded.jobs[1].schedule.kind == "cron"
        assert loaded.jobs[2].schedule.kind == "at"
        assert loaded.jobs[2].schedule.at_ms == 1700000000000


# ---------------------------------------------------------------------------
# cron/service.py — InMemoryCronService
# ---------------------------------------------------------------------------


class TestInMemoryCronService:
    """Test InMemoryCronService job management."""

    @pytest.fixture
    def service(self, tmp_path: Path) -> InMemoryCronService:
        store_path = tmp_path / "jobs.json"
        return InMemoryCronService(store_path, workspace=tmp_path)

    @pytest.mark.asyncio
    async def test_start_loads_empty_store(self, service: InMemoryCronService) -> None:
        await service.start()
        assert service.list_jobs() == []

    @pytest.mark.asyncio
    async def test_add_job_persists(self, service: InMemoryCronService, tmp_path: Path) -> None:
        await service.start()
        job = service.add_job(
            name="test",
            schedule=CronSchedule(kind="every", every_seconds=60),
            message="hello",
        )
        assert job.id is not None
        assert job.name == "test"

        # Verify persisted to disk
        loaded = load_cron_store(tmp_path / "jobs.json")
        assert len(loaded.jobs) == 1
        assert loaded.jobs[0].id == job.id

    @pytest.mark.asyncio
    async def test_add_job_wakes_change_event(self, service: InMemoryCronService) -> None:
        """add_job sets the _change_event."""
        await service.start()
        service._change_event.clear()
        service.add_job(
            name="wake",
            schedule=CronSchedule(kind="every", every_seconds=10),
            message="test",
        )
        assert service._change_event.is_set()

    @pytest.mark.asyncio
    async def test_remove_job(self, service: InMemoryCronService) -> None:
        await service.start()
        job = service.add_job(
            name="to-remove",
            schedule=CronSchedule(kind="every", every_seconds=60),
            message="bye",
        )
        assert service.remove_job(job.id) is True
        assert service.list_jobs() == []

    @pytest.mark.asyncio
    async def test_remove_nonexistent_returns_false(self, service: InMemoryCronService) -> None:
        await service.start()
        assert service.remove_job("nonexistent") is False

    @pytest.mark.asyncio
    async def test_remove_job_wakes_change_event(self, service: InMemoryCronService) -> None:
        """remove_job sets the _change_event."""
        await service.start()
        job = service.add_job(
            name="wake",
            schedule=CronSchedule(kind="every", every_seconds=10),
            message="test",
        )
        service._change_event.clear()
        service.remove_job(job.id)
        assert service._change_event.is_set()

    @pytest.mark.asyncio
    async def test_enable_disable_job(self, service: InMemoryCronService) -> None:
        await service.start()
        job = service.add_job(
            name="toggle",
            schedule=CronSchedule(kind="every", every_seconds=60),
            message="test",
        )
        disabled = service.disable_job(job.id)
        assert disabled is not None
        assert disabled.enabled is False
        assert disabled.state.next_run_at_ms is None

        enabled = service.enable_job(job.id)
        assert enabled is not None
        assert enabled.enabled is True
        assert enabled.state.next_run_at_ms is not None

    @pytest.mark.asyncio
    async def test_execute_job_updates_state(self, service: InMemoryCronService) -> None:
        """Executing a job updates last_run_at_ms and last_status."""
        await service.start()
        executor = AsyncMock(return_value=None)
        service.set_executor(executor)

        job = service.add_job(
            name="exec-test",
            schedule=CronSchedule(kind="every", every_seconds=60),
            message="run me",
        )
        await service.run_job(job.id)

        # Fetch the updated job
        jobs = service.list_jobs(include_disabled=True)
        updated = next(j for j in jobs if j.id == job.id)
        assert updated.state.last_status == "ok"
        assert updated.state.last_run_at_ms is not None

    @pytest.mark.asyncio
    async def test_execute_job_error_updates_state(self, service: InMemoryCronService) -> None:
        """A failing executor sets last_status to 'error'."""
        await service.start()
        executor = AsyncMock(side_effect=RuntimeError("boom"))
        service.set_executor(executor)

        job = service.add_job(
            name="fail-test",
            schedule=CronSchedule(kind="every", every_seconds=60),
            message="fail me",
        )
        await service.run_job(job.id)

        jobs = service.list_jobs(include_disabled=True)
        updated = next(j for j in jobs if j.id == job.id)
        assert updated.state.last_status == "error"
        assert "boom" in (updated.state.last_error or "")

    @pytest.mark.asyncio
    async def test_delete_after_run_removes_job(self, service: InMemoryCronService) -> None:
        """A delete_after_run 'at' job is removed after execution."""
        await service.start()
        executor = AsyncMock(return_value=None)
        service.set_executor(executor)

        job = service.add_job(
            name="one-shot",
            schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 1_000_000),
            message="once",
            delete_after_run=True,
        )
        job_id = job.id

        await service.run_job(job_id)

        jobs = service.list_jobs(include_disabled=True)
        assert all(j.id != job_id for j in jobs)

    @pytest.mark.asyncio
    async def test_at_job_without_delete_disables(self, service: InMemoryCronService) -> None:
        """An 'at' job without delete_after_run is disabled after execution."""
        await service.start()
        executor = AsyncMock(return_value=None)
        service.set_executor(executor)

        job = service.add_job(
            name="disable-after",
            schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 1_000_000),
            message="once",
            delete_after_run=False,
        )
        await service.run_job(job.id)

        jobs = service.list_jobs(include_disabled=True)
        updated = next(j for j in jobs if j.id == job.id)
        assert updated.enabled is False

    @pytest.mark.asyncio
    async def test_scheduler_loop_fires_due_jobs(self, service: InMemoryCronService) -> None:
        """The _run loop fires jobs whose next_run_at_ms has passed."""
        await service.start()
        executor = AsyncMock(return_value=None)
        service.set_executor(executor)

        # Add a job due in the past
        job = service.add_job(
            name="due-now",
            schedule=CronSchedule(kind="every", every_seconds=1),
            message="fire!",
        )
        # Force next_run to past
        job.state.next_run_at_ms = int(time.time() * 1000) - 1000

        # Run one iteration of the loop
        async def run_one_tick():
            service._running = True
            # Manually execute the due-jobs logic
            now = int(time.time() * 1000)
            due = [
                j
                for j in service._store.jobs
                if j.enabled and j.state.next_run_at_ms and j.state.next_run_at_ms <= now
            ]
            for j in due:
                await service._execute_job(j)

        await run_one_tick()
        executor.assert_called_once()


# ---------------------------------------------------------------------------
# cron/service.py — NotConfiguredCronService
# ---------------------------------------------------------------------------


class TestNotConfiguredCronService:
    """NotConfiguredCronService is a safe no-op."""

    @pytest.mark.asyncio
    async def test_start_does_nothing(self) -> None:
        svc = NotConfiguredCronService()
        await svc.start()

    def test_list_jobs_empty(self) -> None:
        assert NotConfiguredCronService().list_jobs() == []

    def test_remove_returns_false(self) -> None:
        assert NotConfiguredCronService().remove_job("x") is False

    @pytest.mark.asyncio
    async def test_run_job_returns_false(self) -> None:
        assert await NotConfiguredCronService().run_job("x") is False


# ---------------------------------------------------------------------------
# cron/audit.py — log_cron_event
# ---------------------------------------------------------------------------


class TestAuditLog:
    """Test cron audit JSONL logging."""

    def test_log_event_creates_file(self, tmp_path: Path) -> None:
        log_cron_event(tmp_path, "cron_executor_started", "j1", "test-job")
        log_path = tmp_path / "logs" / "gateway_cron_events.jsonl"
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "cron_executor_started"
        assert entry["job_id"] == "j1"

    def test_log_event_appends(self, tmp_path: Path) -> None:
        log_cron_event(tmp_path, "cron_executor_started", "j1", "job1")
        log_cron_event(tmp_path, "cron_executor_error", "j2", "job2", error="fail")
        log_path = tmp_path / "logs" / "gateway_cron_events.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[1])["error"] == "fail"


# ---------------------------------------------------------------------------
# configure_cron_tool_runtime_sync_hook — wiring
# ---------------------------------------------------------------------------


class TestSyncHookWiring:
    """Test that configure_cron_tool_runtime_sync_hook wires the tool correctly."""

    @pytest.mark.asyncio
    async def test_list_via_hook(self, tmp_path: Path) -> None:
        """Wired tool delegates list to service."""
        svc = InMemoryCronService(tmp_path / "jobs.json", workspace=tmp_path)
        await svc.start()
        tool = CronTool()
        configure_cron_tool_runtime_sync_hook(tool, svc)

        result = tool.execute({"action": "list"})
        assert isinstance(result, dict)
        assert "jobs" in result
        assert isinstance(result["jobs"], list)

    @pytest.mark.asyncio
    async def test_add_via_hook(self, tmp_path: Path) -> None:
        """Wired tool can add a job."""
        svc = InMemoryCronService(tmp_path / "jobs.json", workspace=tmp_path)
        await svc.start()
        tool = CronTool()
        configure_cron_tool_runtime_sync_hook(tool, svc)

        result = tool.execute(
            {
                "action": "add",
                "message": "Say hello",
                "every_seconds": 300,
            }
        )
        assert result["ok"] is True
        assert "job_id" in result

    @pytest.mark.asyncio
    async def test_add_missing_message_via_hook(self, tmp_path: Path) -> None:
        """Wired tool returns error when message is missing."""
        svc = InMemoryCronService(tmp_path / "jobs.json", workspace=tmp_path)
        await svc.start()
        tool = CronTool()
        configure_cron_tool_runtime_sync_hook(tool, svc)

        result = tool.execute(
            {
                "action": "add",
                "every_seconds": 300,
            }
        )
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_remove_via_hook(self, tmp_path: Path) -> None:
        """Wired tool can remove a job."""
        svc = InMemoryCronService(tmp_path / "jobs.json", workspace=tmp_path)
        await svc.start()
        tool = CronTool()
        configure_cron_tool_runtime_sync_hook(tool, svc)

        add_result = tool.execute(
            {
                "action": "add",
                "message": "to delete",
                "every_seconds": 60,
            }
        )
        job_id = add_result["job_id"]

        rm_result = tool.execute({"action": "remove", "job_id": job_id})
        assert rm_result["ok"] is True

    @pytest.mark.asyncio
    async def test_remove_nonexistent_via_hook(self, tmp_path: Path) -> None:
        """Removing a nonexistent job returns ok=False."""
        svc = InMemoryCronService(tmp_path / "jobs.json", workspace=tmp_path)
        await svc.start()
        tool = CronTool()
        configure_cron_tool_runtime_sync_hook(tool, svc)

        result = tool.execute({"action": "remove", "job_id": "nope"})
        assert result["ok"] is False
