"""WebFetchTool — built-in tool for fetching and extracting web content.

Fetches a URL via ``httpx`` and returns the page content as plain text (with
basic HTML tag stripping) or pretty-printed JSON.  Content is truncated to
*max_chars* to keep LLM context windows manageable.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from clambot.tools._network import SSRFError, validate_url_not_private
from clambot.tools.base import BuiltinTool, ToolApprovalOption
from clambot.utils.constants import USER_AGENT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "WebFetchTool",
]

# ---------------------------------------------------------------------------
# HTML stripping helper
# ---------------------------------------------------------------------------


def _strip_html_tags(html: str) -> str:
    """Remove HTML tags and decode common entities from *html*.

    Processing steps:

    1. Remove ``<script>`` and ``<style>`` blocks (including their content).
    2. Replace all remaining HTML tags with a single space.
    3. Decode the most common HTML entities.
    4. Collapse runs of whitespace into a single space and strip leading /
       trailing whitespace.

    Args:
        html: Raw HTML string.

    Returns:
        Plain-text representation of the HTML content.
    """
    # Remove script and style blocks entirely (content included)
    text = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Replace remaining tags with a space
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    # Normalise whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class WebFetchTool(BuiltinTool):
    """Built-in tool that fetches and extracts content from a URL.

    Supports ``http`` and ``https`` URLs only.  HTML responses are stripped of
    tags; JSON responses are pretty-printed.  All other content types are
    returned as-is.  The result is truncated to *max_chars* characters.

    This tool is read-only and therefore does not surface any approval options.

    Args:
        ssl_fallback_insecure: When ``True``, retry with ``verify=False``
            on SSL certificate errors.  Defaults to ``False`` (strict).
    """

    def __init__(self, *, ssl_fallback_insecure: bool = False) -> None:
        self._ssl_fallback_insecure = ssl_fallback_insecure

    # ------------------------------------------------------------------
    # BuiltinTool interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Registered tool name: ``"web_fetch"``."""
        return "web_fetch"

    @property
    def description(self) -> str:
        """Human-readable description surfaced to the LLM."""
        return "Fetch and extract content from a URL."

    @property
    def usage_instructions(self) -> list[str]:
        """Prompt guidance for generation-time web_fetch usage."""
        return [
            "Use for webpage/article text extraction, not strict JSON APIs.",
            "Pass url (and optionally max_chars for long pages).",
            "Return response.content (useful text), not the full metadata object.",
            "Do not use for video/audio speech transcription; use transcribe instead.",
        ]

    @property
    def returns(self) -> dict[str, Any]:
        """Return value schema for ``web_fetch``."""
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The original requested URL."},
                "final_url": {"type": "string", "description": "URL after redirects."},
                "status": {"type": "integer", "description": "HTTP status code, or 0 on error."},
                "content": {"type": "string", "description": "Extracted text content."},
                "truncated": {"type": "boolean", "description": "True if content was truncated."},
                "length": {"type": "integer", "description": "Length of the returned content."},
                "error": {"type": "string", "description": "Present only on failure."},
            },
        }

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema for the ``web_fetch`` tool parameters."""
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch (http or https only).",
                },
                "extract_mode": {
                    "type": "string",
                    "enum": ["markdown", "text"],
                    "default": "markdown",
                    "description": (
                        "Content extraction mode. "
                        "'markdown' and 'text' both return plain text; "
                        "HTML tags are stripped in either case."
                    ),
                },
                "max_chars": {
                    "type": "integer",
                    "default": 50000,
                    "minimum": 100,
                    "description": "Maximum number of characters to return.",
                },
            },
            "required": ["url"],
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        """Fetch the URL described by *args* and return extracted content.

        Args:
            args: Parameter dict matching :attr:`schema`.

        Returns:
            A dict with keys:

            * ``url`` (str) — The original requested URL.
            * ``final_url`` (str) — The URL after any redirects.
            * ``status`` (int) — HTTP status code, or ``0`` on error.
            * ``content`` (str) — Extracted text content (possibly truncated).
            * ``truncated`` (bool) — ``True`` if content was truncated.
            * ``length`` (int) — Length of the returned content string.
            * ``error`` (str) — Present only on failure; describes the error.
        """
        url: str = args.get("url", "")
        max_chars: int = int(args.get("max_chars", 50000))

        # ------------------------------------------------------------------
        # Validate URL scheme
        # ------------------------------------------------------------------
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {
                "url": url,
                "status": 0,
                "content": "",
                "error": "Only http/https URLs supported",
            }
        if not parsed.hostname:
            return {
                "url": url,
                "status": 0,
                "content": "",
                "error": "Invalid URL: no hostname",
            }

        # ------------------------------------------------------------------
        # SSRF guard — block requests to private/internal addresses.
        # ------------------------------------------------------------------
        try:
            validate_url_not_private(url)
        except SSRFError as exc:
            return {
                "url": url,
                "status": 0,
                "content": "",
                "error": f"SSRF blocked: {exc}",
            }

        # ------------------------------------------------------------------
        # Fetch — try with SSL verification first; fall back to
        # verify=False only when ssl_fallback_insecure is enabled.
        # ------------------------------------------------------------------
        verify_options = [True]
        if self._ssl_fallback_insecure:
            verify_options.append(False)

        resp = None
        for verify in verify_options:
            try:
                with httpx.Client(
                    timeout=30,
                    follow_redirects=True,
                    max_redirects=5,
                    verify=verify,
                ) as client:
                    resp = client.get(url, headers={"User-Agent": USER_AGENT})
                break  # success — stop retrying
            except Exception as exc:  # noqa: BLE001
                is_ssl_error = "SSL" in str(exc) or "CERTIFICATE" in str(exc)
                if verify and is_ssl_error and self._ssl_fallback_insecure:
                    logger.warning(
                        "SSL verification failed for %s; retrying with verify=False",
                        url,
                    )
                    continue
                if verify and is_ssl_error:
                    logger.warning(
                        "SSL verification failed for %s; insecure fallback disabled",
                        url,
                    )
                # Non-SSL error or already tried without verification
                return {
                    "url": url,
                    "status": 0,
                    "content": "",
                    "error": str(exc),
                }

        if resp is None:
            return {
                "url": url,
                "status": 0,
                "content": "",
                "error": "Failed to fetch URL",
            }

        content_type = resp.headers.get("content-type", "")

        # ----------------------------------------------------------
        # Content extraction
        # ----------------------------------------------------------
        if "json" in content_type:
            try:
                text = json.dumps(resp.json(), indent=2)
            except Exception:  # noqa: BLE001
                text = resp.text
        elif "html" in content_type:
            text = _strip_html_tags(resp.text)
        else:
            text = resp.text

        # ----------------------------------------------------------
        # Truncation
        # ----------------------------------------------------------
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        return {
            "url": url,
            "final_url": str(resp.url),
            "status": resp.status_code,
            "content": text,
            "truncated": truncated,
            "length": len(text),
        }

    # ------------------------------------------------------------------
    # Approval options
    # ------------------------------------------------------------------

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Return an empty list — web_fetch is read-only and needs no approval.

        Args:
            args: Unused.

        Returns:
            Empty list.
        """
        return []
