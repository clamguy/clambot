"""Policy violation codes and payloads for capability evaluation errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PolicyViolationCode(str, Enum):
    """Stable violation codes used by the capability evaluator."""

    CONSTRAINT_VIOLATION = "constraint_violation"
    MAX_CALLS_EXCEEDED = "max_calls_exceeded"
    UNDECLARED_TOOL = "undeclared_tool"
    POLICY_PARSE_ERROR = "policy_parse_error"


@dataclass(frozen=True)
class PolicyViolationPayload:
    """Structured payload describing a capability policy violation.

    Attributes:
        code: Stable violation code from PolicyViolationCode enum.
        tool_name: The tool that triggered the violation.
        message: Human-readable description of the violation.
        detail: Machine-readable context for diagnostics.
    """

    code: PolicyViolationCode
    tool_name: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
