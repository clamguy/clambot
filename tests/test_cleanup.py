"""Tests for workspace/cleanup.py — periodic workspace housekeeping.

Covers:
- _prune_stale_clams — removes old clams, skips never-executed ones
- _prune_orphan_builds — removes unpromoted build dirs older than threshold
- _prune_disabled_cron_jobs — removes disabled jobs from jobs.json
- _prune_old_uploads — removes old uploaded files
- _trim_cron_log — trims cron audit log to max_lines
- run_cleanup — integration: runs all tasks, returns correct stats
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal config stub (mirrors CleanupConfig attributes)
# ---------------------------------------------------------------------------


@dataclass
class _CleanupCfg:
    """Minimal stand-in for CleanupConfig used in run_cleanup tests."""

    stale_clam_days: int = 30
    orphan_build_hours: int = 1
    upload_retention_days: int = 30
    cron_log_max_lines: int = 5000
    prune_disabled_cron: bool = True
    session_max_files: int = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_mtime(path: Path, age_seconds: float) -> None:
    """Set a file/directory's mtime to ``now - age_seconds``."""
    t = time.time() - age_seconds
    os.utime(path, (t, t))


# ---------------------------------------------------------------------------
# _prune_stale_clams
# ---------------------------------------------------------------------------


class TestPruneStaleClams:
    """Tests for _prune_stale_clams()."""

    def test_removes_stale_clam_with_usage(self, tmp_path: Path) -> None:
        """Clam with usage_count > 0 and last_used older than threshold is removed."""
        from clambot.workspace.cleanup import _prune_stale_clams

        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()

        # Create a clam directory
        (clams_dir / "old_clam").mkdir()

        # Write usage record: used 60 days ago
        old_ts = time.time() - 60 * 86_400
        usage = {"old_clam": {"usage_count": 5, "last_used": old_ts}}
        (clams_dir / ".usage.json").write_text(json.dumps(usage))

        removed = _prune_stale_clams(tmp_path, max_age_days=30)

        assert removed == ["old_clam"]
        assert not (clams_dir / "old_clam").exists()

    def test_skips_clam_with_zero_usage_count(self, tmp_path: Path) -> None:
        """Clam with usage_count == 0 (never executed) is NOT removed."""
        from clambot.workspace.cleanup import _prune_stale_clams

        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()
        (clams_dir / "new_clam").mkdir()

        old_ts = time.time() - 60 * 86_400
        usage = {"new_clam": {"usage_count": 0, "last_used": old_ts}}
        (clams_dir / ".usage.json").write_text(json.dumps(usage))

        removed = _prune_stale_clams(tmp_path, max_age_days=30)

        assert removed == []
        assert (clams_dir / "new_clam").exists()

    def test_skips_clam_with_no_usage_record(self, tmp_path: Path) -> None:
        """Clam with no entry in .usage.json is skipped."""
        from clambot.workspace.cleanup import _prune_stale_clams

        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()
        (clams_dir / "mystery_clam").mkdir()

        # .usage.json exists but has no entry for mystery_clam
        usage: dict = {}
        (clams_dir / ".usage.json").write_text(json.dumps(usage))

        removed = _prune_stale_clams(tmp_path, max_age_days=30)

        assert removed == []
        assert (clams_dir / "mystery_clam").exists()

    def test_keeps_recently_used_clam(self, tmp_path: Path) -> None:
        """Clam used recently (within threshold) is NOT removed."""
        from clambot.workspace.cleanup import _prune_stale_clams

        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()
        (clams_dir / "active_clam").mkdir()

        recent_ts = time.time() - 5 * 86_400  # 5 days ago
        usage = {"active_clam": {"usage_count": 10, "last_used": recent_ts}}
        (clams_dir / ".usage.json").write_text(json.dumps(usage))

        removed = _prune_stale_clams(tmp_path, max_age_days=30)

        assert removed == []
        assert (clams_dir / "active_clam").exists()

    def test_updates_usage_json_after_removal(self, tmp_path: Path) -> None:
        """After removing a stale clam, its entry is removed from .usage.json."""
        from clambot.workspace.cleanup import _prune_stale_clams

        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()
        (clams_dir / "stale").mkdir()
        (clams_dir / "fresh").mkdir()

        old_ts = time.time() - 60 * 86_400
        recent_ts = time.time() - 5 * 86_400
        usage = {
            "stale": {"usage_count": 3, "last_used": old_ts},
            "fresh": {"usage_count": 7, "last_used": recent_ts},
        }
        usage_path = clams_dir / ".usage.json"
        usage_path.write_text(json.dumps(usage))

        _prune_stale_clams(tmp_path, max_age_days=30)

        updated = json.loads(usage_path.read_text())
        assert "stale" not in updated
        assert "fresh" in updated

    def test_does_nothing_if_clams_dir_missing(self, tmp_path: Path) -> None:
        """Returns empty list when clams/ directory does not exist."""
        from clambot.workspace.cleanup import _prune_stale_clams

        removed = _prune_stale_clams(tmp_path, max_age_days=30)

        assert removed == []

    def test_does_nothing_if_usage_json_missing(self, tmp_path: Path) -> None:
        """Returns empty list when .usage.json does not exist."""
        from clambot.workspace.cleanup import _prune_stale_clams

        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()
        (clams_dir / "some_clam").mkdir()
        # No .usage.json created

        removed = _prune_stale_clams(tmp_path, max_age_days=30)

        assert removed == []
        assert (clams_dir / "some_clam").exists()

    def test_skips_non_directory_entries_in_clams(self, tmp_path: Path) -> None:
        """Non-directory entries (files) in clams/ are ignored."""
        from clambot.workspace.cleanup import _prune_stale_clams

        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()
        # A plain file, not a clam dir
        (clams_dir / "README.txt").write_text("docs")

        old_ts = time.time() - 60 * 86_400
        usage = {"README.txt": {"usage_count": 1, "last_used": old_ts}}
        (clams_dir / ".usage.json").write_text(json.dumps(usage))

        removed = _prune_stale_clams(tmp_path, max_age_days=30)

        assert removed == []
        assert (clams_dir / "README.txt").exists()

    def test_multiple_stale_clams_all_removed(self, tmp_path: Path) -> None:
        """Multiple stale clams are all removed in one pass."""
        from clambot.workspace.cleanup import _prune_stale_clams

        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()

        old_ts = time.time() - 60 * 86_400
        usage = {}
        for name in ("alpha", "beta", "gamma"):
            (clams_dir / name).mkdir()
            usage[name] = {"usage_count": 2, "last_used": old_ts}
        (clams_dir / ".usage.json").write_text(json.dumps(usage))

        removed = _prune_stale_clams(tmp_path, max_age_days=30)

        assert sorted(removed) == ["alpha", "beta", "gamma"]
        for name in ("alpha", "beta", "gamma"):
            assert not (clams_dir / name).exists()


