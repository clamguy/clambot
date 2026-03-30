"""Schema contract constants for the ClamBot filesystem tool.

:class:`FilesystemToolContract` is a frozen dataclass that acts as a
single source of truth for the tool's name and supported operations.
Import it wherever you need to reference these values without creating a
hard dependency on the full :class:`~clambot.tools.filesystem.core.FilesystemTool`
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "FilesystemToolContract",
]

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilesystemToolContract:
    """Schema contract for the ``fs`` tool.

    Attributes:
        TOOL_NAME: The canonical tool name used in LLM function calls.
        OPERATIONS: Tuple of all supported operation strings.
    """

    TOOL_NAME: str = "fs"
    OPERATIONS: tuple[str, ...] = ("list", "read", "write", "edit")
