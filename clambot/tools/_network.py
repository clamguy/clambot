"""Network security helpers for built-in tools.

Provides :func:`validate_url_not_private` which blocks HTTP requests to
private / internal IP addresses (SSRF protection).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = ["SSRFError", "validate_url_not_private"]


class SSRFError(ValueError):
    """Raised when a URL resolves to a private or internal IP address."""


# RFC-defined private / internal networks that must be blocked.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),  # IPv4 loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
]


def validate_url_not_private(url: str) -> None:
    """Validate that *url* does not resolve to a private/internal address.

    Resolves the hostname via :func:`socket.getaddrinfo` and checks every
    returned address against :data:`_BLOCKED_NETWORKS`.  If **any** address
    falls within a blocked range the call raises :class:`SSRFError`.

    Args:
        url: The URL to validate.

    Raises:
        SSRFError: When the URL hostname resolves to a blocked address.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise SSRFError(f"No hostname in URL: {url}")

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # DNS resolution failed — let the HTTP client surface its own error.
        return

    for addr_info in addr_infos:
        raw_addr = addr_info[4][0]
        try:
            addr = ipaddress.ip_address(raw_addr)
        except ValueError:
            continue

        for network in _BLOCKED_NETWORKS:
            if addr in network:
                raise SSRFError(
                    f"URL '{url}' resolves to private/internal address "
                    f"{raw_addr} (blocked network {network})"
                )
