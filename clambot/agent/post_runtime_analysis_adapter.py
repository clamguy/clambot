"""Post-runtime analysis response adapter.

Normalizes the raw LLM analysis output into a structured AnalysisResult.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from clambot.utils.text import strip_markdown_fences

from .post_runtime_analysis import PostRuntimeAnalysisDecision


@dataclass
class AnalysisResult:
    """Structured result from post-runtime analysis."""

    decision: PostRuntimeAnalysisDecision = PostRuntimeAnalysisDecision.ACCEPT
    output: str = ""
    fix_instructions: str = ""
    reason: str = ""


def normalize_analysis_response(raw: str) -> AnalysisResult:
    """Parse and normalize a raw LLM analysis response.

    Expected JSON format:
        {
            "decision": "ACCEPT" | "SELF_FIX" | "REJECT",
            "output": "<cleaned output for user>",
            "fix_instructions": "<if SELF_FIX, what to fix>",
            "reason": "<brief explanation>"
        }

    Falls back to REJECT if parsing fails — only explicit ACCEPT promotes clams.
    """
    # Strip markdown fences
    text = strip_markdown_fences(raw)

    data = _try_parse_json(text)
    if data is None:
        return _fallback_parse(raw)

    decision_str = data.get("decision", "REJECT").upper()
    try:
        decision = PostRuntimeAnalysisDecision(decision_str)
    except ValueError:
        decision = PostRuntimeAnalysisDecision.REJECT

    return AnalysisResult(
        decision=decision,
        output=data.get("output", ""),
        fix_instructions=data.get("fix_instructions", ""),
        reason=data.get("reason", ""),
    )


def _try_parse_json(text: str) -> dict | None:
    """Try to extract a JSON object from text, handling LLM prose wrapping."""
    # 1. Direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Find first { ... } block containing "decision"
    match = re.search(r'\{[^{}]*"decision"[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _fallback_parse(raw: str) -> AnalysisResult:
    """Fallback parsing when JSON fails — look for decision keywords."""
    text = raw.strip()

    # Check for explicit decision keywords — order matters:
    # NEED_FULL_OUTPUT first, then SELF_FIX/REJECT, then ACCEPT.
    if re.search(r"\bNEED_FULL_OUTPUT\b", text):
        return AnalysisResult(
            decision=PostRuntimeAnalysisDecision.NEED_FULL_OUTPUT,
            output="",
            reason="Parsed from non-JSON response",
        )

    if re.search(r"\bSELF_FIX\b", text):
        return AnalysisResult(
            decision=PostRuntimeAnalysisDecision.SELF_FIX,
            output="",
            fix_instructions=text,
            reason="Parsed from non-JSON response",
        )

    if re.search(r"\bREJECT\b", text):
        return AnalysisResult(
            decision=PostRuntimeAnalysisDecision.REJECT,
            output=text,
            reason="Parsed from non-JSON response",
        )

    if re.search(r"\bACCEPT\b", text):
        return AnalysisResult(
            decision=PostRuntimeAnalysisDecision.ACCEPT,
            output=text,
            reason="Parsed from non-JSON response",
        )

    # No recognized keyword — default to REJECT to avoid promoting bad clams
    return AnalysisResult(
        decision=PostRuntimeAnalysisDecision.REJECT,
        output=text,
        reason="Defaulted to REJECT (no decision keyword found)",
    )
