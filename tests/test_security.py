"""Tests for Phase 2 — Security Hardening.

Covers SSRF protection, SSL fallback configuration, and default bind address.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from clambot.config.schema import ClamBotConfig, GatewayConfig, SecurityConfig
from clambot.tools._network import SSRFError, validate_url_not_private
from clambot.tools.http.core import HttpRequestTool
from clambot.tools.web.fetch import WebFetchTool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_httpx_client(status_code: int = 200, text: str = "ok") -> MagicMock:
    """Return a MagicMock that behaves like httpx.Client used as a context manager."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = text
    mock_response.url = "https://example.com"
    mock_response.headers = {"content-type": "text/html"}

    mock_client = MagicMock()
    mock_client.get.return_value = mock_response
    mock_client.request.return_value = mock_response

    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

    return mock_client_cls


# ---------------------------------------------------------------------------
# SSRF protection — validate_url_not_private
# ---------------------------------------------------------------------------


class TestValidateUrlNotPrivate:
    """Tests for SSRF protection in tools/_network.py."""

    def test_blocks_localhost_127(self) -> None:
        """127.0.0.1 is rejected."""
        with pytest.raises(SSRFError, match="private/internal"):
            validate_url_not_private("http://127.0.0.1/api")

    def test_blocks_localhost_name(self) -> None:
        """'localhost' resolves to 127.0.0.1 and is rejected."""
        with pytest.raises(SSRFError, match="private/internal"):
            validate_url_not_private("http://localhost/api")

    def test_blocks_rfc1918_10(self) -> None:
        """10.x.x.x is rejected."""
        with pytest.raises(SSRFError, match="private/internal"):
            validate_url_not_private("http://10.0.0.1/data")

    def test_blocks_rfc1918_172_16(self) -> None:
        """172.16.x.x is rejected."""
        with pytest.raises(SSRFError, match="private/internal"):
            validate_url_not_private("http://172.16.0.1/data")

    def test_blocks_rfc1918_192_168(self) -> None:
        """192.168.x.x is rejected."""
        with pytest.raises(SSRFError, match="private/internal"):
            validate_url_not_private("http://192.168.1.1/data")

    def test_blocks_link_local(self) -> None:
        """169.254.x.x is rejected."""
        with pytest.raises(SSRFError, match="private/internal"):
            validate_url_not_private("http://169.254.169.254/metadata")

    def test_allows_public_ip(self) -> None:
        """Public IPs like 8.8.8.8 are allowed."""
        # Should not raise — mock DNS to return a public IP
        with patch("clambot.tools._network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (2, 1, 6, "", ("8.8.8.8", 0)),
            ]
            validate_url_not_private("http://dns.google/resolve")  # no exception

    def test_allows_unresolvable_hostname(self) -> None:
        """Unresolvable hostnames pass through (HTTP client handles the error)."""
        with patch("clambot.tools._network.socket.getaddrinfo") as mock_dns:
            import socket

            mock_dns.side_effect = socket.gaierror("Name resolution failed")
            validate_url_not_private("http://nonexistent.invalid/test")  # no exception

    def test_no_hostname_raises(self) -> None:
        """URL without hostname raises SSRFError."""
        with pytest.raises(SSRFError, match="No hostname"):
            validate_url_not_private("not-a-url")

    def test_blocks_ipv6_loopback(self) -> None:
        """IPv6 ::1 is rejected."""
        with patch("clambot.tools._network.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (10, 1, 6, "", ("::1", 0, 0, 0)),
            ]
            with pytest.raises(SSRFError, match="private/internal"):
                validate_url_not_private("http://[::1]/api")


# ---------------------------------------------------------------------------
# Web fetch — SSRF integration
# ---------------------------------------------------------------------------


class TestWebFetchSSRF:
    """Tests for SSRF protection in web_fetch tool."""

    def test_web_fetch_rejects_private_ip(self) -> None:
        """web_fetch returns SSRF error for private IP."""
        tool = WebFetchTool()
        result = tool.execute({"url": "http://127.0.0.1/secret"})
        assert result["status"] == 0
        assert "SSRF" in result.get("error", "")

    def test_web_fetch_rejects_rfc1918(self) -> None:
        """web_fetch returns SSRF error for RFC 1918 addresses."""
        tool = WebFetchTool()
        result = tool.execute({"url": "http://10.0.0.1/internal"})
        assert result["status"] == 0
        assert "SSRF" in result.get("error", "")


# ---------------------------------------------------------------------------
# HTTP request — SSRF integration
# ---------------------------------------------------------------------------