# ---------------------------------------------------------------------------
# _prune_orphan_builds
# ---------------------------------------------------------------------------


class TestPruneOrphanBuilds:
    """Tests for _prune_orphan_builds()."""

    def test_removes_old_orphan_build(self, tmp_path: Path) -> None:
        """Build dir older than threshold with no matching clam is removed."""
        from clambot.workspace.cleanup import _prune_orphan_builds

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        orphan = build_dir / "orphan_v1"
        orphan.mkdir()
        _set_mtime(orphan, 3 * 3600)  # 3 hours old

        removed = _prune_orphan_builds(tmp_path, max_age_hours=1)

        assert removed == ["orphan_v1"]
        assert not orphan.exists()

    def test_keeps_build_with_matching_promoted_clam(self, tmp_path: Path) -> None:
        """Build dir that has a matching clams/<name>/ dir is NOT removed."""
        from clambot.workspace.cleanup import _prune_orphan_builds

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()

        promoted = build_dir / "my_clam"
        promoted.mkdir()
        _set_mtime(promoted, 3 * 3600)  # old enough to be pruned otherwise

        # Matching promoted clam exists
        (clams_dir / "my_clam").mkdir()

        removed = _prune_orphan_builds(tmp_path, max_age_hours=1)

        assert removed == []
        assert promoted.exists()

    def test_keeps_build_newer_than_threshold(self, tmp_path: Path) -> None:
        """Build dir newer than threshold is NOT removed even without a clam."""
        from clambot.workspace.cleanup import _prune_orphan_builds

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        fresh = build_dir / "fresh_build"
        fresh.mkdir()
        _set_mtime(fresh, 10 * 60)  # only 10 minutes old

        removed = _prune_orphan_builds(tmp_path, max_age_hours=1)

        assert removed == []
        assert fresh.exists()

    def test_does_nothing_if_build_dir_missing(self, tmp_path: Path) -> None:
        """Returns empty list when build/ directory does not exist."""
        from clambot.workspace.cleanup import _prune_orphan_builds

        removed = _prune_orphan_builds(tmp_path, max_age_hours=1)

        assert removed == []

    def test_skips_files_in_build_dir(self, tmp_path: Path) -> None:
        """Non-directory entries (files) inside build/ are ignored."""
        from clambot.workspace.cleanup import _prune_orphan_builds

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        stray_file = build_dir / "stray.txt"
        stray_file.write_text("oops")
        _set_mtime(stray_file, 5 * 3600)

        removed = _prune_orphan_builds(tmp_path, max_age_hours=1)

        assert removed == []
        assert stray_file.exists()

    def test_removes_multiple_orphan_builds(self, tmp_path: Path) -> None:
        """Multiple old orphan builds are all removed."""
        from clambot.workspace.cleanup import _prune_orphan_builds

        build_dir = tmp_path / "build"
        build_dir.mkdir()

        for name in ("build_a", "build_b"):
            d = build_dir / name
            d.mkdir()
            _set_mtime(d, 5 * 3600)

        removed = _prune_orphan_builds(tmp_path, max_age_hours=1)

        assert sorted(removed) == ["build_a", "build_b"]


