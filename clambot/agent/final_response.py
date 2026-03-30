"""Final response selection — picks the output to return to the user.

Chooses between the analysis result output and the raw runtime result
based on the analysis decision.
"""

from __future__ import annotations

from typing import Any

from clambot.utils.text import get_field

from .post_runtime_analysis import PostRuntimeAnalysisDecision


def select_final_response(
    analysis_result: Any | None,
    runtime_result: Any | None,
) -> str:
    """Select the final response string from analysis and runtime results.

    Priority:
        1. If analysis says ACCEPT and has output → use analysis output
        2. If runtime has output → use runtime output
        3. If runtime has error → use error message
        4. Fallback → generic failure message

    Args:
        analysis_result: Result from post-runtime analysis (has `output`, `decision`).
        runtime_result: RuntimeResult from clam execution (has `output`, `error`).

    Returns:
        The final response string to send to the user.
    """
    # Extract analysis output if available
    if analysis_result is not None:
        decision = get_field(analysis_result, "decision", "")
        analysis_output = get_field(analysis_result, "output", "")

        if decision == PostRuntimeAnalysisDecision.ACCEPT and analysis_output:
            return str(analysis_output)

    # Fall back to runtime output
    if runtime_result is not None:
        output = get_field(runtime_result, "output", "")
        if output:
            return str(output)

        error = get_field(runtime_result, "error", "")
        if error:
            return f"Execution failed: {error}"

    return "I wasn't able to complete that request."
