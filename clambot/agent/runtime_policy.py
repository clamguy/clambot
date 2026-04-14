"""Runtime policy resolution from clam metadata.

Determines execution parameters (timeout, max tool calls, etc.)
from clam metadata and config defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimePolicy:
    """Execution policy for a clam runtime invocation.

    Attributes:
        timeout_seconds: Maximum execution time before timeout.
        max_tool_iterations: Maximum number of tool call rounds.
        max_stdin_bytes: Maximum bytes for stdin piping.
        stdin_threshold_bytes: Script size above which stdin piping is used.
    """

    timeout_seconds: int = 60
    max_tool_iterations: int = 50
    max_stdin_bytes: int = 1_048_576  # 1 MB
    stdin_threshold_bytes: int = 7168  # 7 KB


def resolve_runtime_policy(
    clam_metadata: dict[str, Any] | None = None,
    config_defaults: Any | None = None,
) -> RuntimePolicy:
    """Resolve runtime policy from clam metadata and config.

    Priority: clam metadata overrides > config defaults > built-in defaults.

    Args:
        clam_metadata: Optional dict from clam's CLAM.md metadata section.
        config_defaults: Optional agent defaults config object.

    Returns:
        A ``RuntimePolicy`` with resolved execution parameters.
    """
    policy = RuntimePolicy()

    # Apply config defaults first
    if config_defaults is not None:
        timeout_seconds = getattr(config_defaults, "runtime_timeout_seconds", None)
        if timeout_seconds is not None:
            policy.timeout_seconds = int(timeout_seconds)

        max_iters = getattr(config_defaults, "max_tool_iterations", None)
        if max_iters is not None:
            policy.max_tool_iterations = max_iters

    # Apply clam metadata overrides
    if clam_metadata:
        runtime_cfg = clam_metadata.get("runtime", {})
        if isinstance(runtime_cfg, dict):
            if "timeout_seconds" in runtime_cfg:
                policy.timeout_seconds = int(runtime_cfg["timeout_seconds"])
            if "max_tool_iterations" in runtime_cfg:
                policy.max_tool_iterations = int(runtime_cfg["max_tool_iterations"])

    return policy
