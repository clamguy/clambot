"""Approval scope helpers for the cron tool.

Generates the list of :class:`~clambot.tools.base.ToolApprovalOption` objects
that are presented to the user before a ``cron`` tool call executes.  Two
granularity levels are offered: the specific action, and a wildcard that
covers all cron operations.

``list`` actions do not require approval and return an empty list.
"""

from __future__ import annotations

from typing import Any

from clambot.tools.base import ToolApprovalOption

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "get_cron_approval_options",
]

# ---------------------------------------------------------------------------
# Approval options
# ---------------------------------------------------------------------------


def get_cron_approval_options(args: dict[str, Any]) -> list[ToolApprovalOption]:
    """Return approval scope options for a ``cron`` tool call.

    Two options are returned for mutating actions (``add`` / ``remove``):

    1. **Specific action** — allow this exact cron action forever.
    2. **Wildcard** — allow any cron operation forever.

    ``list`` is a read-only action and returns an empty list.

    Args:
        args: The tool argument dict containing at least ``"action"``.

    Returns:
        Ordered list of :class:`~clambot.tools.base.ToolApprovalOption`
        objects, or an empty list for read-only actions.
    """
    action = args.get("action", "")

    # Read-only actions need no approval
    if action == "list":
        return []

    return [
        ToolApprovalOption(
            id=f"cron:{action}",
            label=f"Allow Always: cron {action}",
            scope=f"cron:{action}",
        ),
        ToolApprovalOption(
            id="cron:*",
            label="Allow Always: any cron operation",
            scope="cron:*",
        ),
    ]
