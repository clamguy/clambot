"""Schema contract for the secrets_add tool."""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = ["SecretsAddToolContract"]


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretsAddToolContract:
    """Constants and schema metadata for the ``secrets_add`` tool."""

    TOOL_NAME: str = "secrets_add"
