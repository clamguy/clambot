"""Generation grounding — post-processing rules for generated clam code.

Applies grounding rules to the generated script to ensure it conforms
to ClamBot's requirements (JS-only, no forbidden patterns, etc.).
"""

from __future__ import annotations

import re

from clambot.utils.text import strip_markdown_fences

from .generation_adapter import GenerationResult


def apply_grounding_rules(result: GenerationResult) -> GenerationResult:
    """Apply post-processing grounding rules to a generated clam.

    Rules applied:
      1. Ensure language is ``javascript``
      2. Strip any ``require()`` or ``import`` statements
      3. Strip any ``fetch()`` calls (must use ``http_request`` tool)
      4. Remove ``/workspace/`` path prefixes (VFS collision)
      5. Clean up common formatting issues

    Args:
        result: The raw generation result.

    Returns:
        A cleaned GenerationResult with grounding rules applied.
    """
    script = result.script

    if not script:
        return result

    # 1. Force language to javascript
    result.language = "javascript"

    # 2. Reject scripts that use Node.js APIs (require, import, fetch, process)
    #    These will always crash in the QuickJS WASM sandbox.  Catching them
    #    here avoids wasting a runtime round-trip and gives the LLM a precise
    #    self-fix hint pointing at the built-in tools.
    _nodejs_patterns = [
        (r"\brequire\s*\(", "require()"),
        (r"\bimport\s+.*\bfrom\b", "import ... from"),
        (r"\bfetch\s*\(", "fetch()"),
        (r"\bprocess\.env\b", "process.env"),
        (r"\bfs\.promises\b", "fs.promises"),
        (r"\bfs\.readFileSync\b", "fs.readFileSync"),
        (r"\bfs\.writeFileSync\b", "fs.writeFileSync"),
    ]
    _nodejs_found = [label for pat, label in _nodejs_patterns if re.search(pat, script)]
    if _nodejs_found:
        result.error = (
            f"Rejected: script uses Node.js APIs ({', '.join(_nodejs_found)}) "
            "which are not available in the WASM sandbox. "
            "Use the built-in tools instead: "
            'await fs({{operation: "read", path: ...}}) for file operations, '
            'await http_request({{url: ..., method: "GET"}}) for HTTP, '
            "await web_fetch({{url: ...}}) for web pages. "
            "Do NOT use require(), import, fetch(), or process.env."
        )
        result.script = script
        return result

    # 3. Remove /workspace/ prefix in string literals
    script = re.sub(r'"/workspace/', '"', script)
    script = re.sub(r"'/workspace/", "'", script)

    # 4. Clean up trailing whitespace
    lines = script.split("\n")
    lines = [line.rstrip() for line in lines]
    script = "\n".join(lines)

    # 5. Ensure script doesn't start with markdown fences
    if script.startswith("```"):
        script = strip_markdown_fences(script)

    # 6. Reject hardcoded refusal clams — scripts that just return a
    #    "sorry / can't / unable" string without calling any tools are
    #    refusals that should never be promoted.
    lower = script.lower()
    refusal_markers = [
        "i'm sorry",
        "i can't",
        "i cannot",
        "i'm unable",
        "i am unable",
        "just a chat assistant",
        "please use a",
    ]
    is_refusal = any(marker in lower for marker in refusal_markers)

    # Also detect non-code output (plain text without any JS syntax)
    has_code = any(
        kw in script
        for kw in [
            "function",
            "return",
            "await",
            "const ",
            "let ",
            "var ",
            "=>",
            "async",
            "console.",
            "require(",
            "import ",
            "class ",
            "throw ",
            "if (",
            "if(",
            "for (",
            "for(",
            "while(",
            "while (",
        ]
    )

    if is_refusal or (not has_code and len(script.strip()) > 0):
        result.error = (
            "Rejected: script is not valid JavaScript. "
            "Generate an async function run(args) that uses the available "
            "tools (web_fetch, http_request, etc.) to fulfill the request."
        )

    result.script = script
    return result
