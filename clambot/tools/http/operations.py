"""Pure HTTP operation functions for the ClamBot http_request tool.

:func:`http_request` is a standalone async helper that performs a single HTTP
call and returns a normalised result dict.  It is intentionally free of
tool-framework concerns so it can be unit-tested in isolation.

Secret injection is handled here: when ``auth.type == "bearer_secret"`` the
secret value is fetched from *secret_store* and injected as an
``Authorization: Bearer`` header.  The raw secret value is **never** included
in the returned dict, logs, or events — only the ``[REDACTED_SECRET]``
sentinel appears in any outward-facing representation.
"""

from __future__ import annotations

from typing import Any

import httpx

from clambot.tools.http.contract import HttpRequestToolContract

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "http_request",
    "REDACTED",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REDACTED: str = HttpRequestToolContract().REDACTED_VALUE

# ---------------------------------------------------------------------------
# Core operation
# ---------------------------------------------------------------------------


async def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    auth: dict[str, str] | None = None,
    secret_store: Any | None = None,
) -> dict[str, Any]:
    """Execute an HTTP request with optional bearer-secret auth injection.

    If ``auth["type"] == "bearer_secret"``:

    * The secret named ``auth["name"]`` is looked up in *secret_store* via
      ``secret_store.get(name)``.
    * The resolved value is injected as ``Authorization: Bearer <token>``.
    * The raw token is **never** returned or logged; only :data:`REDACTED`
      appears in any outward-facing representation.

    Args:
        method: HTTP verb (``GET``, ``POST``, etc.).
        url: Target URL.
        headers: Optional dict of extra request headers.
        body: Optional request body string.
        auth: Optional auth descriptor dict with ``"type"`` and ``"name"``
              keys.  Currently only ``"bearer_secret"`` is supported.
        secret_store: Object with a ``get(name: str) -> str | None`` method
                      used to resolve bearer secrets.

    Returns:
        A dict with the following keys:

        * ``ok`` (bool) — ``True`` when ``200 <= status_code < 400``.
        * ``status_code`` (int) — HTTP response status code, or ``0`` on
          network/timeout error.
        * ``content`` (str) — Response body text.
        * ``error`` (str | None) — Error message, present only on failure.

    Raises:
        ValueError: If both an ``Authorization`` header and an ``auth`` dict
                    are supplied simultaneously.
    """
    effective_headers: dict[str, str] = dict(headers) if headers else {}

    # ------------------------------------------------------------------
    # Guard: conflicting Authorization header + auth field
    # ------------------------------------------------------------------
    if auth and any(k.lower() == "authorization" for k in effective_headers):
        raise ValueError(
            "Conflicting auth: 'Authorization' header and 'auth' field cannot "
            "both be specified at the same time."
        )

    # ------------------------------------------------------------------
    # Bearer secret injection
    # ------------------------------------------------------------------
    if auth:
        auth_type = auth.get("type", "")
        if auth_type == "bearer_secret":
            secret_name = auth.get("name", "")
            if not secret_store:
                return {
                    "ok": False,
                    "status_code": 0,
                    "content": "",
                    "error": "No secret store configured",
                }
            value = secret_store.get(secret_name)
            if not value:
                return {
                    "ok": False,
                    "status_code": 0,
                    "content": "",
                    "error": f"Secret '{secret_name}' not found",
                }
            # Inject — never expose the raw value outside this scope
            effective_headers["Authorization"] = f"Bearer {value}"

    # ------------------------------------------------------------------
    # Execute request — SSL fallback for sandboxed / proxy environments
    # ------------------------------------------------------------------
    for verify in (True, False):
        try:
            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                verify=verify,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers=effective_headers,
                    content=body,
                )
                return {
                    "ok": 200 <= response.status_code < 400,
                    "status_code": response.status_code,
                    "content": response.text,
                }
        except Exception as exc:  # noqa: BLE001
            is_ssl_error = "SSL" in str(exc) or "CERTIFICATE" in str(exc)
            if verify and is_ssl_error:
                continue  # retry without SSL verification
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