# ---------------------------------------------------------------------------
# _prune_disabled_cron_jobs
# ---------------------------------------------------------------------------


class TestPruneDisabledCronJobs:
    """Tests for _prune_disabled_cron_jobs()."""

    def test_removes_disabled_jobs(self, tmp_path: Path) -> None:
        """Disabled jobs (enabled=False) are removed from jobs.json."""
        from clambot.workspace.cleanup import _prune_disabled_cron_jobs

        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        jobs_path = cron_dir / "jobs.json"

        data = {
            "jobs": [
                {"id": "job1", "enabled": True, "schedule": "0 * * * *"},
                {"id": "job2", "enabled": False, "schedule": "@once"},
                {"id": "job3", "enabled": False, "schedule": "@once"},
            ]
        }
        jobs_path.write_text(json.dumps(data))

        removed = _prune_disabled_cron_jobs(tmp_path)

        assert removed == 2
        updated = json.loads(jobs_path.read_text())
        assert len(updated["jobs"]) == 1
        assert updated["jobs"][0]["id"] == "job1"

    def test_keeps_enabled_jobs_untouched(self, tmp_path: Path) -> None:
        """All enabled jobs are preserved."""
        from clambot.workspace.cleanup import _prune_disabled_cron_jobs

        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        jobs_path = cron_dir / "jobs.json"

        data = {
            "jobs": [
                {"id": "job1", "enabled": True},
                {"id": "job2", "enabled": True},
            ]
        }
        jobs_path.write_text(json.dumps(data))

        removed = _prune_disabled_cron_jobs(tmp_path)

        assert removed == 0
        updated = json.loads(jobs_path.read_text())
        assert len(updated["jobs"]) == 2

    def test_does_nothing_if_no_disabled_jobs(self, tmp_path: Path) -> None:
        """Returns 0 and does not rewrite file when no disabled jobs exist."""
        from clambot.workspace.cleanup import _prune_disabled_cron_jobs

        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        jobs_path = cron_dir / "jobs.json"

        data = {"jobs": [{"id": "job1", "enabled": True}]}
        original_text = json.dumps(data)
        jobs_path.write_text(original_text)

        removed = _prune_disabled_cron_jobs(tmp_path)

        assert removed == 0

    def test_does_nothing_if_jobs_json_missing(self, tmp_path: Path) -> None:
        """Returns 0 when jobs.json does not exist."""
        from clambot.workspace.cleanup import _prune_disabled_cron_jobs

        removed = _prune_disabled_cron_jobs(tmp_path)

        assert removed == 0

    def test_treats_missing_enabled_field_as_true(self, tmp_path: Path) -> None:
        """Jobs without an 'enabled' key default to enabled (kept)."""
        from clambot.workspace.cleanup import _prune_disabled_cron_jobs

        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        jobs_path = cron_dir / "jobs.json"

        data = {
            "jobs": [
                {"id": "job1"},  # no 'enabled' key — defaults to True
                {"id": "job2", "enabled": False},
            ]
        }
        jobs_path.write_text(json.dumps(data))

        removed = _prune_disabled_cron_jobs(tmp_path)

        assert removed == 1
        updated = json.loads(jobs_path.read_text())
        assert len(updated["jobs"]) == 1
        assert updated["jobs"][0]["id"] == "job1"

    def test_preserves_extra_fields_in_jobs_json(self, tmp_path: Path) -> None:
        """Top-level fields other than 'jobs' are preserved after rewrite."""
        from clambot.workspace.cleanup import _prune_disabled_cron_jobs

        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        jobs_path = cron_dir / "jobs.json"

        data = {
            "version": 2,
            "jobs": [
                {"id": "job1", "enabled": True},
                {"id": "job2", "enabled": False},
            ],
        }
        jobs_path.write_text(json.dumps(data))

        _prune_disabled_cron_jobs(tmp_path)

        updated = json.loads(jobs_path.read_text())
        assert updated["version"] == 2


