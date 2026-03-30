"""Error detail context builder for clam execution failures.

Produces structured context for the self-fix loop and post-runtime analysis,
including the error payload, clam metadata, and runtime result.
"""

from __future__ import annotations

from typing import Any

from clambot.utils.text import get_field

from .errors import ClamErrorPayload


def build_error_detail_context(
    error: ClamErrorPayload | None,
    clam: Any | None = None,
    result: Any | None = None,
) -> str:
    """Build a human-readable error detail context string.

    Used by the self-fix loop to provide the LLM with structured context
    about what went wrong during clam execution.

    Args:
        error: The error payload from the failed execution.
        clam: Optional clam object/dict for metadata context.
        result: Optional runtime result for output/stderr context.

    Returns:
        A formatted string with error details for LLM consumption.
    """
    parts: list[str] = []

    if error is not None:
        parts.append(f"ERROR CODE: {error.code}")
        parts.append(f"ERROR STAGE: {error.stage.value}")
        parts.append(f"ERROR MESSAGE: {error.message}")

        if error.detail:
            parts.append(f"ERROR DETAIL: {_safe_json(error.detail)}")

    if result is not None:
        # Extract from RuntimeResult or dict
        stderr = get_field(result, "stderr", "")
        output = get_field(result, "output", "")
        error_str = get_field(result, "error", "")

        if error_str and error is None:
            parts.append(f"RUNTIME ERROR: {error_str}")
        if stderr:
            parts.append(f"STDERR:\n{stderr[:2000]}")
        if output:
            parts.append(f"OUTPUT:\n{output[:2000]}")

    if clam is not None:
        script = get_field(clam, "script", "")
        if script:
            # Truncate for context
            parts.append(f"SCRIPT (first 3000 chars):\n{script[:3000]}")

    return "\n\n".join(parts) if parts else "No error details available."


def _safe_json(data: Any) -> str:
    """Safely serialize data to JSON string."""
    try:
        import json

        return json.dumps(data, default=str, ensure_ascii=False)
    except Exception:
        return str(data)
