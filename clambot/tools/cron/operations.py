"""CronTool — built-in tool stub for scheduling recurring or one-time tasks.

This is a **Phase 5 stub**.  The full cron service integration is wired in
Phase 11.  Until then, all mutating actions return a "not configured" message
unless a *sync_hook* callback has been injected via :meth:`CronTool.set_sync_hook`.

The *sync_hook* interface is intentionally minimal: it receives the raw
``args`` dict and returns whatever the cron service produces.  Phase 11 will
replace the stub responses by wiring a real hook at startup.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from clambot.tools.base import BuiltinTool, ToolApprovalOption
from clambot.tools.cron.approval import get_cron_approval_options
from clambot.tools.cron.contract import CronToolContract

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "CronTool",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONTRACT = CronToolContract()

# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class CronTool(BuiltinTool):
    """Built-in tool stub for scheduling recurring or one-time tasks.

    Full implementation is deferred to Phase 11.  Until a *sync_hook* is
    injected, all mutating actions (``add`` / ``remove``) return a stub
    "not configured" message.  ``list`` returns a stub empty-list message.

    Args:
        _sync_hook: Optional callable that accepts the raw ``args`` dict and
                    returns a result dict.  Injected by the Phase 11 runtime
                    via :meth:`set_sync_hook`.
    """

    def __init__(
        self,
        _sync_hook: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._sync_hook = _sync_hook

    # ------------------------------------------------------------------
    # Phase 11 wiring
    # ------------------------------------------------------------------

    def set_sync_hook(
        self,
        hook: Callable[[dict[str, Any]], Any],
    ) -> None:
        """Inject the cron-service sync hook at runtime.

        Called by the Phase 11 runtime to wire the real cron service into
        this tool without requiring a new instance.

        Args:
            hook: Callable that accepts the raw ``args`` dict and returns a
                  result value.
        """
        self._sync_hook = hook

    # ------------------------------------------------------------------
    # BuiltinTool interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Registered tool name: ``"cron"``."""
        return _CONTRACT.TOOL_NAME

    @property
    def description(self) -> str:
        """Human-readable description surfaced to the LLM."""
        return "Schedule recurring or one-time tasks."

    @property
    def returns(self) -> dict[str, Any]:
        """Return value schema for ``cron``."""
        return {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "description": "True on success (add/remove)."},
                "message": {"type": "string", "description": "Status message."},
                "jobs": {
                    "type": "array",
                    "description": "List of scheduled jobs (for 'list' action).",
                },
            },
        }

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema for the ``cron`` tool parameters."""
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_CONTRACT.ACTIONS),
                    "description": "Cron action to perform: add, list, or remove.",
                },
                "message": {
                    "type": "string",
                    "description": "Message or task description for the scheduled job.",
                },
                "every_seconds": {
                    "type": "number",
                    "description": "Repeat interval in seconds (for recurring jobs).",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression (e.g. '0 9 * * 1') for scheduling.",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone name (e.g. 'America/New_York').",
                },
                "at_ms": {
                    "type": "integer",
                    "description": "Unix timestamp in milliseconds for a one-time job.",
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable job name (defaults to first 40 chars of message).",  # noqa: E501
                },
                "clam_id": {
                    "type": "string",
                    "description": "Name of a promoted clam to execute directly on each cron fire (skips the full agent pipeline). If omitted the first execution generates and caches the clam automatically.",  # noqa: E501
                },
                "delete_after_run": {
                    "type": "boolean",
                    "description": "If true, remove the job after it fires (useful for one-shot reminders). Default false.",  # noqa: E501
                },
                "job_id": {
                    "type": "string",
                    "description": "Job identifier (required for 'remove' action).",
                },
            },
            "required": ["action"],
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, args: dict[str, Any]) -> Any:
        """Execute the cron action described by *args*.

        If a *sync_hook* has been injected, it is called with *args* and its
        return value is forwarded directly.  Otherwise a stub response is
        returned.

        Args:
            args: Parameter dict matching :attr:`schema`.

        Returns:
            Result from the sync hook, or a stub string when no hook is
            configured.
        """
        action = args.get("action", "")

        # Delegate to real cron service if wired
        if self._sync_hook is not None:
            return self._sync_hook(args)

        # ------------------------------------------------------------------
        # Stub responses (Phase 5 — no cron service configured)
        # ------------------------------------------------------------------
        if action == "list":
            return {"jobs": [], "message": "No cron service configured."}

        if action == "add":
            return {
                "ok": False,
                "message": "Cron service not configured. Job not scheduled.",
            }

        if action == "remove":
            return {
                "ok": False,
                "message": "Cron service not configured.",
            }

        # Unknown action
        return {
            "ok": False,
            "message": f"Unknown cron action: '{action}'.",
        }

    # ------------------------------------------------------------------
    # Approval options
    # ------------------------------------------------------------------

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Delegate to :func:`~clambot.tools.cron.approval.get_cron_approval_options`.

        Args:
            args: The tool argument dict.

        Returns:
            Ordered list of approval scope options (empty for ``list``).
        """
        return get_cron_approval_options(args)