# ---------------------------------------------------------------------------
# _prune_old_uploads
# ---------------------------------------------------------------------------


class TestPruneOldUploads:
    """Tests for _prune_old_uploads()."""

    def test_removes_old_files(self, tmp_path: Path) -> None:
        """Files older than threshold are removed."""
        from clambot.workspace.cleanup import _prune_old_uploads

        upload_dir = tmp_path / "upload"
        upload_dir.mkdir()

        old_file = upload_dir / "old_report.pdf"
        old_file.write_bytes(b"data")
        _set_mtime(old_file, 60 * 86_400)  # 60 days old

        removed = _prune_old_uploads(tmp_path, max_age_days=30)

        assert removed == 1
        assert not old_file.exists()

    def test_keeps_recent_files(self, tmp_path: Path) -> None:
        """Files newer than threshold are kept."""
        from clambot.workspace.cleanup import _prune_old_uploads

        upload_dir = tmp_path / "upload"
        upload_dir.mkdir()

        recent_file = upload_dir / "recent.png"
        recent_file.write_bytes(b"img")
        _set_mtime(recent_file, 5 * 86_400)  # 5 days old

        removed = _prune_old_uploads(tmp_path, max_age_days=30)

        assert removed == 0
        assert recent_file.exists()

    def test_does_nothing_if_upload_dir_missing(self, tmp_path: Path) -> None:
        """Returns 0 when upload/ directory does not exist."""
        from clambot.workspace.cleanup import _prune_old_uploads

        removed = _prune_old_uploads(tmp_path, max_age_days=30)

        assert removed == 0

    def test_removes_multiple_old_files(self, tmp_path: Path) -> None:
        """Multiple old files are all removed."""
        from clambot.workspace.cleanup import _prune_old_uploads

        upload_dir = tmp_path / "upload"
        upload_dir.mkdir()

        for name in ("a.txt", "b.csv", "c.zip"):
            f = upload_dir / name
            f.write_bytes(b"x")
            _set_mtime(f, 90 * 86_400)

        removed = _prune_old_uploads(tmp_path, max_age_days=30)

        assert removed == 3
        assert list(upload_dir.iterdir()) == []

    def test_mixed_old_and_recent_files(self, tmp_path: Path) -> None:
        """Only old files are removed; recent files are kept."""
        from clambot.workspace.cleanup import _prune_old_uploads

        upload_dir = tmp_path / "upload"
        upload_dir.mkdir()

        old_file = upload_dir / "old.txt"
        old_file.write_bytes(b"old")
        _set_mtime(old_file, 60 * 86_400)

        new_file = upload_dir / "new.txt"
        new_file.write_bytes(b"new")
        _set_mtime(new_file, 1 * 86_400)

        removed = _prune_old_uploads(tmp_path, max_age_days=30)

        assert removed == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_skips_subdirectories_in_upload(self, tmp_path: Path) -> None:
        """Subdirectories inside upload/ are not removed."""
        from clambot.workspace.cleanup import _prune_old_uploads

        upload_dir = tmp_path / "upload"
        upload_dir.mkdir()

        subdir = upload_dir / "subdir"
        subdir.mkdir()
        _set_mtime(subdir, 90 * 86_400)

        removed = _prune_old_uploads(tmp_path, max_age_days=30)

        assert removed == 0
        assert subdir.exists()


# ---------------------------------------------------------------------------
# _trim_cron_log
# ---------------------------------------------------------------------------


