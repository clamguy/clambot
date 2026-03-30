"""EchoTool — debug tool that echoes its input back to the caller.

This tool is intentionally excluded from the default tool surface exposed to
the LLM.  It is useful for testing the tool dispatch pipeline end-to-end
without making any external calls.
"""

from __future__ import annotations

from typing import Any

from clambot.tools.base import BuiltinTool, ToolApprovalOption

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "EchoTool",
]

# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class EchoTool(BuiltinTool):
    """Debug tool that echoes its ``message`` argument back to the caller.

    Excluded from the default tool surface — register explicitly when needed
    for testing or debugging.
    """

    # ------------------------------------------------------------------
    # BuiltinTool interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Registered tool name: ``"echo"``."""
        return "echo"

    @property
    def description(self) -> str:
        """Human-readable description surfaced to the LLM."""
        return "Debug tool that echoes back its input. Excluded from default tool surface."

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema for the ``echo`` tool parameters."""
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message to echo back.",
                },
            },
            "required": ["message"],
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, args: dict[str, Any]) -> str:
        """Return the ``message`` argument unchanged.

        Args:
            args: Parameter dict matching :attr:`schema`.

        Returns:
            The value of ``args["message"]``, or an empty string if the key
            is absent.
        """
        return args.get("message", "")

    # ------------------------------------------------------------------
    # Approval options
    # ------------------------------------------------------------------

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Return an empty list — echo requires no approval.

        Args:
            args: Unused.

        Returns:
            Empty list.
        """
        return []
