"""Approval-scope helpers for the ClamBot filesystem tool.

Provides :func:`get_filesystem_approval_options`, which builds the ordered
list of :class:`~clambot.tools.base.ToolApprovalOption` objects presented to
the user before a write or edit operation is executed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clambot.tools.base import ToolApprovalOption

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "get_filesystem_approval_options",
]

# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


def _readable_path(abs_path: str) -> str:
    """Return a human-readable version of an absolute path.

    Replaces the home directory prefix with ``~`` for readability in
    approval labels, e.g. ``/home/user/.clambot/workspace/data.txt``
    becomes ``~/.clambot/workspace/data.txt``.
    """
    home = str(Path.home())
    if abs_path == home or abs_path.startswith(home + "/"):
        return "~" + abs_path[len(home) :]
    return abs_path


def get_filesystem_approval_options(
    args: dict[str, Any],
    workspace: Path,
) -> list[ToolApprovalOption]:
    """Return approval scope options for a filesystem tool call.

    Expects *args* to have already been normalized by
    :meth:`FilesystemTool.normalize_args_for_approval` (Phase 2) so that
    ``args["path"]`` is an **absolute resolved** path.

    Two options are returned (in order of increasing breadth):

    1. **Exact file path** — ``file:<resolved_abs_path>``
    2. **Parent directory** — ``dir:<resolved_parent>`` (skipped when
       parent == path, e.g. root ``/``)

    Labels use ``~``-relative paths for readability when possible.

    Args:
        args: Pre-normalized argument dict (``"path"`` should be absolute).
        workspace: Resolved workspace root directory.

    Returns:
        Ordered list of :class:`~clambot.tools.base.ToolApprovalOption`
        objects from narrowest to broadest scope.
    """
    path_str = args.get("path", "")
    options: list[ToolApprovalOption] = []

    # Resolve to absolute in case normalize_args_for_approval was not called
    # (defensive — should always be absolute by this point).
    try:
        resolved = str(Path(path_str).resolve()) if path_str else ""
    except (ValueError, OSError):
        resolved = path_str

    # --- Option 1: exact file path ---
    label_path = _readable_path(resolved) if resolved else path_str
    options.append(
        ToolApprovalOption(
            id=f"file:{resolved}",
            label=f"Allow Always: exact path '{label_path}'",
            scope=f"file:{resolved}",
        )
    )

    # --- Option 2: parent directory ---
    # Skip when parent == path (root "/" or empty) and skip "/"
    # to avoid blanket root-level grants.
    if resolved:
        parent = str(Path(resolved).parent)
        if parent != resolved and parent != "/":
            label_parent = _readable_path(parent)
            options.append(
                ToolApprovalOption(
                    id=f"dir:{parent}",
                    label=f"Allow Always: directory '{label_parent}'",
                    scope=f"dir:{parent}",
                )
            )

    return options
