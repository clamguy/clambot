"""Amla-sandbox WASM runtime backend for clam execution.

Wraps the amla-sandbox ``Sandbox`` API to execute JavaScript clams with
tool dispatch, capability enforcement, and PCA token generation.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from amla_sandbox import MethodCapability, Sandbox, ToolDefinition

from .run_log import RunLogBuilder

# ---------------------------------------------------------------------------
# Runtime result
# ---------------------------------------------------------------------------


@dataclass
class RuntimeResult:
    """Result from a clam execution.

    Attributes:
        output: The string output from the sandbox execution.
        error: Error message if execution failed, empty string on success.
        tool_calls: List of tool call records ``{name, args, result}``.
        stderr: Any stderr output captured from the sandbox.
        timed_out: Whether execution was terminated due to timeout.
        run_log: Optional run log from the execution.
    """

    output: str = ""
    error: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stderr: str = ""
    timed_out: bool = False
    run_log: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Runtime backend
# ---------------------------------------------------------------------------


class AmlaSandboxRuntimeBackend:
    """Runtime backend wrapping amla-sandbox WASM for JavaScript execution.

    This backend:
    - Creates a Sandbox instance per execution
    - Generates PCA tokens automatically
    - Converts BuiltinToolRegistry schemas to amla-sandbox ToolDefinitions
    - Pipes scripts > 7KB via stdin
    - Delegates tool calls to the provided tool_handler
    """

    STDIN_THRESHOLD = 7168  # 7 KB
    SAFE_OUTPUT_CHUNK_SIZE = 900

    def __init__(
        self,
        tool_handler: Callable[[str, dict[str, Any]], Any] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Initialize the backend.

        Args:
            tool_handler: Sync callable ``(method, params) -> result`` for
                         tool dispatch. The ``mcp:`` prefix is stripped by
                         the Sandbox before calling this handler.
            on_event: Optional callback for execution events.
        """
        self._tool_handler = tool_handler
        self._on_event = on_event

    def execute(
        self,
        script: str,
        declared_tools: list[dict[str, Any]] | None = None,
        capabilities: list[MethodCapability] | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> RuntimeResult:
        """Execute a JavaScript script in the WASM sandbox.

        Args:
            script: JavaScript source code to execute.
            declared_tools: List of tool schema dicts (OpenAI format) that the
                          clam is allowed to call.
            capabilities: Optional amla-sandbox capability constraints.
            inputs: Optional arguments to inject as ``const args = ...;``
                    at the top of the script.  If the script defines an
                    ``async function run(args)`` entry point, a trailing
                    ``return await run(args);`` call is appended automatically.

        Returns:
            ``RuntimeResult`` with output, errors, and tool call records.
        """
        run_log = RunLogBuilder()
        tool_calls: list[dict[str, Any]] = []

        # Build tool definitions for the sandbox
        tool_defs = self._build_tool_definitions(declared_tools or [])

        # Build capability list — default to wildcard if none specified
        caps = capabilities or [MethodCapability(method_pattern="**")]

        # Create tool handler that records calls
        def handler(method: str, params: dict[str, Any]) -> Any:
            run_log.append_tool_call(method, params)
            tool_calls.append({"name": method, "args": params})

            if self._tool_handler is not None:
                result = self._tool_handler(method, params)
            else:
                result = {"error": f"No handler for tool: {method}"}

            run_log.append_tool_result(method, result)
            tool_calls[-1]["result"] = result

            if self._on_event:
                self._on_event(
                    {
                        "type": "tool_result",
                        "tool_name": method,
                        "result": result,
                    }
                )

            return result

        # Generate PCA token
        pca = secrets.token_bytes(32)

        # Create sandbox
        sandbox = Sandbox(
            pca=pca,
            tools=tool_defs,
            capabilities=caps,
            tool_handler=handler,
        )

        # Inject inputs and wrap output emission so large return values are
        # printed in small console chunks.  This avoids JSON decode failures
        # in older amla-sandbox bridge builds when a single return payload is
        # too large for one runtime-step response.
        exec_script = _inject_inputs(script, inputs)
        exec_script = _wrap_script_with_chunked_output(
            exec_script,
            chunk_size=self.SAFE_OUTPUT_CHUNK_SIZE,
        )

        # Execute — the Sandbox auto-pipes large scripts via stdin
        try:
            output = sandbox.execute(exec_script)
            stderr = sandbox.last_stderr or ""

            # Strip trailing newline added by sandbox
            if output.endswith("\n"):
                output = output[:-1]

            # Detect sandbox runtime errors reported via stderr.
            # QuickJS reports syntax errors and runtime exceptions in
            # stderr without raising a Python exception, leaving both
            # output and error empty.  Promote stderr to error so the
            # self-fix loop can trigger.
            error = ""
            if not output and stderr:
                error = _extract_stderr_error(stderr)
                if error:
                    run_log.append_error("runtime_execution_error", error)

            run_log.append_output(output)

            return RuntimeResult(
                output=output,
                error=error,
                tool_calls=tool_calls,
                stderr=stderr,
                timed_out=False,
                run_log=run_log.summary(),
            )
        except Exception as exc:
            error_msg = str(exc)
            run_log.append_error("runtime_execution_error", error_msg)

            return RuntimeResult(
                output="",
                error=error_msg,
                tool_calls=tool_calls,
                stderr=getattr(sandbox, "last_stderr", "") or "",
                timed_out=False,
                run_log=run_log.summary(),
            )

    def abort(self) -> None:
        """Abort the current execution (best-effort)."""
        # The Sandbox doesn't expose a direct abort — the caller should
        # use the CancellationToken / timeout mechanism at the ClamRuntime level.
        pass

    @staticmethod
    def _build_tool_definitions(
        declared_tools: list[dict[str, Any]],
    ) -> list[ToolDefinition]:
        """Convert OpenAI-format tool schemas to amla-sandbox ToolDefinitions."""
        defs: list[ToolDefinition] = []
        for tool_schema in declared_tools:
            func = tool_schema.get("function", tool_schema)
            name = func.get("name", "")
            description = func.get("description", "")
            parameters = func.get("parameters", {})

            if name:
                defs.append(
                    ToolDefinition(
                        name=name,
                        description=description,
                        parameters=parameters,
                    )
                )
        return defs


# ---------------------------------------------------------------------------
# Input injection
# ---------------------------------------------------------------------------

_RUN_FUNC_RE = re.compile(
    r"(?:^|\n)\s*(?:async\s+)?function\s+run\s*\(",
)


_RUN_CALL_RE = re.compile(
    r"(?:return\s+)?await\s+run\s*\(\s*args\s*\)\s*;",
)


def _inject_inputs(
    script: str,
    inputs: dict[str, Any] | None,
) -> str:
    """Inject ``inputs`` into a script and auto-invoke ``run()`` if defined.

    Behaviour:
      - If the script defines a ``function run(...)`` entry point **and**
        does not already call ``run(args)``, a trailing
        ``return await run(args);`` is appended.
      - If *inputs* is non-empty, ``const args = <JSON>;`` is prepended.
      - If *inputs* is empty/None but ``run`` is detected, the call uses
        an empty object: ``const args = {};``
      - If neither inputs nor ``run`` are present, the script is returned
        unchanged.
    """
    has_run_def = bool(_RUN_FUNC_RE.search(script))
    already_calls_run = bool(_RUN_CALL_RE.search(script))

    if not inputs and not has_run_def:
        return script

    args_json = json.dumps(inputs or {}, ensure_ascii=False)
    preamble = f"const args = {args_json};"

    if has_run_def and not already_calls_run:
        return f"{preamble}\n{script}\nreturn await run(args);"

    return f"{preamble}\n{script}"


def _wrap_script_with_chunked_output(script: str, chunk_size: int = 900) -> str:
    """Wrap script so return values are emitted in safe-sized console chunks.

    The amla-sandbox bridge in v0.1.7 can fail with ``JSONDecodeError`` when a
    single returned string is large (for example long transcripts).  This wrapper
    executes the user script inside an inner async function, captures its return
    value, and emits it via ``console.log`` chunks to keep each host-op payload
    comfortably below bridge limits.
    """
    return (
        f"const __clambot_chunk_size = {int(chunk_size)};\n"
        "const __clambot_emit_value = (value) => {\n"
        "  if (value === undefined) return;\n"
        "  let text;\n"
        "  if (typeof value === 'string') {\n"
        "    text = value;\n"
        "  } else {\n"
        "    try {\n"
        "      text = JSON.stringify(value);\n"
        "    } catch (_err) {\n"
        "      text = String(value);\n"
        "    }\n"
        "  }\n"
        "  if (text.length <= __clambot_chunk_size) {\n"
        "    console.log(text);\n"
        "    return;\n"
        "  }\n"
        "  for (let i = 0; i < text.length; i += __clambot_chunk_size) {\n"
        "    console.log(text.slice(i, i + __clambot_chunk_size));\n"
        "  }\n"
        "};\n"
        "const __clambot_result = await (async () => {\n"
        f"{script}\n"
        "})();\n"
        "__clambot_emit_value(__clambot_result);"
    )


# ---------------------------------------------------------------------------
# Stderr error extraction
# ---------------------------------------------------------------------------

_STDERR_ERROR_RE = re.compile(
    r"(?:Runtime error|SyntaxError|ReferenceError|TypeError|RangeError"
    r"|InternalError|URIError|EvalError):\s*.+",
)


def _extract_stderr_error(stderr: str) -> str:
    """Extract a meaningful error message from sandbox stderr.

    QuickJS reports syntax errors and runtime exceptions to stderr
    (e.g. ``node: Runtime error: expecting ';'``) without raising a
    Python exception.  This helper detects those patterns and returns
    a clean error string suitable for ``RuntimeResult.error``.

    Returns:
        The extracted error message, or ``""`` if no error pattern found.
    """
    match = _STDERR_ERROR_RE.search(stderr)
    if match:
        return match.group(0).strip()
    # Fallback: if stderr has content and looks like an error
    stripped = stderr.strip()
    if stripped and ("error" in stripped.lower() or "exception" in stripped.lower()):
        return stripped
    return ""
