"""Session key encoding and decoding utilities.

Session keys (e.g. "telegram:12345") are base64url-encoded (without padding)
to produce safe filesystem filenames. Legacy files that used ``_`` as a
separator are auto-detected and migrated on load.
"""

from __future__ import annotations

import base64
from pathlib import Path


def encode_session_key(key: str) -> str:
    """Encode a session key to a URL-safe, padding-free base64url string.

    Args:
        key: Raw session key, e.g. ``"telegram:12345"``.

    Returns:
        Base64url-encoded string with ``=`` padding stripped.
    """
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def decode_session_key(encoded: str) -> str:
    """Decode a base64url-encoded session key back to its original form.

    Padding is re-added before decoding to satisfy the base64 spec.

    Args:
        encoded: Base64url string (without ``=`` padding).

    Returns:
        Original session key string.
    """
    # Re-add stripped padding: base64 requires length % 4 == 0
    padding = (4 - len(encoded) % 4) % 4
    padded = encoded + "=" * padding
    return base64.urlsafe_b64decode(padded).decode()


def find_legacy_path(sessions_dir: Path, key: str) -> Path | None:
    """Check for a legacy session file that used ``_`` instead of ``:`` in the key.

    Older versions stored session files by replacing ``:`` with ``_`` in the
    raw key (e.g. ``telegram_12345.jsonl``). This function looks for such a
    file so callers can migrate it transparently.

    Args:
        sessions_dir: Directory where session JSONL files are stored.
        key: Raw session key, e.g. ``"telegram:12345"``.

    Returns:
        The legacy :class:`~pathlib.Path` if it exists, otherwise ``None``.
    """
    legacy_name = key.replace(":", "_") + ".jsonl"
    legacy_path = sessions_dir / legacy_name
    return legacy_path if legacy_path.exists() else None
