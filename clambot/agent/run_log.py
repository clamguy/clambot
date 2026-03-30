"""Run log builder — structured execution event logging.

Records events during clam execution for diagnostics and tracing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunLogEvent:
    """A single event in a run log.

    Attributes:
        type: Event type string (e.g. ``"tool_call"``, ``"error"``, ``"output"``).
        timestamp: Unix timestamp (seconds) when the event occurred.
        data: Arbitrary event payload.
    """

    type: str
    timestamp: float
    data: dict[str, Any] = field(default_factory=dict)


class RunLogBuilder:
    """Builds a structured log of execution events for a single clam run.

    Provides an append-only interface for recording events and a summary
    method for producing a finalized log.
    """

    def __init__(self, run_id: str = "") -> None:
        self._run_id = run_id
        self._events: list[RunLogEvent] = []
        self._start_time: float = time.time()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def events(self) -> list[RunLogEvent]:
        return list(self._events)

    def append(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Append an event to the log.

        Args:
            event_type: The type of event (e.g. ``"tool_call"``).
            data: Optional payload dict.
        """
        self._events.append(
            RunLogEvent(
                type=event_type,
                timestamp=time.time(),
                data=data or {},
            )
        )

    def append_tool_call(self, tool_name: str, args: dict[str, Any]) -> None:
        """Record a tool call event."""
        self.append("tool_call", {"tool_name": tool_name, "args": args})

    def append_tool_result(self, tool_name: str, result: Any) -> None:
        """Record a tool result event."""
        result_str = str(result) if result is not None else ""
        self.append("tool_result", {"tool_name": tool_name, "result": result_str[:2000]})

    def append_error(self, error_code: str, message: str) -> None:
        """Record an error event."""
        self.append("error", {"code": error_code, "message": message})

    def append_output(self, output: str) -> None:
        """Record sandbox output."""
        self.append("output", {"content": output[:4000]})

    def summary(self) -> dict[str, Any]:
        """Produce a summary of the run log.

        Returns:
            Dict with ``run_id``, ``duration_ms``, ``event_count``, and
            ``events`` list.
        """
        duration_ms = int((time.time() - self._start_time) * 1000)
        return {
            "run_id": self._run_id,
            "duration_ms": duration_ms,
            "event_count": len(self._events),
            "events": [
                {
                    "type": e.type,
                    "timestamp": e.timestamp,
                    "data": e.data,
                }
                for e in self._events
            ],
        }
