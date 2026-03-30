"""Secret store — persistent, atomic, permission-hardened secret storage.

Secrets are persisted as a JSON object in a single file.  All writes use the
atomic rename pattern (write to a sibling temp file, then ``os.rename()``) so
a crash mid-write never corrupts the store.

File-system permissions are set to 0700 (directory) and 0600 (file) on
POSIX platforms; the ``chmod`` calls are wrapped in ``try/except OSError`` so
the module works on Windows without raising.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "SecretRecord",
    "SecretStore",
]

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass
class SecretRecord:
    """A single stored secret with metadata.

    Attributes:
        name: Unique identifier for the secret (e.g. ``"OPENAI_API_KEY"``).
        value: The secret value (plaintext).
        description: Optional human-readable description of the secret.
        created_at: ISO-8601 timestamp when the secret was first stored.
        updated_at: ISO-8601 timestamp of the most recent update.
    """

    name: str
    value: str
    description: str = ""
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SecretStore:
    """Persistent secret storage with atomic writes and secure file permissions.

    The backing store is a single JSON file whose keys are secret names and
    whose values are dicts matching the :class:`SecretRecord` fields.

    Args:
        path: Absolute path to the JSON secrets file (e.g.
              ``~/.clambot/secrets.json``).
    """

    def __init__(self, path: Path) -> None:
        """Initialize store. Ensures directory has 0700 and file has 0600 permissions."""
        self._path = path
        self._ensure_permissions()

    # ------------------------------------------------------------------
    # Permission bootstrap
    # ------------------------------------------------------------------

    def _ensure_permissions(self) -> None:
        """Create directory (0700) and file (0600) if they don't exist."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError as exc:
            logger.debug("Cannot set directory permissions to 0700: %s", exc)
        if not self._path.exists():
            self._path.write_text("{}")
            try:
                os.chmod(self._path, 0o600)
            except OSError as exc:
                logger.debug("Cannot set file permissions to 0600: %s", exc)

    # ------------------------------------------------------------------
    # Internal I/O helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        """Load the secrets JSON file.

        Returns:
            Dict mapping secret names to their raw field dicts.

        Raises:
            OSError: If the file cannot be read.
            json.JSONDecodeError: If the file contains invalid JSON.
        """
        text = self._path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data  # type: ignore[return-value]

    def _save(self, data: dict[str, dict]) -> None:
        """Atomic write: write to temp file then ``os.rename()``.

        Writing to a sibling temp file (same filesystem) and then renaming
        is atomic on POSIX — a crash mid-write leaves the original file
        intact.

        Args:
            data: Full secrets dict to persist.
        """
        dir_path = self._path.parent
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            try:
                os.chmod(tmp_path, 0o600)
            except OSError as exc:
                logger.debug("Cannot set temp file permissions to 0600: %s", exc)
            os.rename(tmp_path, self._path)
        except Exception:
            # Clean up the temp file if anything goes wrong before the rename.
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                logger.debug("Failed to clean up temp file %s: %s", tmp_path, exc)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str) -> str | None:
        """Get a secret value by name.

        Args:
            name: The secret name to look up.

        Returns:
            The secret value string, or ``None`` if the name is not found.
        """
        data = self._load()
        record = data.get(name)
        if record is None:
            return None
        return record.get("value")

    def save(self, name: str, value: str, description: str = "") -> None:
        """Save or update a secret with atomic write.

        On first creation ``created_at`` is set to the current UTC time.
        ``updated_at`` is always refreshed to the current UTC time.

        Args:
            name: Unique secret name.
            value: The secret value to store.
            description: Optional human-readable description.
        """
        data = self._load()
        now = datetime.now(tz=UTC).isoformat()
        existing = data.get(name, {})
        data[name] = {
            "name": name,
            "value": value,
            "description": description if description else existing.get("description", ""),
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
        }
        self._save(data)

    def list(self) -> dict[str, SecretRecord]:
        """Return all secrets as :class:`SecretRecord` objects.

        Returns:
            Dict mapping secret names to their :class:`SecretRecord`
            representations.  The ``value`` field is included — callers are
            responsible for handling it securely.
        """
        data = self._load()
        result: dict[str, SecretRecord] = {}
        for name, raw in data.items():
            result[name] = SecretRecord(
                name=raw.get("name", name),
                value=raw.get("value", ""),
                description=raw.get("description", ""),
                created_at=raw.get("created_at", ""),
                updated_at=raw.get("updated_at", ""),
            )
        return result
