"""Progress state enum for clam execution lifecycle."""

from __future__ import annotations

from enum import Enum


class ProgressState(str, Enum):
    """Progress states emitted during clam execution.

    These states are used by the gateway orchestrator to send status
    updates to channels (e.g. Telegram typing indicators, status messages).
    """

    DISCOVERING = "DISCOVERING"
    GENERATING = "GENERATING"
    VALIDATING = "VALIDATING"
    EXECUTING = "EXECUTING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
