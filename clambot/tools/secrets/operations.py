"""Secrets add tool — store or update named secrets.

The ``secrets_add`` tool lets clams (and users) persist secrets in the
:class:`~clambot.tools.secrets.store.SecretStore`.  The actual secret
value is resolved through the priority chain defined in
:func:`~clambot.tools.secrets.env.resolve_secret_value`.
"""

from __future__ import annotations

from typing import Any

from clambot.tools.base import BuiltinTool, ToolApprovalOption
from clambot.tools.secrets.approval import get_secrets_approval_options
from clambot.tools.secrets.env import resolve_secret_value
from clambot.tools.secrets.store import SecretStore

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = ["SecretsAddTool"]


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class SecretsAddTool(BuiltinTool):
    """Store or update a named secret in the SecretStore."""

    def __init__(self, secret_store: SecretStore) -> None:
        self._secret_store = secret_store

    # -- BuiltinTool interface -----------------------------------------------

    @property
    def name(self) -> str:
        return "secrets_add"

    @property
    def description(self) -> str:
        return (
            "Store or update a named secret. The secret value is resolved "
            "from the provided value, an environment variable, or an "
            "interactive prompt."
        )

    @property
    def usage_instructions(self) -> list[str]:
        """Prompt guidance for generation-time secrets_add usage."""
        return [
            "Use to persist/update a secret before tools that require credentials.",
            "Pass name and either value or from_env (environment variable name).",
            "Optional description helps users manage stored secrets.",
            "Check result.ok/result.error and never echo secret values back to the user.",
        ]

    @property
    def returns(self) -> dict[str, Any]:
        """Return value schema for ``secrets_add``."""
        return {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "description": "True on success."},
                "message": {
                    "type": "string",
                    "description": "Success message (present when ok=true).",
                },
                "error": {
                    "type": "string",
                    "description": "Error message (present when ok=false).",
                },
            },
        }

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Unique name for the secret (e.g. 'OPENAI_API_KEY').",
                },
                "value": {
                    "type": "string",
                    "description": "Explicit secret value. If omitted, other resolution methods are tried.",  # noqa: E501
                },
                "from_env": {
                    "type": "string",
                    "description": "Name of an environment variable to read the secret from.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional human-readable description of the secret.",
                },
            },
            "required": ["name"],
        }

    def execute(self, args: dict[str, Any]) -> Any:
        """Resolve and store a secret."""
        secret_name: str = args.get("name", "")
        if not secret_name:
            return {"ok": False, "error": "Secret name is required."}

        desc: str = args.get("description", "")

        try:
            value = resolve_secret_value(
                name=secret_name,
                args=args,
                secret_store=self._secret_store,
            )
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}

        self._secret_store.save(secret_name, value, description=desc)
        return {"ok": True, "message": f"Secret '{secret_name}' stored."}

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Return approval scope options for secrets operations."""
        return get_secrets_approval_options(args)
