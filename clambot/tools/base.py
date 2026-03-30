"""Base tool abstractions for ClamBot's built-in tool system.

Defines the :class:`ToolApprovalOption` value object and the
:class:`BuiltinTool` abstract base class that all built-in tool
implementations must subclass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ToolApprovalOption",
    "BuiltinTool",
]

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolApprovalOption:
    """An approval scope option presented to the user before a tool executes.

    Attributes:
        id: Unique identifier for this option (e.g. ``"allow_host"``).
        label: Human-readable display label shown in the approval prompt
               (e.g. ``"Allow Always: host api.coinbase.com"``).
        scope: Machine-readable scope descriptor used to persist the grant
               (e.g. ``"host:api.coinbase.com"``).
    """

    id: str  # Unique identifier for this option
    label: str  # Display label (e.g., "Allow Always: host api.coinbase.com")
    scope: str  # Scope descriptor (e.g., "host:api.coinbase.com")


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class BuiltinTool(ABC):
    """Abstract base class for all ClamBot built-in tools.

    Subclasses must implement :attr:`name`, :attr:`description`, and
    :attr:`schema`.  Optionally override :meth:`execute` and
    :meth:`get_approval_options` to provide tool behaviour and fine-grained
    approval scopes.
    """

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name used as the function name in LLM calls."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what the tool does."""
        ...

    @property
    @abstractmethod
    def schema(self) -> dict[str, Any]:
        """JSON Schema for the tool's parameters (``type`` must be ``"object"``)."""
        ...

    # ------------------------------------------------------------------
    # Optional interface — override for richer metadata
    # ------------------------------------------------------------------

    @property
    def returns(self) -> dict[str, Any]:
        """JSON Schema describing the tool's return value.

        Override in subclasses to make the return shape visible to the
        LLM during clam generation.  The default is an empty dict
        (unspecified).
        """
        return {}

    # ------------------------------------------------------------------
    # Concrete helpers
    # ------------------------------------------------------------------

    def execute(self, args: dict[str, Any]) -> Any:
        """Execute the tool with the given arguments. Override in subclass.

        Args:
            args: Validated parameter dict matching :attr:`schema`.

        Raises:
            NotImplementedError: Always — subclasses must override this method.
        """
        raise NotImplementedError

    def normalize_args_for_approval(self, args: dict[str, Any]) -> dict[str, Any]:
        """Normalize raw tool args to canonical form for approval fingerprinting.

        Override in subclasses to resolve raw args (e.g. relative file paths)
        to their canonical absolute form before fingerprinting and scope
        matching.  The default implementation returns *args* unchanged.

        Args:
            args: Raw parameter dict as received from the clam script.

        Returns:
            A new dict with canonicalized values, or the original dict if
            no normalization is needed.
        """
        return args

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Return available approval scope options for this tool call.

        Override in subclasses to surface fine-grained permission scopes
        (e.g. per-host, per-directory) that the user can choose to grant
        permanently.

        Args:
            args: The arguments that will be passed to :meth:`execute`.

        Returns:
            Ordered list of :class:`ToolApprovalOption` objects, or an empty
            list if no custom scopes are applicable.
        """
        return []

    def to_schema(self) -> dict[str, Any]:
        """Return the tool definition in OpenAI function-call format.

        Returns:
            A dict with ``type``, ``function.name``, ``function.description``,
            ``function.parameters``, and optionally ``function.returns`` keys
            suitable for passing directly to an LLM ``tools`` list.
        """
        func: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.schema,
        }
        if self.returns:
            func["returns"] = self.returns
        return {"type": "function", "function": func}