class TestHttpRequestSSRF:
    """Tests for SSRF protection in http_request tool."""

    def test_http_request_rejects_private_ip(self) -> None:
        """http_request returns SSRF error for private IP."""
        tool = HttpRequestTool()
        result = tool.execute(
            {
                "method": "GET",
                "url": "http://127.0.0.1/secret",
            }
        )
        assert result["ok"] is False
        assert "SSRF" in result.get("error", "")

    def test_http_request_rejects_rfc1918(self) -> None:
        """http_request returns SSRF error for RFC 1918 addresses."""
        tool = HttpRequestTool()
        result = tool.execute(
            {
                "method": "GET",
                "url": "http://192.168.1.1/admin",
            }
        )
        assert result["ok"] is False
        assert "SSRF" in result.get("error", "")


# ---------------------------------------------------------------------------
# SSL fallback configuration
# ---------------------------------------------------------------------------


class TestSSLFallback:
    """Tests for ssl_fallback_insecure configuration."""

    def test_ssl_fallback_disabled_by_default_config(self) -> None:
        """SecurityConfig defaults ssl_fallback_insecure to False."""
        cfg = SecurityConfig()
        assert cfg.ssl_fallback_insecure is False

    def test_ssl_fallback_disabled_by_default_root(self) -> None:
        """ClamBotConfig defaults ssl_fallback_insecure to False."""
        cfg = ClamBotConfig()
        assert cfg.security.ssl_fallback_insecure is False

    def test_web_fetch_ssl_error_not_bypassed_by_default(self) -> None:
        """SSL error is NOT silently bypassed when ssl_fallback_insecure=False."""
        tool = WebFetchTool(ssl_fallback_insecure=False)

        # Mock httpx.Client to always raise an SSL error
        mock_client_cls = MagicMock()
        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("SSL: CERTIFICATE_VERIFY_FAILED")
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        with patch("clambot.tools.web.fetch.httpx.Client", mock_client_cls):
            # Mock the SSRF check to allow the URL through
            with patch("clambot.tools.web.fetch.validate_url_not_private"):
                result = tool.execute({"url": "https://example.com"})

        assert result["status"] == 0
        assert "SSL" in result.get("error", "") or "CERTIFICATE" in result.get("error", "")
        # Should only have been called ONCE (no retry with verify=False)
        assert mock_client_cls.call_count == 1

    def test_web_fetch_ssl_error_retried_when_insecure_enabled(self) -> None:
        """SSL error triggers retry with verify=False when ssl_fallback_insecure=True."""
        tool = WebFetchTool(ssl_fallback_insecure=True)

        call_count = 0

        def mock_client_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            ctx = MagicMock()
            if kwargs.get("verify", True) is True:
                # First call with verify=True → SSL error
                mock_inner = MagicMock()
                mock_inner.get.side_effect = Exception("SSL: CERTIFICATE_VERIFY_FAILED")
                ctx.__enter__ = MagicMock(return_value=mock_inner)
            else:
                # Second call with verify=False → success
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.url = "https://example.com"
                mock_resp.text = "success"
                mock_resp.headers = {"content-type": "text/plain"}
                mock_inner = MagicMock()
                mock_inner.get.return_value = mock_resp
                ctx.__enter__ = MagicMock(return_value=mock_inner)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("clambot.tools.web.fetch.httpx.Client", side_effect=mock_client_side_effect):
            with patch("clambot.tools.web.fetch.validate_url_not_private"):
                result = tool.execute({"url": "https://example.com"})

        assert result["status"] == 200
        assert call_count == 2  # Called twice: verify=True then verify=False

    def test_http_request_ssl_error_not_bypassed_by_default(self) -> None:
        """http_request SSL error is NOT bypassed when ssl_fallback_insecure=False."""
        tool = HttpRequestTool(ssl_fallback_insecure=False)

        mock_client_cls = MagicMock()
        mock_client = MagicMock()
        mock_client.request.side_effect = Exception("SSL: CERTIFICATE_VERIFY_FAILED")
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        with patch("clambot.tools.http.core.httpx.Client", mock_client_cls):
            with patch("clambot.tools.http.core.validate_url_not_private"):
                result = tool.execute(
                    {
                        "method": "GET",
                        "url": "https://example.com",
                    }
                )

        assert result["ok"] is False
        assert "SSL" in result.get("error", "") or "CERTIFICATE" in result.get("error", "")
        assert mock_client_cls.call_count == 1  # No retry


# ---------------------------------------------------------------------------
# Default bind address
# ---------------------------------------------------------------------------


class TestDefaultBindAddress:
    """Tests for GatewayConfig default bind address."""

    def test_gateway_default_host_is_localhost(self) -> None:
        """GatewayConfig defaults to 127.0.0.1 (not 0.0.0.0)."""
        cfg = GatewayConfig()
        assert cfg.host == "127.0.0.1"

    def test_gateway_host_overridable(self) -> None:
        """GatewayConfig host can be overridden to 0.0.0.0."""
        cfg = GatewayConfig(host="0.0.0.0")
        assert cfg.host == "0.0.0.0"

    def test_root_config_gateway_default(self) -> None:
        """ClamBotConfig.gateway.host defaults to 127.0.0.1."""
        cfg = ClamBotConfig()
        assert cfg.gateway.host == "127.0.0.1"
