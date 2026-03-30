"""Provider-backed post-runtime analyzer.

Analyzes the results of a clam execution and decides whether to
ACCEPT (use the output), SELF_FIX (re-generate), or REJECT (report error).
"""

from __future__ import annotations

import logging
from typing import Any

from clambot.providers.base import LLMProvider
from clambot.utils.text import get_field

from .post_runtime_analysis import PostRuntimeAnalysisDecision
from .post_runtime_analysis_adapter import AnalysisResult, normalize_analysis_response

logger = logging.getLogger(__name__)


ANALYSIS_SYSTEM_PROMPT = """\
You are a post-execution analyzer for ClamBot. A JavaScript clam was generated and executed in a WASM sandbox to fulfill the user's request.

Analyze the execution result and decide:

1. **ACCEPT** — The execution succeeded and the output correctly addresses the user's request. In the "output" field, provide a **concise, direct answer to what the user asked**. Focus only on the information the user requested — do not dump all available data. If the runtime output is JSON or structured data, extract the relevant value and present it naturally. Never pass raw JSON to the user.
2. **SELF_FIX** — The execution had errors or produced incorrect output, but the issue is fixable. Provide specific fix instructions.
3. **REJECT** — The execution failed in a way that cannot be automatically fixed. Provide an error explanation for the user.
4. **NEED_FULL_OUTPUT** — The output was truncated in this prompt, but the user's request requires processing the full data (e.g. summarization, translation, analysis, reformatting). Use this ONLY when the output is marked as truncated AND you need the complete text to fulfill the user's request. The system will re-run this analysis with the full output.

**Post-processing**: When the user's request asks for transformation of the \
output (translation, summarization, reformatting, analysis, etc.), apply that \
transformation in the "output" field. For example, if the user asked to "fetch \
page X and translate to Russian", the clam fetches the page — you translate \
the content to Russian in your output. The clam handles data retrieval; you \
handle language/text transformation.

Respond with a JSON object (no markdown fences):
{{"decision": "ACCEPT"|"SELF_FIX"|"REJECT"|"NEED_FULL_OUTPUT", "output": "<human-readable text for the user>", "fix_instructions": "<if SELF_FIX, specific instructions for regeneration>", "reason": "<brief explanation of your decision>"}}
"""


class ProviderBackedPostRuntimeAnalyzer:
    """Analyzes clam execution results using an LLM.

    Called after each clam execution to determine whether the result
    is acceptable, needs fixing, or should be rejected.
    """

    def __init__(
        self,
        provider: LLMProvider,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> None:
        self._provider = provider
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def analyze(
        self,
        message: str,
        clam: Any,
        runtime_result: Any,
        *,
        full_output: bool = False,
    ) -> AnalysisResult:
        """Analyze a clam execution result.

        Args:
            message: The original user request.
            clam: The clam that was executed.
            runtime_result: The RuntimeResult from execution.
            full_output: If True, send the full (untruncated) runtime output
                to the analysis LLM.  Used on a second pass after the LLM
                returns ``NEED_FULL_OUTPUT``.

        Returns:
            An AnalysisResult with the decision and details.
        """
        # Extract fields from runtime result
        output = get_field(runtime_result, "output", "")
        error = get_field(runtime_result, "error", "")
        stderr = get_field(runtime_result, "stderr", "")
        timed_out = get_field(runtime_result, "timed_out", False)

        # Quick decisions without LLM
        if timed_out:
            return AnalysisResult(
                decision=PostRuntimeAnalysisDecision.SELF_FIX,
                fix_instructions=(
                    "The script timed out. Make it more efficient or reduce its scope."
                ),
                reason="Execution timed out",
            )

        if error and not output:
            return AnalysisResult(
                decision=PostRuntimeAnalysisDecision.SELF_FIX,
                output="",
                fix_instructions=f"Script error: {error}\nStderr: {stderr}",
                reason=f"Execution error: {error}",
            )

        # For successful executions, use LLM to evaluate quality
        context = self._build_context(
            message,
            clam,
            runtime_result,
            full_output=full_output,
        )

        messages = [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ]

        try:
            response = await self._provider.acomplete(
                messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )

            return normalize_analysis_response(response.content)

        except Exception as exc:
            logger.warning("Analysis LLM call failed: %s", exc)
            # If LLM fails but we have output, reject to avoid
            # promoting a potentially broken clam.
            if output:
                return AnalysisResult(
                    decision=PostRuntimeAnalysisDecision.REJECT,
                    output=output,
                    reason=f"LLM analysis failed ({exc}); rejecting to prevent bad promotion",
                )
            return AnalysisResult(
                decision=PostRuntimeAnalysisDecision.REJECT,
                output=f"Analysis failed: {exc}",
                reason=str(exc),
            )

    def _build_context(
        self,
        message: str,
        clam: Any,
        runtime_result: Any,
        *,
        full_output: bool = False,
    ) -> str:
        """Build the analysis context string."""
        parts = [f"USER REQUEST: {message}"]

        # Script
        script = get_field(clam, "script", "")
        if script:
            parts.append(f"SCRIPT:\n```javascript\n{script[:3000]}\n```")

        # Output — truncate unless full_output requested (NEED_FULL_OUTPUT retry)
        # Even in full_output mode, apply a hard ceiling to avoid exceeding
        # the LLM context window (100K chars ≈ 25K tokens).
        _FULL_OUTPUT_CEILING = 100_000  # noqa: N806
        _PREVIEW_LIMIT = 3000  # noqa: N806
        output = get_field(runtime_result, "output", "")
        if output:
            if full_output:
                if len(output) > _FULL_OUTPUT_CEILING:
                    parts.append(
                        f"OUTPUT (first {_FULL_OUTPUT_CEILING} of {len(output)} chars):\n"
                        f"{output[:_FULL_OUTPUT_CEILING]}"
                    )
                else:
                    parts.append(f"OUTPUT:\n{output}")
            elif len(output) <= _PREVIEW_LIMIT:
                parts.append(f"OUTPUT:\n{output}")
            else:
                parts.append(
                    f"OUTPUT (showing first {_PREVIEW_LIMIT} of {len(output)} chars — "
                    f"full output is available. If you need the full text to "
                    f"fulfill the user's request, respond with NEED_FULL_OUTPUT):\n"
                    f"{output[:_PREVIEW_LIMIT]}"
                )

        # Error
        error = get_field(runtime_result, "error", "")
        if error:
            parts.append(f"ERROR: {error}")

        # Stderr
        stderr = get_field(runtime_result, "stderr", "")
        if stderr:
            parts.append(f"STDERR:\n{stderr[:1000]}")

        return "\n\n".join(parts)
