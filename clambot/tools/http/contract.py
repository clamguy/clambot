"""Contract constants for the HTTP request tool.

Centralises the tool name and sentinel values so they can be imported
by both the tool implementation and tests without creating circular
dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "HttpRequestToolContract",
]

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpRequestToolContract:
    """Immutable contract constants for the ``http_request`` tool.

    Attributes:
        TOOL_NAME: The registered tool name used in LLM function calls.
        REDACTED_VALUE: Sentinel string substituted wherever a secret value
            would otherwise appear in logs, events, or returned args.
    """

    TOOL_NAME: str = "http_request"
    REDACTED_VALUE: str = "[REDACTED_SECRET]"
