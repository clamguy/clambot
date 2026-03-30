"""Post-runtime analysis decision enum.

Defines the possible outcomes of analyzing a clam's execution result.
"""

from __future__ import annotations

from enum import Enum


class PostRuntimeAnalysisDecision(str, Enum):
    """Decision from post-runtime analysis of a clam execution."""

    ACCEPT = "ACCEPT"
    """Execution succeeded — promote clam and use output."""

    SELF_FIX = "SELF_FIX"
    """Execution had issues — re-generate with error context."""

    REJECT = "REJECT"
    """Execution failed irrecoverably — report error to user."""

    NEED_FULL_OUTPUT = "NEED_FULL_OUTPUT"
    """Output was truncated but the user's request requires the full data
    (e.g. summarization, translation).  Re-run analysis with full output."""
