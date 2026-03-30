"""Session log retention — prune old JSONL session files.

``prune_session_logs()`` caps the number of session JSONL files in the
sessions directory. Oldest files (by modification time) are deleted first.
"""

from __future__ import annotations

import logging
from pathlib import Path

__all__ = ["prune_session_logs"]

logger = logging.getLogger(__name__)


def prune_session_logs(
    sessions_dir: Path | str,
    max_files: int = 100,
) -> int:
    """Delete oldest session JSONL files when count exceeds ``max_files``.

    Args:
        sessions_dir: Directory containing ``*.jsonl`` session files.
        max_files: Maximum number of session files to keep.

    Returns:
        Number of files deleted.
    """
    sessions_dir = Path(sessions_dir)
    if not sessions_dir.is_dir():
        return 0

    # List all JSONL files sorted by modification time (oldest first)
    files = sorted(
        sessions_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )

    excess = len(files) - max_files
    if excess <= 0:
        return 0

    deleted = 0
    for f in files[:excess]:
        try:
            f.unlink()
            deleted += 1
            logger.debug("Pruned session log: %s", f.name)
        except OSError as exc:
            logger.warning("Failed to prune %s: %s", f.name, exc)

    logger.info("Pruned %d session log(s) from %s", deleted, sessions_dir)
    return deleted
