"""HttpRequestTool — built-in tool for making HTTP requests.

Wraps :func:`~clambot.tools.http.operations.http_request` in the
:class:`~clambot.tools.base.BuiltinTool` interface.  Bearer-secret
authentication is supported via an optional *secret_store* dependency
injected at construction time.

The ``execute`` method is intentionally synchronous (the ClamBot runtime
calls tools from a regular thread).  It drives the async
:func:`~clambot.tools.http.operations.http_request` helper via
:func:`asyncio.get_event_loop().run_until_complete` so the underlying
``httpx.AsyncClient`` is used consistently.  In practice the synchronous
``httpx.Client`` path is used directly here for simplicity and to avoid
event-loop nesting issues in the sandbox thread.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from clambot.tools._network import SSRFError, validate_url_not_private
from clambot.tools.base import BuiltinTool, ToolApprovalOption
from clambot.tools.http.approval import get_http_approval_options
from clambot.tools.http.contract import HttpRequestToolContract

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "HttpRequestTool",
]

# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

_CONTRACT = HttpRequestToolContract()


class HttpRequestTool(BuiltinTool):
    """Built-in tool that executes HTTP requests on behalf of the agent.

    Supports optional bearer-secret authentication: when the ``auth`` argument
    has ``type == "bearer_secret"`` the named secret is resolved from
    *secret_store* and injected as an ``Authorization: Bearer`` header.  The
    raw secret value is **never** returned to the LLM or written to logs.

    Args:
        secret_store: Optional object with a ``get(name: str) -> str | None``
                      method.  Pass ``None`` (the default) to disable secret
                      injection — calls that require a secret will return an
                      error result instead of raising.
        ssl_fallback_insecure: When ``True``, retry with ``verify=False``
            on SSL certificate errors.  Defaults to ``False`` (strict).
    """

    def __init__(
        self,
        secret_store: Any | None = None,
        *,
        ssl_fallback_insecure: bool = False,
    ) -> None:
        self._secret_store = secret_store
        self._ssl_fallback_insecure = ssl_fallback_insecure

    # ------------------------------------------------------------------
    # BuiltinTool interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Registered tool name: ``"http_request"``."""
        return _CONTRACT.TOOL_NAME

    @property
    def description(self) -> str:
        """Human-readable description surfaced to the LLM."""
        return "Make HTTP requests with optional bearer_secret authentication."

    @property
    def returns(self) -> dict[str, Any]:
        """Return value schema for ``http_request``."""
        return {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "description": "True when 200 <= status_code < 400."},
                "status_code": {
                    "type": "integer",
                    "description": "HTTP status code, or 0 on error.",
                },
                "content": {"type": "string", "description": "Response body text."},
                "error": {"type": "string", "description": "Present only on failure."},
            },
        }

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema for the ``http_request`` tool parameters."""
        return {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                    "description": "HTTP method to use.",
                },
                "url": {
                    "type": "string",
                    "description": "Target URL for the request.",
                },
                "headers": {
                    "type": "object",
                    "description": "Optional dict of HTTP request headers.",
                    "additionalProperties": {"type": "string"},
                },
                "body": {
                    "type": "string",
                    "description": "Optional request body string.",
                },
                "auth": {
                    "type": "object",
                    "description": (
                        "Optional authentication descriptor. "
                        "Set type to 'bearer_secret' and name to the secret name "
                        "to inject a Bearer token from the secret store."
                    ),
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Auth type, e.g. 'bearer_secret'.",
                        },
                        "name": {
                            "type": "string",
                            "description": "Name of the secret to look up.",
                        },
                    },
                },
            },
            "required": ["method", "url"],
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        """Execute the HTTP request described by *args*.

        Args:
            args: Parameter dict matching :attr:`schema`.

        Returns:
            A dict with keys:

            * ``ok`` (bool) — ``True`` when ``200 <= status_code < 400``.
            * ``status_code`` (int) — HTTP status code, or ``0`` on error.
            * ``content`` (str) — Response body text.
            * ``error`` (str) — Present only on failure; describes the error.
        """
        method = args.get("method", "GET").upper()
        url = args.get("url", "")
        headers: dict[str, str] = dict(args.get("headers") or {})
        body: str | None = args.get("body")
        auth: dict[str, Any] | None = args.get("auth")

        # ------------------------------------------------------------------
        # Guard: conflicting Authorization header + auth field
        # ------------------------------------------------------------------
        if auth and any(k.lower() == "authorization" for k in headers):
            return {
                "ok": False,
                "status_code": 0,
                "content": "",
                "error": "authorization_header_conflicts_with_auth",
            }

        # ------------------------------------------------------------------
        # Bearer secret injection
        # ------------------------------------------------------------------
        if auth:
            auth_type = auth.get("type", "")
            if auth_type == "bearer_secret":
                secret_name = auth.get("name", "")
                if not self._secret_store:
                    return {
                        "ok": False,
                        "status_code": 0,
                        "content": "",
                        "error": "No secret store configured",
                    }
                value = self._secret_store.get(secret_name)
                if not value:
                    return {
                        "ok": False,
                        "status_code": 0,
                        "content": "",
                        "error": f"Secret '{secret_name}' not found",
                    }
                # Inject — raw value stays in this scope only
                headers["Authorization"] = f"Bearer {value}"

        # ------------------------------------------------------------------
        # SSRF guard — block requests to private/internal addresses.
        # ------------------------------------------------------------------
        try:
            validate_url_not_private(url)
        except SSRFError as exc:
            return {
                "ok": False,
                "status_code": 0,
                "content": "",
                "error": f"SSRF blocked: {exc}",
            }

        # ------------------------------------------------------------------
        # Execute request (synchronous path for sandbox-thread compatibility)
        # SSL fallback only when ssl_fallback_insecure is enabled.
        # ------------------------------------------------------------------
        verify_options = [True]
        if self._ssl_fallback_insecure:
            verify_options.append(False)

        for verify in verify_options:
            try:
                with httpx.Client(
                    timeout=30,
                    follow_redirects=True,
                    verify=verify,
                ) as client:
                    response = client.request(
                        method,
                        url,
                        headers=headers,
                        content=body,
                    )
                    return {
                        "ok": 200 <= response.status_code < 400,
                        "status_code": response.status_code,
                        "content": response.text,
                    }
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
                return {
                    "ok": False,
                    "status_code": 0,
                    "content": "",
                    "error": str(exc),
                }

        return {
            "ok": False,
            "status_code": 0,
            "content": "",
            "error": "Failed to execute request",
        }

    # ------------------------------------------------------------------
    # Approval options
    # ------------------------------------------------------------------

    def get_approval_options(self, args: dict[str, Any]) -> list[ToolApprovalOption]:
        """Delegate to :func:`~clambot.tools.http.approval.get_http_approval_options`.

        Args:
            args: The tool argument dict.

        Returns:
            Ordered list of approval scope options.
        """
        return get_http_approval_options(args)
