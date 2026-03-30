"""Clam runtime error types and error codes.

Defines structured error payloads for every stage of clam execution:
pre-runtime checks, compatibility validation, and runtime execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Error stages
# ---------------------------------------------------------------------------


class ClamErrorStage(str, Enum):
    """Stage at which a clam error occurred."""

    PRE_RUNTIME = "PRE_RUNTIME"
    COMPATIBILITY = "COMPATIBILITY"
    RUNTIME = "RUNTIME"


# ---------------------------------------------------------------------------
# Error code constants
# ---------------------------------------------------------------------------

# Runtime errors
RUNTIME_TIMEOUT_UNRESPONSIVE = "runtime_timeout_unresponsive"
RUNTIME_EXECUTION_ERROR = "runtime_execution_error"

# Pre-runtime errors
PRE_RUNTIME_SECRET_REQUIREMENTS_UNRESOLVED = "pre_runtime_secret_requirements_unresolved"
INPUT_UNAVAILABLE = "input_unavailable"

# Compatibility errors
INCOMPATIBLE_LANGUAGE = "incompatible_language"

# Capability errors
CAPABILITY_VIOLATION = "capability_violation"

# HTTP tool errors
AUTHORIZATION_HEADER_CONFLICTS_WITH_AUTH = "authorization_header_conflicts_with_auth"

# All known error codes for reference
ALL_ERROR_CODES = (
    RUNTIME_TIMEOUT_UNRESPONSIVE,
    RUNTIME_EXECUTION_ERROR,
    PRE_RUNTIME_SECRET_REQUIREMENTS_UNRESOLVED,
    INPUT_UNAVAILABLE,
    INCOMPATIBLE_LANGUAGE,
    CAPABILITY_VIOLATION,
    AUTHORIZATION_HEADER_CONFLICTS_WITH_AUTH,
)


# ---------------------------------------------------------------------------
# Error payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClamErrorPayload:
    """Structured error payload for clam execution failures.

    All fields are immutable (frozen dataclass). Error codes are stable
    strings used by the self-fix loop to diagnose failures.

    Attributes:
        code: Stable error code string (e.g. ``"runtime_timeout_unresponsive"``).
        stage: The execution stage where the error occurred.
        message: Human-readable error summary.
        detail: Optional structured detail dict for diagnostics.
        user_message: Optional message safe to show to the end user.
    """

    code: str
    stage: ClamErrorStage
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    user_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON/logging."""
        return {
            "code": self.code,
            "stage": self.stage.value,
            "message": self.message,
            "detail": self.detail,
            "user_message": self.user_message,
        }
