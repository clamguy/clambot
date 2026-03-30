"""Session — Conversation session management."""

from clambot.session.compaction import maybe_auto_compact_session
from clambot.session.errors import SessionStorageError, SessionValidationError
from clambot.session.history import turns_to_llm_history
from clambot.session.key import decode_session_key, encode_session_key
from clambot.session.manager import SessionManager
from clambot.session.types import SessionRecord, SessionTurn

__all__ = [
    "encode_session_key",
    "decode_session_key",
    "SessionTurn",
    "SessionRecord",
    "SessionManager",
    "turns_to_llm_history",
    "maybe_auto_compact_session",
    "SessionStorageError",
    "SessionValidationError",
]
