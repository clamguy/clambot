"""Session manager — persistent JSONL-backed conversation history.

Design notes:
- JSONL files are append-only; they are never rewritten in place (crash-safe).
- In-memory cache is keyed by raw session key and populated lazily on first
  ``load_history()`` call.
- Lines with ``_type: "metadata"`` are skipped during load (reserved for
  future session-level metadata records).
- Legacy filenames (``channel_chatid.jsonl``) are auto-detected and read
  transparently; the canonical encoded filename is used for all new writes.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from clambot.session.errors import SessionStorageError
from clambot.session.key import encode_session_key, find_legacy_path
from clambot.session.types import SessionTurn

log = logging.getLogger(__name__)


class SessionManager:
    """Manages per-session conversation history backed by JSONL files.

    Args:
        workspace: Root workspace directory. Session files are stored under
                   ``workspace/sessions/``.
    """

    def __init__(self, workspace: Path) -> None:
        self.sessions_dir: Path = workspace / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, list[SessionTurn]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_history(self, key: str) -> list[SessionTurn]:
        """Return the turn history for *key*, loading from disk if needed.

        Args:
            key: Raw session key (e.g. ``"telegram:12345"``).

        Returns:
            Ordered list of :class:`~clambot.session.types.SessionTurn` objects.
        """
        if key not in self._cache:
            self._cache[key] = self._load_from_disk(key)
        return self._cache[key]

    def append_turn(
        self,
        key: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> None:
        """Append a new turn to the session history.

        The turn is added to the in-memory cache (loading from disk first if
        the session is not yet cached) and immediately persisted to the JSONL
        file.

        Args:
            key: Raw session key.
            role: Speaker role (``"user"``, ``"assistant"``, ``"system"``,
                  ``"tool"``).
            content: Text content of the turn.
            metadata: Optional metadata dict attached to the turn.
        """
        turn = SessionTurn(
            role=role,
            content=content,
            timestamp=time.time(),
            metadata=metadata or {},
        )
        # Ensure cache is populated before appending
        if key not in self._cache:
            self._cache[key] = self._load_from_disk(key)
        self._cache[key].append(turn)
        self._append_to_disk(key, turn)

    def reset_session(self, key: str) -> None:
        """Remove *key* from the in-memory cache.

        The JSONL file on disk is left untouched — this only clears the
        cached view so the next ``load_history()`` call re-reads from disk.

        Args:
            key: Raw session key to evict from the cache.
        """
        self._cache.pop(key, None)

    def clear_session(self, key: str) -> None:
        """Evict *key* from the cache **and** truncate the JSONL file.

        Use this after successful memory consolidation (``/new``) so the
        next conversation starts with an empty history.  The on-disk file
        is preserved (as an empty file) rather than deleted, so the
        session path remains stable.

        Args:
            key: Raw session key.
        """
        self._cache.pop(key, None)
        path = self._session_path(key)
        if path.exists():
            try:
                path.write_text("", encoding="utf-8")
            except OSError as exc:
                log.warning("Failed to truncate session file %s: %s", path, exc)

    def rewrite_session(self, key: str, turns: list[SessionTurn]) -> None:
        """Replace the entire session — both cache and disk — with *turns*.

        Used by auto-compaction to persist the compacted view so that
        a process restart reloads the compact history rather than the
        full pre-compaction JSONL.

        Args:
            key: Raw session key.
            turns: The new canonical turn list (e.g. summary + recent).
        """
        self._cache[key] = list(turns)
        self._rewrite_disk(key, turns)

    def _rewrite_disk(self, key: str, turns: list[SessionTurn]) -> None:
        """Atomically rewrite the JSONL file for *key*.

        Writes to a temporary sibling file and renames — prevents
        data loss on crash.
        """
        path = self._session_path(key)
        tmp_path = path.with_suffix(".jsonl.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                for turn in turns:
                    record = {
                        "role": turn.role,
                        "content": turn.content,
                        "timestamp": turn.timestamp,
                        "metadata": turn.metadata,
                    }
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            tmp_path.replace(path)
        except OSError as exc:
            # Clean up temp file on failure
            tmp_path.unlink(missing_ok=True)
            raise SessionStorageError(f"Cannot rewrite session file {path}: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session_path(self, key: str) -> Path:
        """Return the canonical JSONL path for *key*.

        Args:
            key: Raw session key.

        Returns:
            Absolute path to the ``.jsonl`` file.
        """
        return self.sessions_dir / f"{encode_session_key(key)}.jsonl"

    def _load_from_disk(self, key: str) -> list[SessionTurn]:
        """Read and parse the JSONL file for *key*.

        Falls back to a legacy filename (``channel_chatid.jsonl``) if the
        canonical encoded file does not exist. Lines that cannot be parsed or
        that carry ``_type: "metadata"`` are skipped with a warning.

        Args:
            key: Raw session key.

        Returns:
            Ordered list of :class:`~clambot.session.types.SessionTurn` objects.
        """
        path = self._session_path(key)

        if not path.exists():
            legacy = find_legacy_path(self.sessions_dir, key)
            if legacy is not None:
                log.info(
                    "Session %r: using legacy path %s "
                    "(will write to canonical path on next append)",
                    key,
                    legacy,
                )
                path = legacy
            else:
                return []

        turns: list[SessionTurn] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SessionStorageError(f"Cannot read session file {path}: {exc}") from exc

        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Session %r: skipping malformed JSON on line %d", key, lineno)
                continue

            # Skip metadata-only records (reserved for future use)
            if data.get("_type") == "metadata":
                continue

            try:
                turns.append(
                    SessionTurn(
                        role=data["role"],
                        content=data["content"],
                        timestamp=data.get("timestamp", 0.0),
                        metadata=data.get("metadata", {}),
                    )
                )
            except KeyError as exc:
                log.warning(
                    "Session %r: skipping line %d — missing field %s",
                    key,
                    lineno,
                    exc,
                )

        return turns

    def _append_to_disk(self, key: str, turn: SessionTurn) -> None:
        """Append a single turn as a JSON line to the session JSONL file.

        The file is opened in append mode so existing content is never
        overwritten (crash-safe).

        Args:
            key: Raw session key.
            turn: The :class:`~clambot.session.types.SessionTurn` to persist.

        Raises:
            :class:`~clambot.session.errors.SessionStorageError`: If the write
                fails.
        """
        path = self._session_path(key)
        record = {
            "role": turn.role,
            "content": turn.content,
            "timestamp": turn.timestamp,
            "metadata": turn.metadata,
        }
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise SessionStorageError(f"Cannot write to session file {path}: {exc}") from exc
