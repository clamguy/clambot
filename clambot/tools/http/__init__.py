"""HTTP request tool — built-in tool for making HTTP requests.

Re-exports :class:`HttpRequestTool` as the primary public symbol for this
sub-package.  Internal helpers (``operations``, ``approval``, ``contract``)
are importable directly from their respective modules when needed.
"""

from __future__ import annotations

from clambot.tools.http.core import HttpRequestTool

__all__ = [
    "HttpRequestTool",
]
