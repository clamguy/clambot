"""Generation response adapter — normalizes raw LLM output to GenerationResult.

Parses the LLM's generation response (which may be JSON with metadata or
raw JavaScript) into a structured GenerationResult.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from clambot.utils.text import strip_markdown_fences


@dataclass
class GenerationResult:
    """Structured result from clam code generation."""

    language: str = "javascript"
    script: str = ""
    declared_tools: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def normalize_generation_response(raw: str) -> GenerationResult:
    """Parse and normalize a raw LLM generation response.

    Handles multiple response formats:
      1. JSON object with ``script``, ``declared_tools``, etc.
      2. Markdown code block containing JavaScript
      3. Raw JavaScript code

    Args:
        raw: The raw LLM response string.

    Returns:
        A normalized GenerationResult.
    """
    text = raw.strip()

    # Try JSON format first
    result = _try_parse_json(text)
    if result is not None:
        return result

    # Try markdown code block
    result = _try_parse_code_block(text)
    if result is not None:
        return result

    # Fallback: treat entire response as JavaScript
    return GenerationResult(script=text)


def _try_parse_json(text: str) -> GenerationResult | None:
    """Try to parse the response as a JSON object.

    Handles several formats:
      - Clean JSON object
      - JSON wrapped in ```json or ``` markdown fences
      - JSON embedded in prose (with surrounding text / ```json fences)
      - JSON with nested objects in any key order
    """
    # 1. Strip markdown fences if the text starts with them
    cleaned = strip_markdown_fences(text)

    result = _try_parse_json_string(cleaned)
    if result is not None:
        return result

    # 2. Search for ```json fenced blocks within the text
    json_block_match = _JSON_BLOCK_RE.search(text)
    if json_block_match:
        block_content = json_block_match.group(1).strip()
        result = _try_parse_json_string(block_content)
        if result is not None:
            return result

    # 3. Find JSON objects embedded in prose by scanning for '{'
    #    positions and attempting json.loads from each one.  This
    #    handles nested objects (e.g. "metadata": {...}) appearing
    #    before the "script" key — the old regex approach failed on
    #    those because [^{}] couldn't skip inner braces.
    result = _find_embedded_json(text)
    if result is not None:
        return result

    return None


# Match ```json code blocks
_JSON_BLOCK_RE = re.compile(
    r"```json\s*\n(.*?)```",
    re.DOTALL,
)


def _find_embedded_json(text: str) -> GenerationResult | None:
    """Scan *text* for a JSON object containing a ``"script"`` key.

    Iterates over every ``{`` in the text and attempts
    ``json.loads`` from that position.  Stops at the first valid
    JSON dict that contains a ``"script"`` key.  This is robust to
    arbitrary key ordering and nested objects.
    """
    start = 0
    while True:
        idx = text.find("{", start)
        if idx == -1:
            break
        # Quick heuristic: only try positions where "script" appears
        # somewhere after this brace to avoid expensive json.loads on
        # every '{' in the text.
        if '"script"' not in text[idx:]:
            break
        result = _try_parse_json_string(text[idx:])
        if result is not None:
            return result
        start = idx + 1
    return None


_JSON_DECODER = json.JSONDecoder()


def _try_parse_json_string(text: str) -> GenerationResult | None:
    """Try to parse a string as a JSON generation response.

    Uses ``json.JSONDecoder.raw_decode`` so that trailing non-JSON
    text (prose after the closing ``}``) is tolerated.
    """
    try:
        data, _ = _JSON_DECODER.raw_decode(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    # Must have a script field
    script = data.get("script", "")
    if not script:
        return None

    return GenerationResult(
        language=data.get("language", "javascript"),
        script=script,
        declared_tools=data.get("declared_tools", []),
        inputs=data.get("inputs", {}),
        metadata=data.get("metadata", {}),
    )


# Match ```javascript or ```js code blocks
_CODE_BLOCK_RE = re.compile(
    r"```(?:javascript|js)?\s*\n(.*?)```",
    re.DOTALL,
)


def _try_parse_code_block(text: str) -> GenerationResult | None:
    """Try to extract JavaScript from a markdown code block."""
    match = _CODE_BLOCK_RE.search(text)
    if match:
        script = match.group(1).strip()
        if script:
            # Try to extract metadata from surrounding text
            declared_tools = _extract_declared_tools(text)
            metadata = _extract_metadata(text)
            return GenerationResult(
                script=script,
                declared_tools=declared_tools,
                metadata=metadata,
            )
    return None


def _extract_declared_tools(text: str) -> list[str]:
    """Try to extract declared_tools from surrounding text/JSON."""
    # Look for declared_tools in JSON-like structures
    match = re.search(r'"declared_tools"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if match:
        tools_str = match.group(1)
        tools = re.findall(r'"(\w+)"', tools_str)
        return tools
    return []


def _extract_metadata(text: str) -> dict[str, Any]:
    """Try to extract metadata from surrounding text/JSON."""
    metadata: dict[str, Any] = {}

    # Look for reusable flag
    if re.search(r'"reusable"\s*:\s*true', text, re.IGNORECASE):
        metadata["reusable"] = True

    # Look for source_request
    match = re.search(r'"source_request"\s*:\s*"([^"]*)"', text)
    if match:
        metadata["source_request"] = match.group(1)

    # Look for description
    match = re.search(r'"description"\s*:\s*"([^"]*)"', text)
    if match:
        metadata["description"] = match.group(1)

    return metadata
