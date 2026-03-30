"""Session data types.

``SessionTurn`` is intentionally *not* frozen so that compaction metadata can
be updated in-place without creating new objects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SessionTurn:
    """A single turn in a conversation session.

    Attributes:
        role: Speaker role — one of ``"user"``, ``"assistant"``, ``"system"``,
              or ``"tool"``.
        content: Text content of the turn.
        timestamp: Unix epoch seconds when the turn was created.
        metadata: Arbitrary key/value pairs (e.g. compaction markers,
                  tool call IDs).
    """

    role: str
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class SessionRecord:
    """Full record for a session, including all turns and top-level metadata.

    Attributes:
        key: Raw session key (e.g. ``"telegram:12345"``).
        created_at: Unix epoch seconds when the session was first created.
        turns: Ordered list of conversation turns.
        metadata: Arbitrary session-level metadata.
    """

    key: str
    created_at: float
    turns: list[SessionTurn]
    metadata: dict = field(default_factory=dict)
