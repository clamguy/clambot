"""PDF reader tool — extract text from PDF files in the workspace.

Clams call this tool to read PDF content that the ``fs`` tool cannot
handle (binary files).  Text is extracted page-by-page using *pypdf*,
which is dynamically installed on first use.
"""

from __future__ import annotations

import importlib
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from clambot.tools.base import BuiltinTool

logger = logging.getLogger(__name__)

__all__ = ["PdfReaderTool"]

# Hard ceiling — keeps output within LLM context window limits
# 100K chars ≈ 25K tokens, fits most models with room for prompt overhead
_MAX_TEXT_CHARS = 100_000


# ---------------------------------------------------------------------------
# Dynamic dependency management
# ---------------------------------------------------------------------------


def _ensure_pypdf() -> Any:
    """Import ``pypdf``, installing it dynamically on first ``ImportError``.

    Returns:
        The ``pypdf`` module.

    Raises:
        RuntimeError: If installation fails or the module still cannot
            be imported after installation.
    """
    try:
        import pypdf  # type: ignore[import-untyped]

        return pypdf
    except ImportError:
        pass

    # Attempt dynamic install — try uv first (project standard), fall back to pip
    installed = False
    for cmd in (
        ["uv", "pip", "install", "pypdf"],
        [sys.executable, "-m", "pip", "install", "pypdf"],
    ):
        try:
            subprocess.check_call(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            installed = True
            break
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    if not installed:
        raise RuntimeError(
            "Failed to install pypdf. Please install it manually: uv pip install pypdf"
        )

    # Retry import — invalidate finder caches so Python sees the
    # newly-installed package without a process restart.
    importlib.invalidate_caches()

    try:
        import pypdf  # type: ignore[import-untyped]

        return pypdf
    except ImportError as exc:
        raise RuntimeError("pypdf was installed but could not be imported.") from exc


class PdfReaderTool(BuiltinTool):
    """Read and extract text from PDF files."""

    def __init__(self, *, workspace: Path) -> None:
        self._workspace = Path(workspace)

    # ── BuiltinTool interface ─────────────────────────────────

    @property
    def name(self) -> str:
        return "pdf_reader"

    @property
    def description(self) -> str:
        return (
            "Extract text content from a PDF file. "
            "Accepts a file path (relative to workspace or absolute) "
            "and returns the extracted text with page numbers."
        )

    @property
    def usage_instructions(self) -> list[str]:
        """Prompt guidance for generation-time pdf_reader usage."""
        return [
            "Use for PDF files (for uploaded files typically under upload/<name>.pdf).",
            "Pass path and optional pages (for example: '1-3' or '1,3,5').",
            "Check result.error before reading result.text.",
            "Return result.text for downstream summarize/translate steps.",
        ]

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path to the PDF file. Relative paths are resolved "
                        "against the workspace directory."
                    ),
                },
                "pages": {
                    "type": "string",
                    "description": (
                        "Optional page selection. Examples: '1' (first page), "
                        "'1-5' (pages 1 through 5), '1,3,5' (specific pages). "
                        "Omit to read all pages. Pages are 1-indexed."
                    ),
                },
            },
            "required": ["path"],
        }

    @property
    def returns(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The resolved file path."},
                "total_pages": {"type": "integer", "description": "Total pages in the PDF."},
                "pages_read": {"type": "integer", "description": "Number of pages extracted."},
                "text": {"type": "string", "description": "Extracted text content."},
                "truncated": {"type": "boolean", "description": "Whether text was truncated."},
                "error": {"type": "string", "description": "Error message on failure."},
            },
        }

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        path_str = args.get("path", "")
        if not path_str:
            return {"error": "Missing required parameter: path"}

        try:
            resolved = self._resolve_path(path_str)
        except PermissionError as exc:
            return {"error": str(exc)}

        if not resolved.exists():
            return {"error": f"File not found: {path_str}"}

        if resolved.suffix.lower() != ".pdf":
            return {"error": f"Not a PDF file: {path_str}"}

        # Dynamic install of pypdf on first use
        try:
            pypdf = _ensure_pypdf()
        except RuntimeError as exc:
            return {"error": str(exc)}

        try:
            reader = pypdf.PdfReader(str(resolved))
            total_pages = len(reader.pages)

            # Determine which pages to read
            page_indices = self._parse_pages(args.get("pages"), total_pages)

            # Extract text
            parts: list[str] = []
            for idx in page_indices:
                page = reader.pages[idx]
                text = page.extract_text() or ""
                if text.strip():
                    parts.append(f"--- Page {idx + 1} ---\n{text}")

            full_text = "\n\n".join(parts)
            truncated = False
            if len(full_text) > _MAX_TEXT_CHARS:
                full_text = full_text[:_MAX_TEXT_CHARS]
                truncated = True

            return {
                "path": str(resolved),
                "total_pages": total_pages,
                "pages_read": len(page_indices),
                "text": full_text,
                "truncated": truncated,
            }

        except Exception as exc:
            logger.error("Failed to read PDF %s: %s", resolved, exc)
            return {"error": f"Failed to read PDF: {exc}"}

    # ── Helpers ───────────────────────────────────────────────

    def _resolve_path(self, path_str: str) -> Path:
        """Resolve a path relative to the workspace, same rules as fs tool."""
        if path_str.startswith("/workspace/") or path_str == "/workspace":
            raise PermissionError(
                f"Path '{path_str}' uses the /workspace/ prefix which conflicts "
                f"with the sandbox virtual filesystem. Use relative paths instead."
            )

        p = Path(path_str).expanduser()
        if not p.is_absolute():
            p = self._workspace / p
        return p.resolve()

    @staticmethod
    def _parse_pages(pages_spec: str | None, total: int) -> list[int]:
        """Parse a page specification into 0-based page indices.

        Supports: ``None`` (all), ``"3"`` (single), ``"1-5"`` (range),
        ``"1,3,5"`` (list), or combinations like ``"1-3,7,10-12"``.
        """
        if not pages_spec:
            return list(range(total))

        indices: list[int] = []
        for part in pages_spec.split(","):
            part = part.strip()
            if "-" in part:
                start_s, end_s = part.split("-", 1)
                start = max(int(start_s) - 1, 0)
                end = min(int(end_s), total)
                indices.extend(range(start, end))
            else:
                idx = int(part) - 1
                if 0 <= idx < total:
                    indices.append(idx)

        # Deduplicate while preserving order
        seen: set[int] = set()
        result: list[int] = []
        for i in indices:
            if i not in seen:
                seen.add(i)
                result.append(i)
        return result
