"""Secrets — Secret store, value resolver, and secrets_add tool."""

from clambot.tools.secrets.env import resolve_secret_value
from clambot.tools.secrets.operations import SecretsAddTool
from clambot.tools.secrets.store import SecretRecord, SecretStore

__all__ = [
    "SecretRecord",
    "SecretStore",
    "SecretsAddTool",
    "resolve_secret_value",
]