class TestTrimCronLog:
    """Tests for _trim_cron_log()."""

    def test_trims_log_to_max_lines(self, tmp_path: Path) -> None:
        """Log with more than max_lines is trimmed to the last max_lines."""
        from clambot.workspace.cleanup import _trim_cron_log

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        log_path = logs_dir / "gateway_cron_events.jsonl"

        lines = [json.dumps({"event": f"e{i}"}) for i in range(100)]
        log_path.write_text("\n".join(lines) + "\n")

        trimmed = _trim_cron_log(tmp_path, max_lines=50)

        assert trimmed == 50
        remaining = log_path.read_text().splitlines()
        assert len(remaining) == 50
        # Should keep the LAST 50 lines
        assert remaining[0] == json.dumps({"event": "e50"})
        assert remaining[-1] == json.dumps({"event": "e99"})

    def test_does_nothing_if_under_max_lines(self, tmp_path: Path) -> None:
        """Log with fewer lines than max_lines is left untouched."""
        from clambot.workspace.cleanup import _trim_cron_log

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        log_path = logs_dir / "gateway_cron_events.jsonl"

        lines = [json.dumps({"event": f"e{i}"}) for i in range(20)]
        original = "\n".join(lines) + "\n"
        log_path.write_text(original)

        trimmed = _trim_cron_log(tmp_path, max_lines=50)

        assert trimmed == 0
        assert log_path.read_text() == original

    def test_does_nothing_if_exactly_max_lines(self, tmp_path: Path) -> None:
        """Log with exactly max_lines lines is not trimmed."""
        from clambot.workspace.cleanup import _trim_cron_log

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        log_path = logs_dir / "gateway_cron_events.jsonl"

        lines = [json.dumps({"event": f"e{i}"}) for i in range(50)]
        original = "\n".join(lines) + "\n"
        log_path.write_text(original)

        trimmed = _trim_cron_log(tmp_path, max_lines=50)

        assert trimmed == 0

    def test_does_nothing_if_log_missing(self, tmp_path: Path) -> None:
        """Returns 0 when the cron log file does not exist."""
        from clambot.workspace.cleanup import _trim_cron_log

        trimmed = _trim_cron_log(tmp_path, max_lines=5000)

        assert trimmed == 0

    def test_trimmed_log_ends_with_newline(self, tmp_path: Path) -> None:
        """Trimmed log file ends with a trailing newline."""
        from clambot.workspace.cleanup import _trim_cron_log

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        log_path = logs_dir / "gateway_cron_events.jsonl"

        lines = [json.dumps({"event": f"e{i}"}) for i in range(100)]
        log_path.write_text("\n".join(lines) + "\n")

        _trim_cron_log(tmp_path, max_lines=10)

        content = log_path.read_text()
        assert content.endswith("\n")


# ---------------------------------------------------------------------------
# run_cleanup — integration
# ---------------------------------------------------------------------------


