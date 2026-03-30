"""Text utility functions shared across the clambot codebase."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, urlunparse


def strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences (```...```) wrapping text.

    Handles opening fences with optional language tags (```json, ```javascript, etc.)
    and closing fences. Returns the inner content stripped of leading/trailing whitespace.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]  # Remove opening fence line
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def get_field(obj: Any, field_name: str, default: Any = None) -> Any:
    """Extract a field from an object or dict, supporting both attribute and key access."""
    if isinstance(obj, dict):
        return obj.get(field_name, default)
    return getattr(obj, field_name, default)


def sanitize_args_for_display(args: dict[str, Any]) -> dict[str, Any]:
    """Sanitize tool arguments for human-readable display.

    Strips query strings from URLs so approval/display messages show only
    scheme + host + path.
    """
    sanitized = dict(args)
    if "url" in sanitized and isinstance(sanitized["url"], str):
        parsed = urlparse(sanitized["url"])
        sanitized["url"] = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    return sanitized
