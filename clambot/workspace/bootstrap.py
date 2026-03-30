"""Workspace bootstrap — creates all directories and template files.

``bootstrap_workspace()`` is idempotent: safe to call on every startup.
It creates the directory tree and seed files only if they don't already exist.
"""

from __future__ import annotations

import logging
from pathlib import Path

__all__ = ["bootstrap_workspace"]

logger = logging.getLogger(__name__)

# Template content for seed files
_MEMORY_TEMPLATE = """\
# Memory

> Durable facts the agent should always remember.
"""

_HISTORY_TEMPLATE = """\
# History

> Append-only conversation history log.
"""

_HEARTBEAT_TEMPLATE = """\
# Heartbeat

> Add tasks below for the agent to execute periodically.
> Lines that are only headings, empty checkboxes, or whitespace are skipped.

- [ ]
"""

# Directories to create under the workspace root
_WORKSPACE_DIRS = [
    "clams",
    "build",
    "sessions",
    "logs",
    "docs",
    "memory",
    "upload",
]

# Seed files: (relative_path, template_content)
_SEED_FILES = [
    ("memory/MEMORY.md", _MEMORY_TEMPLATE),
    ("memory/HISTORY.md", _HISTORY_TEMPLATE),
    ("memory/HEARTBEAT.md", _HEARTBEAT_TEMPLATE),
]


def bootstrap_workspace(workspace_path: Path | str) -> None:
    """Create the workspace directory tree and seed files.

    This function is idempotent — calling it multiple times has no
    side effects beyond the initial creation.  Existing files are
    never overwritten.

    Args:
        workspace_path: Root workspace directory.
    """
    ws = Path(workspace_path).expanduser()

    # Create all directories
    for dirname in _WORKSPACE_DIRS:
        dirpath = ws / dirname
        dirpath.mkdir(parents=True, exist_ok=True)

    # Create seed files (only if missing)
    for relpath, template in _SEED_FILES:
        filepath = ws / relpath
        if not filepath.exists():
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(template, encoding="utf-8")
            logger.debug("Created seed file: %s", filepath)

    logger.info("Workspace bootstrapped at %s", ws)
