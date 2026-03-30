"""Approval options for the secrets_add tool."""

from __future__ import annotations

from typing import Any

from clambot.tools.base import ToolApprovalOption

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = ["get_secrets_approval_options"]


# ---------------------------------------------------------------------------
# Approval options builder
# ---------------------------------------------------------------------------


def get_secrets_approval_options(args: dict[str, Any]) -> list[ToolApprovalOption]:
    """Return approval scope options for the secrets_add tool.

    Options:
    - Exact secret name  (e.g. ``secret:MY_KEY``)
    - Any secrets operation (``secret:*``)
    """
    secret_name = args.get("name", "")
    options: list[ToolApprovalOption] = []

    if secret_name:
        options.append(
            ToolApprovalOption(
                id=f"secret:{secret_name}",
                label=f"Allow Always: secret '{secret_name}'",
                scope=f"secret:{secret_name}",
            )
        )

    options.append(
        ToolApprovalOption(
            id="secret:*",
            label="Allow Always: any secrets operation",
            scope="secret:*",
        )
    )

    return options
