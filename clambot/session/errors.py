"""Session error types."""

from __future__ import annotations


class SessionStorageError(Exception):
    """Raised when session storage operations fail."""


class SessionValidationError(Exception):
    """Raised when session data validation fails."""
