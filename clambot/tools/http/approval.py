"""Approval scope helpers for the HTTP request tool.

Generates the list of :class:`~clambot.tools.base.ToolApprovalOption` objects
that are presented to the user before an ``http_request`` tool call executes.
Three granularity levels are offered: exact URL+method, same hostname, and
same path prefix.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from clambot.tools.base import ToolApprovalOption

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "get_http_approval_options",
]

# ---------------------------------------------------------------------------
# Approval options
# ---------------------------------------------------------------------------


def get_http_approval_options(args: dict[str, Any]) -> list[ToolApprovalOption]:
    """Return approval scope options for an ``http_request`` tool call.

    Up to three options are returned, ordered from most specific to least:

    1. **Exact** — allow this exact method + URL combination forever.
    2. **Host** — allow any request to the same hostname forever.
    3. **Path prefix** — allow any request whose URL starts with the same
       scheme + netloc + path prefix forever.

    Args:
        args: The tool argument dict containing at least ``"url"`` and
              optionally ``"method"``.

    Returns:
        Ordered list of :class:`~clambot.tools.base.ToolApprovalOption`
        objects.  The list may be shorter than three entries if the URL
        cannot be parsed into a hostname or meaningful path.
    """
    url = args.get("url", "")
    method = args.get("method", "GET").upper()
    parsed = urlparse(url)

    options: list[ToolApprovalOption] = []

    # 1. Exact URL + method
    options.append(
        ToolApprovalOption(
            id=f"exact:{method}:{url}",
            label=f"Allow Always: {method} {url}",
            scope=f"exact:{method}:{url}",
        )
    )

    # 2. Same hostname
    if parsed.hostname:
        options.append(
            ToolApprovalOption(
                id=f"host:{parsed.hostname}",
                label=f"Allow Always: host {parsed.hostname}",
                scope=f"host:{parsed.hostname}",
            )
        )

    # 3. Same path prefix (only when there is a non-trivial path)
    if parsed.path and parsed.path != "/":
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        options.append(
            ToolApprovalOption(
                id=f"path:{base}",
                label=f"Allow Always: path {base}",
                scope=f"path:{base}",
            )
        )

    return options
