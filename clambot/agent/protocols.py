"""Protocol types for the agent subsystem.

Defines structural typing contracts for the major components passed
into ``AgentLoop`` and ``GatewayOrchestrator`` so that concrete
implementations can be swapped without import-time coupling.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


@runtime_checkable
class GeneratorProtocol(Protocol):
    """Generates clam code (script + metadata) from a user request."""

    async def generate(
        self,
        message: str,
        history: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        link_context: str = "",
        self_fix_context: str = "",
    ) -> Any:  # GenerationResult
        """Return a GenerationResult for the given message."""
        ...


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


@runtime_checkable
class RuntimeProtocol(Protocol):
    """Executes a clam inside the WASM sandbox."""

    def begin_turn(self) -> None:
        """Reset per-turn state (e.g. turn-scoped approval grants)."""
        ...

    async def execute(
        self,
        clam: Any,
        inputs: dict[str, Any] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        one_time_approval_grants: list[dict[str, Any]] | None = None,
        secret_store: Any | None = None,
    ) -> Any:  # RuntimeResult
        """Execute the clam and return a RuntimeResult."""
        ...


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


@runtime_checkable
class AnalyzerProtocol(Protocol):
    """Analyzes clam execution results to decide ACCEPT / SELF_FIX / REJECT."""

    async def analyze(
        self,
        message: str,
        clam: Any,
        runtime_result: Any,
        *,
        full_output: bool = False,
    ) -> Any:  # AnalysisResult
        """Return an AnalysisResult for the given execution."""
        ...


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolRegistryProtocol(Protocol):
    """Registry of built-in tools available to clam scripts."""

    def get_schemas(self) -> list[dict[str, Any]]:
        """Return JSON Schema definitions for all registered tools."""
        ...

    def get_tool(self, name: str) -> Any | None:
        """Look up a tool by name, or return ``None``."""
        ...

    def get_usage_instructions(self) -> dict[str, list[str]]:
        """Return per-tool prompt usage instructions keyed by tool name."""
        ...

    def dispatch(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Execute a tool call and return its result."""
        ...


# ---------------------------------------------------------------------------
# Secret store
# ---------------------------------------------------------------------------


@runtime_checkable
class SecretStoreProtocol(Protocol):
    """Persistent key-value store for user secrets."""

    def get(self, name: str) -> str | None:
        """Retrieve a secret by name, or ``None`` if not found."""
        ...

    def save(self, name: str, value: str, description: str = "") -> None:
        """Persist a secret under *name*."""
        ...
