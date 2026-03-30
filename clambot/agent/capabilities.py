"""Capability policy evaluation with constraint DSL.

Evaluates clam-declared capability policies against actual tool calls.
Constraint types: is_in, starts_with, <=, >=, max_calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .policy_violations import PolicyViolationCode, PolicyViolationPayload


@dataclass
class CapabilityConstraint:
    """A single constraint on a tool argument.

    Attributes:
        param: The argument name this constraint applies to.
        op: The constraint operator: "is_in", "starts_with", "<=", ">=".
        value: The constraint value (list for is_in, string for starts_with, number for
            comparisons).
    """

    param: str
    op: str
    value: Any


@dataclass
class CapabilityPolicy:
    """Policy declared by a clam for a specific tool method.

    Attributes:
        method: Tool name this policy applies to.
        constraints: List of constraints that must ALL pass.
        max_calls: Maximum number of calls allowed (0 = unlimited).
    """

    method: str
    constraints: list[CapabilityConstraint] = field(default_factory=list)
    max_calls: int = 0


class CapabilityEvaluator:
    """Evaluates tool calls against clam-declared capability policies.

    Tracks call counts per tool and validates constraints on each call.
    """

    def __init__(self, policies: list[CapabilityPolicy] | None = None) -> None:
        self._policies: dict[str, CapabilityPolicy] = {}
        self._call_counts: dict[str, int] = {}
        if policies:
            for policy in policies:
                self._policies[policy.method] = policy

    @classmethod
    def from_clam_metadata(cls, metadata: dict[str, Any]) -> CapabilityEvaluator:
        """Parse capability policies from clam metadata.

        Expected format in metadata:
        {
            "capabilities": [
                {
                    "method": "fs",
                    "constraints": [
                        {"param": "operation", "op": "is_in", "value": ["read", "list"]},
                        {"param": "path", "op": "starts_with", "value": "/data"}
                    ],
                    "max_calls": 10
                }
            ]
        }
        """
        policies = []
        caps = metadata.get("capabilities", [])
        for cap in caps:
            constraints = []
            for c in cap.get("constraints", []):
                constraints.append(
                    CapabilityConstraint(
                        param=c["param"],
                        op=c["op"],
                        value=c["value"],
                    )
                )
            policies.append(
                CapabilityPolicy(
                    method=cap["method"],
                    constraints=constraints,
                    max_calls=cap.get("max_calls", 0),
                )
            )
        return cls(policies)

    def evaluate(
        self,
        tool_name: str,
        args: dict[str, Any],
        call_count: int | None = None,
    ) -> PolicyViolationPayload | None:
        """Evaluate a tool call against declared policies.

        Args:
            tool_name: The tool being called.
            args: Arguments for the call.
            call_count: Optional override for call count (otherwise tracked internally).

        Returns:
            PolicyViolationPayload if a violation is detected, None if the call is allowed.
        """
        policy = self._policies.get(tool_name)
        if policy is None:
            # No policy declared for this tool — allowed by default
            return None

        # Track call count
        if call_count is None:
            self._call_counts[tool_name] = self._call_counts.get(tool_name, 0) + 1
            call_count = self._call_counts[tool_name]

        # Check max_calls
        if policy.max_calls > 0 and call_count > policy.max_calls:
            return PolicyViolationPayload(
                code=PolicyViolationCode.MAX_CALLS_EXCEEDED,
                tool_name=tool_name,
                message=f"Tool '{tool_name}' exceeded max_calls limit ({policy.max_calls})",
                detail={"max_calls": policy.max_calls, "actual_calls": call_count},
            )

        # Check constraints
        for constraint in policy.constraints:
            violation = self._check_constraint(constraint, tool_name, args)
            if violation is not None:
                return violation

        return None

    def _check_constraint(
        self,
        constraint: CapabilityConstraint,
        tool_name: str,
        args: dict[str, Any],
    ) -> PolicyViolationPayload | None:
        """Check a single constraint against the tool call args."""
        arg_value = args.get(constraint.param)

        if constraint.op == "is_in":
            if arg_value not in constraint.value:
                return PolicyViolationPayload(
                    code=PolicyViolationCode.CONSTRAINT_VIOLATION,
                    tool_name=tool_name,
                    message=(
                        f"Argument '{constraint.param}' value '{arg_value}' "
                        f"not in allowed set {constraint.value}"
                    ),
                    detail={
                        "constraint": "is_in",
                        "param": constraint.param,
                        "actual": arg_value,
                        "allowed": constraint.value,
                    },
                )

        elif constraint.op == "starts_with":
            str_value = str(arg_value) if arg_value is not None else ""
            if not str_value.startswith(str(constraint.value)):
                return PolicyViolationPayload(
                    code=PolicyViolationCode.CONSTRAINT_VIOLATION,
                    tool_name=tool_name,
                    message=(
                        f"Argument '{constraint.param}' value '{str_value}' "
                        f"does not start with '{constraint.value}'"
                    ),
                    detail={
                        "constraint": "starts_with",
                        "param": constraint.param,
                        "actual": str_value,
                        "required_prefix": constraint.value,
                    },
                )

        elif constraint.op == "<=":
            num_value = _to_number(arg_value)
            threshold = _to_number(constraint.value)
            if num_value is not None and threshold is not None and num_value > threshold:
                return PolicyViolationPayload(
                    code=PolicyViolationCode.CONSTRAINT_VIOLATION,
                    tool_name=tool_name,
                    message=(
                        f"Argument '{constraint.param}' value {num_value} "
                        f"exceeds maximum {threshold}"
                    ),
                    detail={
                        "constraint": "<=",
                        "param": constraint.param,
                        "actual": num_value,
                        "maximum": threshold,
                    },
                )

        elif constraint.op == ">=":
            num_value = _to_number(arg_value)
            threshold = _to_number(constraint.value)
            if num_value is not None and threshold is not None and num_value < threshold:
                return PolicyViolationPayload(
                    code=PolicyViolationCode.CONSTRAINT_VIOLATION,
                    tool_name=tool_name,
                    message=(
                        f"Argument '{constraint.param}' value {num_value} below minimum {threshold}"
                    ),
                    detail={
                        "constraint": ">=",
                        "param": constraint.param,
                        "actual": num_value,
                        "minimum": threshold,
                    },
                )

        return None

    def reset_counts(self) -> None:
        """Reset all call counts."""
        self._call_counts.clear()


def _to_number(value: Any) -> float | None:
    """Attempt to convert a value to a number."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
