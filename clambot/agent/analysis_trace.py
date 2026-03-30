"""Analysis trace builder — records post-runtime analysis decisions.

Captures the sequence of analysis steps (ACCEPT, SELF_FIX, REJECT) for
debugging and diagnostics.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AnalysisTraceEntry:
    """Single entry in an analysis trace."""

    attempt: int
    decision: str
    reason: str = ""
    fix_instructions: str = ""
    timestamp: float = field(default_factory=time.time)


class AnalysisTraceBuilder:
    """Builds a trace of post-runtime analysis decisions.

    Used to record the sequence of analysis attempts during the self-fix
    loop for diagnostics and debugging.
    """

    def __init__(self) -> None:
        self._entries: list[AnalysisTraceEntry] = []

    def record(
        self,
        attempt: int,
        decision: str,
        reason: str = "",
        fix_instructions: str = "",
    ) -> None:
        """Record an analysis decision."""
        self._entries.append(
            AnalysisTraceEntry(
                attempt=attempt,
                decision=decision,
                reason=reason,
                fix_instructions=fix_instructions,
            )
        )

    @property
    def entries(self) -> list[AnalysisTraceEntry]:
        return list(self._entries)

    @property
    def last_decision(self) -> str | None:
        return self._entries[-1].decision if self._entries else None

    def summary(self) -> dict[str, Any]:
        """Produce a summary dict for logging/diagnostics."""
        return {
            "total_attempts": len(self._entries),
            "final_decision": self.last_decision,
            "entries": [
                {
                    "attempt": e.attempt,
                    "decision": e.decision,
                    "reason": e.reason,
                }
                for e in self._entries
            ],
        }
