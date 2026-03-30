"""Memory store — MEMORY.md and HISTORY.md persistence.

Provides read/write access to the workspace's long-term memory files:
  - ``MEMORY.md``: Durable facts the agent should always remember
  - ``HISTORY.md``: Append-only conversation history log
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def memory_recall(workspace: Path) -> str:
    """Read the contents of MEMORY.md from the workspace.

    Returns empty string if the file doesn't exist.
    """
    memory_path = _resolve_memory_path(workspace)
    if memory_path.exists():
        try:
            return memory_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to read MEMORY.md: %s", exc)
            return ""
    return ""


def memory_save(workspace: Path, content: str) -> None:
    """Overwrite MEMORY.md with new content.

    Creates the file and parent directories if they don't exist.
    """
    memory_path = _resolve_memory_path(workspace)
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(content, encoding="utf-8")


def memory_append_history(workspace: Path, entry: str) -> None:
    """Append an entry to HISTORY.md.

    Each entry is separated by a blank line. Creates the file if missing.
    """
    history_path = _resolve_history_path(workspace)
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing content
    existing = ""
    if history_path.exists():
        try:
            existing = history_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to read HISTORY.md: %s", exc)

    # Append with separator
    separator = "\n\n" if existing.strip() else ""
    history_path.write_text(
        existing + separator + entry.strip() + "\n",
        encoding="utf-8",
    )


def memory_search_history(
    workspace: Path,
    query: str,
    limit: int = 10,
) -> list[str]:
    """Search HISTORY.md for entries matching the query substring.

    Entries are separated by double newlines. Returns up to ``limit``
    matching entries, most recent first.
    """
    history_path = _resolve_history_path(workspace)
    if not history_path.exists():
        return []

    try:
        content = history_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to read HISTORY.md for search: %s", exc)
        return []

    if not content.strip():
        return []

    # Split into entries (separated by blank lines)
    entries = [e.strip() for e in content.split("\n\n") if e.strip()]

    # Filter by query (case-insensitive substring match)
    query_lower = query.lower()
    matches = [e for e in entries if query_lower in e.lower()]

    # Return most recent first, limited
    return list(reversed(matches))[:limit]


def _resolve_memory_path(workspace: Path) -> Path:
    """Resolve the MEMORY.md path, checking memory/ subdirectory first."""
    memory_dir = Path(workspace) / "memory"
    if (memory_dir / "MEMORY.md").exists():
        return memory_dir / "MEMORY.md"
    # Default to memory/ subdirectory
    return memory_dir / "MEMORY.md"


def _resolve_history_path(workspace: Path) -> Path:
    """Resolve the HISTORY.md path, checking memory/ subdirectory first."""
    memory_dir = Path(workspace) / "memory"
    if (memory_dir / "HISTORY.md").exists():
        return memory_dir / "HISTORY.md"
    return memory_dir / "HISTORY.md"
