"""FilesystemTool — ClamBot built-in tool for filesystem operations.

Provides ``list``, ``read``, ``write``, and ``edit`` operations over a
configurable workspace directory.  Path resolution enforces sandbox safety
rules (see :meth:`FilesystemTool._resolve_path`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clambot.config.schema import FilesystemToolConfig
from clambot.tools.base import BuiltinTool, ToolApprovalOption
from clambot.tools.filesystem.approval import get_filesystem_approval_options
from clambot.tools.filesystem.operations import fs_disk_usage, fs_edit, fs_list, fs_read, fs_write

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "FilesystemTool",
]

# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class FilesystemTool(BuiltinTool):
    """Filesystem operations: list, read, write, edit.

    Args:
        workspace: Root directory for relative path resolution.  Resolved to
            an absolute path on construction.
        config: Optional :class:`~clambot.config.schema.FilesystemToolConfig`
            instance.  Defaults to the schema defaults when omitted.
    """

    def __init__(
        self,
        workspace: Path,
        config: FilesystemToolConfig | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._config = config or FilesystemToolConfig()

    # ------------------------------------------------------------------
    # BuiltinTool interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Canonical tool name used in LLM function calls."""
        return "fs"

    @property
    def description(self) -> str:
        """Human-readable description of the tool's capabilities."""
        return (
            "File system operations: list directories, read files, "
            "write files, edit files, and check disk usage of directories."
        )

    @property
    def returns(self) -> dict[str, Any]:
        """Return value schema for ``fs``."""
        return {
            "type": "string",
            "description": (
                "A plain text result. For 'list': directory entries with metadata "
                "(type, name, human-readable size, last-modified timestamp); "
                "for 'disk_usage': subdirectories sorted by total recursive size; "
                "for 'read': file content; for 'write'/'edit': confirmation message. "
                "Errors are returned as strings starting with 'Error:' or 'Permission denied:'."
            ),
        }

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema (``type: object``) describing the tool's parameters."""
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list", "read", "write", "edit", "disk_usage"],
                    "description": "The filesystem operation to perform.",
                },
                "path": {
                    "type": "string",
                    "description": ("File or directory path (relative to workspace or absolute)."),
                },
                "content": {
                    "type": "string",
                    "description": "Content to write (for write operation).",
                },
                "old_text": {
                    "type": "string",
                    "description": "Text to find (for edit operation).",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text (for edit operation).",
                },
            },
            "required": ["operation", "path"],
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, args: dict[str, Any]) -> Any:
        """Dispatch to the appropriate filesystem operation.

        Args:
            args: Parameter dict validated against :attr:`schema`.

        Returns:
            A human-readable result string.  Errors are returned as strings
            rather than raised so the LLM can read and react to them.
        """
        operation: str = args.get("operation", "")
        path_str: str = args.get("path", "")

        try:
            resolved = self._resolve_path(path_str)
        except PermissionError as exc:
            return f"Permission denied: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Path resolution error: {exc}"

        try:
            if operation == "list":
                return fs_list(resolved)

            if operation == "disk_usage":
                return fs_disk_usage(resolved)

            if operation == "read":
                return fs_read(resolved, max_bytes=self._config.max_read_bytes)

            if operation == "write":
                content: str = args.get("content", "")
                if len(content.encode("utf-8")) > self._config.max_write_bytes:
                    return (
                        f"Error: content exceeds the maximum write size of "
                        f"{self._config.max_write_bytes:,} bytes."
                    )
                return fs_write(resolved, content)

            if operation == "edit":
                old_text: str = args.get("old_text", "")
                new_text: str = args.get("new_text", "")
                return fs_edit(resolved, old_text, new_text)

            ops = "list, read, write, edit, disk_usage"
            return f"Error: unknown operation '{operation}'. Must be one of: {ops}."

        except FileNotFoundError as exc:
            return f"File not found: {exc}"
        except (IsADirectoryError, NotADirectoryError) as exc:
            return f"Path type error: {exc}"
        except PermissionError as exc:
            return f"Permission denied: {exc}"
        except OSError as exc:
            return f"OS error during '{operation}': {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"Unexpected error during '{operation}': {exc}"

    # ------------------------------------------------------------------
    # Args normalization for approval
    # ------------------------------------------------------------------

    def normalize_args_for_approval(self, args: dict[str, Any]) -> dict[str, Any]:
        """Resolve ``args["path"]`` to an absolute host path for approval.

        This ensures fingerprinting and scope matching use canonical absolute
        paths regardless of whether the clam passed a relative, tilde, or
        absolute path.  On any resolution error (e.g. ``/workspace/`` prefix),
        returns *args* unchanged — the actual error will surface at
        :meth:`execute` time.
        """
        path_str = args.get("path", "")
        if not path_str:
            return args
        try:
            resolved = self._resolve_path(path_str)
            return {**args, "path": str(resolved)}
        except Exception:  # noqa: BLE001
            return args

    # ------------------------------------------------------------------
    # Approval options
    # ------------------------------------------------------------------

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Return approval scope options for this filesystem call.

        Delegates to :func:`~clambot.tools.filesystem.approval.get_filesystem_approval_options`.

        Args:
            args: The arguments that will be passed to :meth:`execute`.

        Returns:
            Ordered list of :class:`~clambot.tools.base.ToolApprovalOption`
            objects from narrowest to broadest scope.
        """
        return get_filesystem_approval_options(args, self._workspace)

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _resolve_path(self, path_str: str) -> Path:
        """Resolve *path_str* to an absolute host path.

        Resolution rules (applied in order):

        1. Paths starting with ``/workspace/`` (or equal to ``/workspace``)
           are **rejected** — they collide with the sandbox virtual filesystem
           mount point.
        2. Relative paths are resolved against :attr:`_workspace`.
        3. When :attr:`~clambot.config.schema.FilesystemToolConfig.restrict_to_workspace`
           is ``True``, the resolved path must remain inside
           :attr:`_workspace`; otherwise a :exc:`PermissionError` is raised.

        Args:
            path_str: Raw path string from the tool arguments.

        Returns:
            Resolved absolute :class:`~pathlib.Path`.

        Raises:
            PermissionError: If the path uses the ``/workspace/`` VFS prefix
                or escapes the workspace when restriction is enabled.
        """
        if path_str.startswith("/workspace/") or path_str == "/workspace":
            raise PermissionError(
                f"Path '{path_str}' uses the /workspace/ prefix which conflicts with "
                f"the sandbox virtual filesystem. Use relative paths or absolute host "
                f"paths instead."
            )

        p = Path(path_str).expanduser()
        if not p.is_absolute():
            p = self._workspace / p
        resolved = p.resolve()

        if self._config.restrict_to_workspace:
            workspace_str = str(self._workspace)
            # Ensure the resolved path is inside the workspace.
            # We append os.sep to avoid false positives like
            # /workspace-other matching /workspace.
            if not (
                str(resolved) == workspace_str or str(resolved).startswith(workspace_str + "/")
            ):
                raise PermissionError(
                    f"Path '{path_str}' resolves to '{resolved}' which is outside "
                    f"the workspace '{self._workspace}'."
                )

        return resolved
