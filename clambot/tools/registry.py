"""Built-in tool registry — lookup, dispatch, and LLM schema rendering.

The :class:`BuiltinToolRegistry` is the single point of truth for which tools
are available at runtime.  Tools are registered by name and can be dispatched
by name, looked up individually, or serialised to the OpenAI function-call
schema format for inclusion in LLM requests.
"""

from __future__ import annotations

from typing import Any

from clambot.tools.base import BuiltinTool

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "BuiltinToolRegistry",
]

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class BuiltinToolRegistry:
    """Registry for built-in tools. Supports lookup, dispatch, and LLM schema rendering.

    Tools are keyed by their :attr:`~clambot.tools.base.BuiltinTool.name`.
    Registering a tool with a name that already exists silently replaces the
    previous entry, which allows tests and plugins to override defaults.

    Example::

        registry = BuiltinToolRegistry()
        registry.register(MyTool())
        result = registry.dispatch("my_tool", {"arg": "value"})
    """

    def __init__(self) -> None:
        self._tools: dict[str, BuiltinTool] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(self, tool: BuiltinTool) -> None:
        """Register a tool by its name.

        If a tool with the same name is already registered it is replaced.

        Args:
            tool: A :class:`~clambot.tools.base.BuiltinTool` instance to add.
        """
        self._tools[tool.name] = tool

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Dispatch a tool call by name.

        Args:
            tool_name: The registered name of the tool to invoke.
            args: Argument dict forwarded verbatim to
                  :meth:`~clambot.tools.base.BuiltinTool.execute`.

        Returns:
            Whatever the tool's ``execute`` method returns.

        Raises:
            ValueError: If *tool_name* is not registered.
        """
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        return tool.execute(args)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_schemas(self) -> list[dict[str, Any]]:
        """Return all tool schemas in OpenAI function-call format.

        Returns:
            Ordered list of dicts produced by each tool's
            :meth:`~clambot.tools.base.BuiltinTool.to_schema` method,
            suitable for passing directly to an LLM ``tools`` parameter.
        """
        return [tool.to_schema() for tool in self._tools.values()]

    def get_tool(self, name: str) -> BuiltinTool | None:
        """Look up a tool by name.

        Args:
            name: Registered tool name.

        Returns:
            The :class:`~clambot.tools.base.BuiltinTool` instance, or
            ``None`` if no tool with that name is registered.
        """
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        """Return the names of all registered tools in insertion order."""
        return list(self._tools.keys())

    # ------------------------------------------------------------------
    # Dunder helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Return the number of registered tools."""
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """Return ``True`` if a tool with *name* is registered."""
        return name in self._tools
