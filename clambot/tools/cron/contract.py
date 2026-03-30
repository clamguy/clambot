"""Contract constants for the cron tool.

Centralises the tool name and supported action names so they can be imported
by both the tool implementation and tests without creating circular
dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "CronToolContract",
]

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CronToolContract:
    """Immutable contract constants for the ``cron`` tool.

    Attributes:
        TOOL_NAME: The registered tool name used in LLM function calls.
        ACTIONS: Tuple of supported action strings.
    """

    TOOL_NAME: str = "cron"
    ACTIONS: tuple[str, ...] = ("add", "list", "remove")
