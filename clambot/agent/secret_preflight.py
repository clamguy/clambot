"""Secret preflight check — resolves secret requirements before runtime.

Verifies that all secrets declared by a clam are available in the
SecretStore. Returns an error payload if any required secrets are missing.
"""

from __future__ import annotations

from typing import Any

from .errors import (
    PRE_RUNTIME_SECRET_REQUIREMENTS_UNRESOLVED,
    ClamErrorPayload,
    ClamErrorStage,
)


def resolve_pre_runtime_secret_requirements(
    clam: Any,
    secret_store: Any,
) -> ClamErrorPayload | None:
    """Check that all secrets required by a clam are available.

    Args:
        clam: A clam object or dict. Looks for ``secret_requirements`` in
              metadata — a list of secret name strings.
        secret_store: A :class:`~clambot.tools.secrets.store.SecretStore`
                      instance to check for secret existence.

    Returns:
        ``ClamErrorPayload`` if any required secrets are missing, ``None`` if
        all secrets are resolved.
    """
    requirements = _extract_secret_requirements(clam)

    if not requirements:
        return None

    missing: list[str] = []
    for secret_name in requirements:
        value = secret_store.get(secret_name)
        if value is None:
            missing.append(secret_name)

    if missing:
        return ClamErrorPayload(
            code=PRE_RUNTIME_SECRET_REQUIREMENTS_UNRESOLVED,
            stage=ClamErrorStage.PRE_RUNTIME,
            message=f"Missing required secrets: {', '.join(missing)}",
            detail={"missing_secrets": missing, "required_secrets": requirements},
            user_message=(
                f"This operation requires the following secrets to be configured: "
                f"{', '.join(missing)}. Reply with the value to provide."
            ),
        )

    return None


def _extract_secret_requirements(clam: Any) -> list[str]:
    """Extract secret requirements from a clam object or dict."""
    if isinstance(clam, dict):
        metadata = clam.get("metadata", {})
    else:
        metadata = getattr(clam, "metadata", {}) or {}

    if isinstance(metadata, dict):
        reqs = metadata.get("secret_requirements", [])
        if isinstance(reqs, list):
            return [str(r) for r in reqs]

    return []
