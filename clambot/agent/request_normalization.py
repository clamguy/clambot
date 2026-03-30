"""Request normalization — NFKC + punctuation stripping for pre-selection matching.

Used by the selector's Stage 1 pre-selection to match incoming requests
against cached clam ``source_request`` values without LLM involvement.
"""

from __future__ import annotations

import re
import unicodedata

# Strip punctuation and extra whitespace
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_request(text: str) -> str:
    """Normalize a request string for exact-match comparison.

    Steps:
        1. NFKC Unicode normalization (compatibility decomposition + composition)
        2. Lowercase
        3. Strip punctuation (keep letters, digits, whitespace)
        4. Collapse whitespace to single spaces
        5. Strip leading/trailing whitespace

    Examples:
        >>> normalize_request("What's the weather?")
        'whats the weather'
        >>> normalize_request("  Hello,  World!  ")
        'hello world'
    """
    # 1. NFKC normalization
    text = unicodedata.normalize("NFKC", text)

    # 2. Lowercase
    text = text.lower()

    # 3. Strip punctuation
    text = _PUNCTUATION_RE.sub("", text)

    # 4. Collapse whitespace
    text = _WHITESPACE_RE.sub(" ", text)

    # 5. Strip
    return text.strip()