class TestRunCleanup:
    """Integration tests for run_cleanup()."""

    def test_returns_cleanup_stats_instance(self, tmp_path: Path) -> None:
        """run_cleanup returns a CleanupStats object."""
        from clambot.workspace.cleanup import CleanupStats, run_cleanup

        cfg = _CleanupCfg()
        stats = run_cleanup(tmp_path, cfg)

        assert isinstance(stats, CleanupStats)

    def test_fresh_empty_workspace_no_errors(self, tmp_path: Path) -> None:
        """run_cleanup on a completely empty workspace raises no errors."""
        from clambot.workspace.cleanup import run_cleanup

        cfg = _CleanupCfg()
        stats = run_cleanup(tmp_path, cfg)

        assert stats.stale_clams_removed == []
        assert stats.orphan_builds_removed == []
        assert stats.disabled_cron_jobs_removed == 0
        assert stats.uploads_removed == 0
        assert stats.cron_log_lines_trimmed == 0
        assert stats.sessions_pruned == 0

    def test_returns_correct_stale_clams_count(self, tmp_path: Path) -> None:
        """run_cleanup reports stale clams removed in stats."""
        from clambot.workspace.cleanup import run_cleanup

        clams_dir = tmp_path / "clams"
        clams_dir.mkdir()
        (clams_dir / "old_clam").mkdir()

        old_ts = time.time() - 60 * 86_400
        usage = {"old_clam": {"usage_count": 3, "last_used": old_ts}}
        (clams_dir / ".usage.json").write_text(json.dumps(usage))

        cfg = _CleanupCfg(stale_clam_days=30)
        stats = run_cleanup(tmp_path, cfg)

        assert stats.stale_clams_removed == ["old_clam"]

    def test_returns_correct_orphan_builds_count(self, tmp_path: Path) -> None:
        """run_cleanup reports orphan builds removed in stats."""
        from clambot.workspace.cleanup import run_cleanup

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        orphan = build_dir / "orphan_build"
        orphan.mkdir()
        _set_mtime(orphan, 5 * 3600)

        cfg = _CleanupCfg(orphan_build_hours=1)
        stats = run_cleanup(tmp_path, cfg)

        assert stats.orphan_builds_removed == ["orphan_build"]

    def test_returns_correct_disabled_cron_jobs_count(self, tmp_path: Path) -> None:
        """run_cleanup reports disabled cron jobs removed in stats."""
        from clambot.workspace.cleanup import run_cleanup

        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        jobs_path = cron_dir / "jobs.json"
        data = {
            "jobs": [
                {"id": "j1", "enabled": True},
                {"id": "j2", "enabled": False},
            ]
        }
        jobs_path.write_text(json.dumps(data))

        cfg = _CleanupCfg(prune_disabled_cron=True)
        stats = run_cleanup(tmp_path, cfg)

        assert stats.disabled_cron_jobs_removed == 1

    def test_skips_disabled_cron_pruning_when_flag_false(self, tmp_path: Path) -> None:
        """run_cleanup skips cron pruning when prune_disabled_cron=False."""
        from clambot.workspace.cleanup import run_cleanup

        cron_dir = tmp_path / "cron"
        cron_dir.mkdir()
        jobs_path = cron_dir / "jobs.json"
        data = {"jobs": [{"id": "j1", "enabled": False}]}
        jobs_path.write_text(json.dumps(data))

        cfg = _CleanupCfg(prune_disabled_cron=False)
        stats = run_cleanup(tmp_path, cfg)

        assert stats.disabled_cron_jobs_removed == 0
        # File should be unchanged
        assert json.loads(jobs_path.read_text())["jobs"][0]["enabled"] is False

    def test_returns_correct_uploads_removed_count(self, tmp_path: Path) -> None:
        """run_cleanup reports old uploads removed in stats."""
        from clambot.workspace.cleanup import run_cleanup

        upload_dir = tmp_path / "upload"
        upload_dir.mkdir()
        old_file = upload_dir / "old.bin"
        old_file.write_bytes(b"data")
        _set_mtime(old_file, 90 * 86_400)

        cfg = _CleanupCfg(upload_retention_days=30)
        stats = run_cleanup(tmp_path, cfg)

        assert stats.uploads_removed == 1

    def test_returns_correct_cron_log_lines_trimmed(self, tmp_path: Path) -> None:
        """run_cleanup reports cron log lines trimmed in stats."""
        from clambot.workspace.cleanup import run_cleanup

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        log_path = logs_dir / "gateway_cron_events.jsonl"
        lines = [json.dumps({"e": i}) for i in range(200)]
        log_path.write_text("\n".join(lines) + "\n")

        cfg = _CleanupCfg(cron_log_max_lines=100)
        stats = run_cleanup(tmp_path, cfg)

        assert stats.cron_log_lines_trimmed == 100

    def test_sessions_pruned_when_sessions_dir_exists(self, tmp_path: Path) -> None:
        """run_cleanup prunes session logs when sessions/ dir exists."""
        from clambot.workspace.cleanup import run_cleanup

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # Create more files than the limit
        for i in range(5):
            f = sessions_dir / f"session_{i}.jsonl"
            f.write_text(f"data-{i}")
            _set_mtime(f, (5 - i) * 100)

        cfg = _CleanupCfg(session_max_files=3)
        stats = run_cleanup(tmp_path, cfg)

        assert stats.sessions_pruned == 2

    def test_sessions_not_pruned_when_sessions_dir_missing(self, tmp_path: Path) -> None:
        """run_cleanup reports 0 sessions pruned when sessions/ dir is absent."""
        from clambot.workspace.cleanup import run_cleanup

        cfg = _CleanupCfg(session_max_files=3)
        stats = run_cleanup(tmp_path, cfg)

        assert stats.sessions_pruned == 0

    def test_uses_real_cleanup_config(self, tmp_path: Path) -> None:
        """run_cleanup works with the real CleanupConfig from schema."""
        from clambot.config.schema import CleanupConfig
        from clambot.workspace.cleanup import run_cleanup

        cfg = CleanupConfig()
        stats = run_cleanup(tmp_path, cfg)

        # Should complete without error on empty workspace
        assert stats.stale_clams_removed == []
        assert stats.orphan_builds_removed == []
