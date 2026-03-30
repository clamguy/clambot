"""ClamBot filesystem tool package.

Re-exports the public surface of the filesystem tool so callers can import
directly from ``clambot.tools.filesystem`` without knowing the internal
module layout::

    from clambot.tools.filesystem import FilesystemTool, FilesystemToolContract

:class:`~clambot.config.schema.FilesystemToolConfig` lives in
``clambot.config.schema`` and is re-exported here for convenience.
"""

from __future__ import annotations

from clambot.config.schema import FilesystemToolConfig
from clambot.tools.filesystem.contract import FilesystemToolContract
from clambot.tools.filesystem.core import FilesystemTool

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "FilesystemTool",
    "FilesystemToolConfig",
    "FilesystemToolContract",
]
