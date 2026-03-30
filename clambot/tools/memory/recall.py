"""MemoryRecallTool — built-in tool for recalling long-term memory.

Reads the agent's persistent ``MEMORY.md`` file from the workspace and
returns its content to the LLM.  If the file does not exist an empty string
is returned rather than raising an error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clambot.tools.base import BuiltinTool, ToolApprovalOption

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "MemoryRecallTool",
]

# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class MemoryRecallTool(BuiltinTool):
    """Built-in tool that recalls stored long-term memory from ``MEMORY.md``.

    The memory file is expected at ``<workspace>/memory/MEMORY.md``.  If the
    file does not exist the tool returns an empty string without raising.

    Args:
        workspace: Path to the agent's workspace root directory.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    # ------------------------------------------------------------------
    # BuiltinTool interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Registered tool name: ``"memory_recall"``."""
        return "memory_recall"

    @property
    def description(self) -> str:
        """Human-readable description surfaced to the LLM."""
        return "Recall stored memory (long-term facts from MEMORY.md)."

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema for the ``memory_recall`` tool parameters.

        No parameters are required — the tool always reads the full memory
        file.
        """
        return {
            "type": "object",
            "properties": {},
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, args: dict[str, Any]) -> str:
        """Read and return the content of ``MEMORY.md``.

        Args:
            args: Unused — no parameters are accepted.

        Returns:
            The full text content of ``<workspace>/memory/MEMORY.md``, or an
            empty string if the file does not exist.
        """
        memory_path = self._workspace / "memory" / "MEMORY.md"
        if not memory_path.exists():
            return ""
        return memory_path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Approval options
    # ------------------------------------------------------------------

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Return an empty list — memory_recall is read-only.

        Args:
            args: Unused.

        Returns:
            Empty list.
        """
        return []
