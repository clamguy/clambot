"""Provider link context builder — URL prefetch for generation context.

Extracts URLs from the user's message, fetches their content, and
provides it as context for clam generation.
"""

from __future__ import annotations

import logging
import re

from clambot.utils.constants import USER_AGENT

logger = logging.getLogger(__name__)


# URL extraction pattern
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


class ProviderLinkContextBuilder:
    """Extracts and pre-fetches URLs from messages for generation context.

    Respects budget limits on number of links and content size.
    """

    def __init__(
        self,
        max_links: int = 3,
        max_chars_per_link: int = 8000,
        enabled: bool = True,
        explicit_links_only: bool = False,
    ) -> None:
        self._max_links = max_links
        self._max_chars_per_link = max_chars_per_link
        self._enabled = enabled
        self._explicit_links_only = explicit_links_only

    async def fetch(self, message: str) -> str:
        """Extract URLs from the message and fetch their content.

        Args:
            message: The user's message text.

        Returns:
            Formatted link context string, or empty string if no links.
        """
        if not self._enabled:
            return ""

        urls = self._extract_urls(message)
        if not urls:
            return ""

        # Limit number of URLs
        urls = urls[: self._max_links]

        results: list[str] = []
        for url in urls:
            try:
                content = await self._fetch_url(url)
                if content:
                    # Truncate to budget
                    if len(content) > self._max_chars_per_link:
                        content = content[: self._max_chars_per_link] + "\n[truncated]"
                    results.append(f"URL: {url}\nContent:\n{content}")
            except Exception as exc:
                logger.debug("Failed to fetch %s: %s", url, exc)
                results.append(f"URL: {url}\n[Failed to fetch: {exc}]")

        if not results:
            return ""

        return "\n\n---\n\n".join(results)

    def _extract_urls(self, text: str) -> list[str]:
        """Extract URLs from text."""
        return _URL_RE.findall(text)

    @staticmethod
    async def _fetch_url(url: str) -> str:
        """Fetch URL content. Best-effort, no auth.

        Falls back to unverified SSL if the default context fails
        (common in sandboxed / proxy environments).
        """
        import ssl
        import urllib.request

        contexts = [None, ssl._create_unverified_context()]  # noqa: S323
        last_exc: Exception | None = None

        for ctx in contexts:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": USER_AGENT},
                )
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    content = resp.read().decode("utf-8", errors="replace")
                    return content[:50000]  # Hard limit
            except Exception as exc:
                last_exc = exc
                is_ssl = "SSL" in str(exc) or "CERTIFICATE" in str(exc)
                if ctx is None and is_ssl:
                    continue  # retry with unverified context
                raise RuntimeError(f"Fetch failed: {exc}") from exc

        raise RuntimeError(f"Fetch failed: {last_exc}") from last_exc
