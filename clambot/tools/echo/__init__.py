"""Echo tool — debug tool that echoes its input back to the caller.

Re-exports :class:`EchoTool` as the primary public symbol for this
sub-package.  This tool is excluded from the default tool surface and must
be registered explicitly when needed for testing or debugging.
"""

from __future__ import annotations

from clambot.tools.echo.echo import EchoTool

__all__ = [
    "EchoTool",
]
