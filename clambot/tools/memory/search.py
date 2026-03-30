"""MemorySearchHistoryTool — built-in tool for searching conversation history.

Reads the agent's ``HISTORY.md`` file from the workspace, splits it into
individual entries, and returns those that contain the query string
(case-insensitive substring match).  Returns an empty list if the file does
not exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clambot.tools.base import BuiltinTool, ToolApprovalOption

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "MemorySearchHistoryTool",
]

# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class MemorySearchHistoryTool(BuiltinTool):
    """Built-in tool that searches conversation history stored in ``HISTORY.md``.

    The history file is expected at ``<workspace>/memory/HISTORY.md``.
    Entries are delimited by ``---`` separators or double newlines.  A
    case-insensitive substring search is performed against each entry and
    matching entries are returned up to *limit*.

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
        """Registered tool name: ``"memory_search_history"``."""
        return "memory_search_history"

    @property
    def description(self) -> str:
        """Human-readable description surfaced to the LLM."""
        return "Search conversation history for specific topics or information."

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema for the ``memory_search_history`` tool parameters."""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string (case-insensitive substring match).",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "description": "Maximum number of matching entries to return.",
                },
            },
            "required": ["query"],
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, args: dict[str, Any]) -> list[str]:
        """Search ``HISTORY.md`` for entries matching *query*.

        Args:
            args: Parameter dict matching :attr:`schema`.

        Returns:
            List of matching entry strings (stripped of leading/trailing
            whitespace), up to *limit* entries.  Returns an empty list if
            the history file does not exist or no entries match.
        """
        query: str = args.get("query", "")
        limit: int = int(args.get("limit", 10))

        history_path = self._workspace / "memory" / "HISTORY.md"
        if not history_path.exists():
            return []

        content = history_path.read_text(encoding="utf-8")

        # ------------------------------------------------------------------
        # Split into entries
        # ------------------------------------------------------------------
        # Primary delimiter: "---" separator lines (Markdown horizontal rule)
        # Fallback: double newlines
        raw_entries = content.split("---") if "---" in content else content.split("\n\n")

        # ------------------------------------------------------------------
        # Filter and return
        # ------------------------------------------------------------------
        query_lower = query.lower()
        matches: list[str] = []
        for entry in raw_entries:
            stripped = entry.strip()
            if not stripped:
                continue
            if query_lower in stripped.lower():
                matches.append(stripped)
                if len(matches) >= limit:
                    break

        return matches

    # ------------------------------------------------------------------
    # Approval options
    # ------------------------------------------------------------------

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Return an empty list — memory_search_history is read-only.

        Args:
            args: Unused.

        Returns:
            Empty list.
        """
        return []
