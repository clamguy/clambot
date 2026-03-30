"""Cron schedule parsing and next-run computation.

All schedule logic is self-contained — no external cron library is required.
The 5-field cron expression parser handles ``*``, ranges (``1-5``), steps
(``*/2``), and lists (``1,3,5``).

Timezone support uses :mod:`zoneinfo` (stdlib in Python 3.9+).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from clambot.cron.types import CronSchedule

__all__ = [
    "parse_schedule",
    "validate_cron_expression",
    "calculate_next_run_ms",
    "parse_duration_to_seconds",
    "parse_iso8601_to_epoch_ms",
]

# ---------------------------------------------------------------------------
# Cron expression parsing
# ---------------------------------------------------------------------------

_FIELD_RANGES: list[tuple[int, int]] = [
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week (0=Sun)
]


def _expand_field(field: str, lo: int, hi: int) -> set[int]:
    """Expand a single cron field into a set of matching integers.

    Supports: ``*``, ``N``, ``N-M``, ``*/S``, ``N-M/S``, ``N,M,O``.
    """
    result: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                start, end = (int(x) for x in base.split("-", 1))
            else:
                start, end = int(base), hi
            result.update(range(start, end + 1, step))
        elif "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            result.update(range(start, end + 1))
        elif part == "*":
            result.update(range(lo, hi + 1))
        else:
            result.add(int(part))
    return result


def validate_cron_expression(expr: str) -> bool:
    """Check whether *expr* is a valid 5-field cron expression.

    Returns ``True`` if the expression can be parsed without error,
    ``False`` otherwise.  Does not raise.
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        return False
    try:
        for i, field in enumerate(fields):
            lo, hi = _FIELD_RANGES[i]
            values = _expand_field(field, lo, hi)
            if not values:
                return False
            if any(v < lo or v > hi for v in values):
                return False
    except (ValueError, IndexError):
        return False
    return True


def _cron_next_match(
    expr: str,
    after: datetime,
    tz: ZoneInfo | None = None,
) -> datetime:
    """Find the next datetime matching the 5-field cron *expr* after *after*.

    Brute-force forward scan, minute by minute.  This is perfectly
    efficient for the expected scheduling frequencies.

    Args:
        expr: 5-field cron expression.
        after: Reference time (inclusive lower bound is ``after + 1 minute``).
        tz: Timezone to evaluate the expression in.

    Returns:
        The next matching :class:`datetime` (timezone-aware if *tz* given).

    Raises:
        ValueError: If *expr* is invalid.
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron expression: {expr!r}")

    expanded: list[set[int]] = []
    for i, f in enumerate(fields):
        lo, hi = _FIELD_RANGES[i]
        expanded.append(_expand_field(f, lo, hi))

    minutes, hours, doms, months, dows = expanded

    # Start from the next whole minute after *after*
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if tz is not None:
        candidate = candidate.astimezone(tz)

    # Safety: cap the search at 4 years to avoid infinite loops on
    # impossible expressions like "30 2 30 2 *".
    limit = candidate + timedelta(days=366 * 4)

    while candidate < limit:
        if (
            candidate.month in months
            and candidate.day in doms
            and candidate.weekday() in _convert_dow(dows)
            and candidate.hour in hours
            and candidate.minute in minutes
        ):
            return candidate
        candidate += timedelta(minutes=1)

    raise ValueError(f"No matching time found for cron expression: {expr!r}")


def _convert_dow(cron_dow: set[int]) -> set[int]:
    """Convert cron day-of-week (0=Sun) to Python weekday (0=Mon).

    Cron: 0=Sun, 1=Mon, …, 6=Sat
    Python: 0=Mon, 1=Tue, …, 6=Sun
    """
    py_dow: set[int] = set()
    for d in cron_dow:
        if d == 0:
            py_dow.add(6)  # Sun → 6
        else:
            py_dow.add(d - 1)  # Mon=1→0, …, Sat=6→5
    return py_dow


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------


def parse_schedule(data: dict) -> CronSchedule:
    """Create a :class:`CronSchedule` from a raw dict (tool args or JSON).

    Detects the schedule ``kind`` from which keys are present:
    - ``every_seconds`` → ``"every"``
    - ``cron_expr`` → ``"cron"``
    - ``at_ms`` → ``"at"``

    Raises:
        ValueError: If no recognised schedule key is present.
    """
    if "every_seconds" in data and data["every_seconds"] is not None:
        return CronSchedule(
            kind="every",
            every_seconds=int(data["every_seconds"]),
        )
    cron_expr = data.get("cron_expr") or data.get("cron")
    if cron_expr is not None:
        tz = data.get("timezone") or "UTC"
        return CronSchedule(
            kind="cron",
            cron_expr=cron_expr,
            timezone=tz,
        )
    if "at_ms" in data and data["at_ms"] is not None:
        return CronSchedule(
            kind="at",
            at_ms=int(data["at_ms"]),
        )
    raise ValueError(
        "Cannot determine schedule kind: provide 'every_seconds', 'cron_expr', or 'at_ms'"
    )


# ---------------------------------------------------------------------------
# Next-run calculation
# ---------------------------------------------------------------------------


def calculate_next_run_ms(
    schedule: CronSchedule,
    after_ms: int | None = None,
) -> int | None:
    """Compute the next run time in epoch milliseconds.

    Args:
        schedule: The schedule to evaluate.
        after_ms: Reference epoch ms (defaults to *now*).

    Returns:
        Next run time in epoch ms, or ``None`` if the schedule is exhausted
        (e.g. a one-time ``"at"`` job whose time has passed).
    """
    if after_ms is None:
        after_ms = int(time.time() * 1000)

    if schedule.kind == "every":
        if not schedule.every_seconds or schedule.every_seconds <= 0:
            return None
        return after_ms + schedule.every_seconds * 1000

    if schedule.kind == "cron" and schedule.cron_expr:
        tz = ZoneInfo(schedule.timezone) if schedule.timezone else ZoneInfo("UTC")
        after_dt = datetime.fromtimestamp(after_ms / 1000, tz=tz)
        try:
            next_dt = _cron_next_match(schedule.cron_expr, after_dt, tz)
            return int(next_dt.timestamp() * 1000)
        except ValueError:
            return None

    if schedule.kind == "at":
        if schedule.at_ms is not None and schedule.at_ms > after_ms:
            return schedule.at_ms
        return None

    return None


# ---------------------------------------------------------------------------
# CLI duration/time helpers
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h|d)$", re.IGNORECASE)
_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration_to_seconds(text: str) -> int:
    """Parse a human duration string like ``"60s"``, ``"5m"`` to seconds.

    Raises:
        ValueError: If the format is not recognised.
    """
    m = _DURATION_RE.match(text.strip())
    if not m:
        raise ValueError(f"Unrecognised duration format: {text!r}")
    return int(m.group(1)) * _DURATION_MULTIPLIERS[m.group(2).lower()]


def parse_iso8601_to_epoch_ms(text: str) -> int:
    """Parse an ISO-8601 datetime string to epoch milliseconds.

    Falls back to :func:`datetime.fromisoformat`.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    dt = datetime.fromisoformat(text)
    return int(dt.timestamp() * 1000)
