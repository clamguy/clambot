"""Tests for Phase 7 — Clam Runtime.

Tests:
- Non-JS language rejected with incompatible_language error code
- Secret preflight blocks on missing secret
- ClamErrorPayload has all required fields
- Timeout produces runtime_timeout_unresponsive error code
- CompatibilityChecker accepts JavaScript
- RunLogBuilder records and summarizes events
- RuntimePolicy resolution from metadata and config
- Error detail context builder
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from clambot.agent.compatibility import CompatibilityChecker
from clambot.agent.error_detail_context import build_error_detail_context
from clambot.agent.errors import (
    ALL_ERROR_CODES,
    INCOMPATIBLE_LANGUAGE,
    PRE_RUNTIME_SECRET_REQUIREMENTS_UNRESOLVED,
    RUNTIME_EXECUTION_ERROR,
    RUNTIME_TIMEOUT_UNRESPONSIVE,
    ClamErrorPayload,
    ClamErrorStage,
)
from clambot.agent.progress import ProgressState
from clambot.agent.run_log import RunLogBuilder
from clambot.agent.runtime import ClamRuntime
from clambot.agent.runtime_backend_amla_sandbox import (
    AmlaSandboxRuntimeBackend,
    RuntimeResult,
    _inject_inputs,
)
from clambot.agent.runtime_policy import RuntimePolicy, resolve_runtime_policy
from clambot.agent.secret_preflight import resolve_pre_runtime_secret_requirements

# ═══════════════════════════════════════════════════════════════════════
# ClamErrorPayload
# ═══════════════════════════════════════════════════════════════════════


class TestClamErrorPayload:
    """ClamErrorPayload structure and serialization tests."""

    def test_has_all_required_fields(self):
        """ClamErrorPayload has code, stage, message, detail, user_message."""
        error = ClamErrorPayload(
            code=RUNTIME_EXECUTION_ERROR,
            stage=ClamErrorStage.RUNTIME,
            message="Test error",
            detail={"key": "value"},
            user_message="Something went wrong.",
        )
        assert error.code == RUNTIME_EXECUTION_ERROR
        assert error.stage == ClamErrorStage.RUNTIME
        assert error.message == "Test error"
        assert error.detail == {"key": "value"}
        assert error.user_message == "Something went wrong."

    def test_frozen_immutable(self):
        """ClamErrorPayload is frozen (immutable)."""
        error = ClamErrorPayload(
            code=RUNTIME_EXECUTION_ERROR,
            stage=ClamErrorStage.RUNTIME,
            message="Test",
        )
        with pytest.raises(AttributeError):
            error.code = "new_code"  # type: ignore[misc]

    def test_to_dict_serialization(self):
        """to_dict produces correct dict structure."""
        error = ClamErrorPayload(
            code=INCOMPATIBLE_LANGUAGE,
            stage=ClamErrorStage.COMPATIBILITY,
            message="Bad language",
            detail={"language": "python"},
            user_message="Only JS supported",
        )
        d = error.to_dict()
        assert d["code"] == INCOMPATIBLE_LANGUAGE
        assert d["stage"] == "COMPATIBILITY"
        assert d["message"] == "Bad language"
        assert d["detail"]["language"] == "python"
        assert d["user_message"] == "Only JS supported"

    def test_default_detail_and_user_message(self):
        """Default detail is empty dict, user_message is empty string."""
        error = ClamErrorPayload(
            code="test",
            stage=ClamErrorStage.RUNTIME,
            message="msg",
        )
        assert error.detail == {}
        assert error.user_message == ""

    def test_all_error_codes_are_strings(self):
        """All error code constants are non-empty strings."""
        for code in ALL_ERROR_CODES:
            assert isinstance(code, str)
            assert len(code) > 0

    def test_error_stages_enum(self):
        """ClamErrorStage has expected values."""
        assert ClamErrorStage.PRE_RUNTIME.value == "PRE_RUNTIME"
        assert ClamErrorStage.COMPATIBILITY.value == "COMPATIBILITY"
        assert ClamErrorStage.RUNTIME.value == "RUNTIME"


# ═══════════════════════════════════════════════════════════════════════
# CompatibilityChecker
# ═══════════════════════════════════════════════════════════════════════


class TestCompatibilityChecker:
    """CompatibilityChecker language validation tests."""

    def setup_method(self):
        self.checker = CompatibilityChecker()

    def test_javascript_accepted(self):
        """'javascript' language passes compatibility."""
        result = self.checker.check({"language": "javascript"})
        assert result is None

    def test_js_shorthand_accepted(self):
        """'js' shorthand passes compatibility."""
        result = self.checker.check({"language": "js"})
        assert result is None

    def test_javascript_case_insensitive(self):
        """Language check is case-insensitive."""
        result = self.checker.check({"language": "JavaScript"})
        assert result is None
        result = self.checker.check({"language": "JS"})
        assert result is None

    def test_python_rejected(self):
        """Python language is rejected with incompatible_language code."""
        result = self.checker.check({"language": "python"})
        assert result is not None
        assert result.code == INCOMPATIBLE_LANGUAGE
        assert result.stage == ClamErrorStage.COMPATIBILITY
        assert "python" in result.message.lower()

    def test_empty_language_rejected(self):
        """Empty language string is rejected."""
        result = self.checker.check({"language": ""})
        assert result is not None
        assert result.code == INCOMPATIBLE_LANGUAGE

    def test_shell_rejected(self):
        """Shell language is rejected."""
        result = self.checker.check({"language": "shell"})
        assert result is not None
        assert result.code == INCOMPATIBLE_LANGUAGE

    def test_object_with_attribute(self):
        """Checker works with objects that have a language attribute."""
        clam = MagicMock()
        clam.language = "javascript"
        clam.metadata = {}
        result = self.checker.check(clam)
        assert result is None

    def test_object_with_metadata_language(self):
        """Checker falls back to metadata.language."""
        clam = MagicMock(spec=[])  # No language attribute
        clam.metadata = {"language": "javascript"}
        # Need to make getattr work correctly
        result = self.checker.check({"metadata": {"language": "javascript"}})
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# Secret Preflight
# ═══════════════════════════════════════════════════════════════════════


class TestSecretPreflight:
    """Secret preflight resolution tests."""

    def test_no_requirements_passes(self):
        """Clam with no secret_requirements passes preflight."""
        clam = {"metadata": {}}
        store = MagicMock()
        result = resolve_pre_runtime_secret_requirements(clam, store)
        assert result is None

    def test_all_secrets_available(self):
        """Clam with all secrets available passes preflight."""
        clam = {"metadata": {"secret_requirements": ["API_KEY", "TOKEN"]}}
        store = MagicMock()
        store.get.side_effect = lambda name: f"value_{name}"
        result = resolve_pre_runtime_secret_requirements(clam, store)
        assert result is None

    def test_missing_secret_blocks(self):
        """Missing secret returns error with correct code."""
        clam = {"metadata": {"secret_requirements": ["API_KEY", "MISSING_SECRET"]}}
        store = MagicMock()
        store.get.side_effect = lambda name: "value" if name == "API_KEY" else None
        result = resolve_pre_runtime_secret_requirements(clam, store)
        assert result is not None
        assert result.code == PRE_RUNTIME_SECRET_REQUIREMENTS_UNRESOLVED
        assert result.stage == ClamErrorStage.PRE_RUNTIME
        assert "MISSING_SECRET" in result.message

    def test_missing_secret_detail_lists_missing(self):
        """Error detail includes list of missing secrets."""
        clam = {"metadata": {"secret_requirements": ["A", "B", "C"]}}
        store = MagicMock()
        store.get.return_value = None
        result = resolve_pre_runtime_secret_requirements(clam, store)
        assert result is not None
        assert set(result.detail["missing_secrets"]) == {"A", "B", "C"}

    def test_empty_requirements_list(self):
        """Empty requirements list passes preflight."""
        clam = {"metadata": {"secret_requirements": []}}
        store = MagicMock()
        result = resolve_pre_runtime_secret_requirements(clam, store)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# ProgressState
# ═══════════════════════════════════════════════════════════════════════


class TestProgressState:
    """ProgressState enum tests."""

    def test_all_states_exist(self):
        """All expected progress states exist."""
        states = [
            ProgressState.DISCOVERING,
            ProgressState.GENERATING,
            ProgressState.VALIDATING,
            ProgressState.EXECUTING,
            ProgressState.WAITING_APPROVAL,
            ProgressState.COMPLETED,
            ProgressState.FAILED,
        ]
        assert len(states) == 7

    def test_states_are_strings(self):
        """Progress states are string enum values."""
        for state in ProgressState:
            assert isinstance(state.value, str)
            assert state.value == state.name


# ═══════════════════════════════════════════════════════════════════════
# RunLogBuilder
# ═══════════════════════════════════════════════════════════════════════


class TestRunLogBuilder:
    """RunLogBuilder event recording and summary tests."""

    def test_append_events(self):
        """Events are appended in order."""
        builder = RunLogBuilder(run_id="test-run")
        builder.append("start", {"msg": "hello"})
        builder.append("end", {"msg": "bye"})
        assert len(builder.events) == 2
        assert builder.events[0].type == "start"
        assert builder.events[1].type == "end"

    def test_append_tool_call(self):
        """Tool call events are recorded."""
        builder = RunLogBuilder()
        builder.append_tool_call("fs", {"operation": "read", "path": "/test"})
        assert len(builder.events) == 1
        assert builder.events[0].type == "tool_call"
        assert builder.events[0].data["tool_name"] == "fs"

    def test_append_tool_result(self):
        """Tool result events are recorded with truncation."""
        builder = RunLogBuilder()
        builder.append_tool_result("fs", "x" * 3000)
        assert len(builder.events) == 1
        assert builder.events[0].type == "tool_result"
        assert len(builder.events[0].data["result"]) <= 2000

    def test_append_error(self):
        """Error events are recorded."""
        builder = RunLogBuilder()
        builder.append_error("runtime_error", "Something broke")
        assert builder.events[0].type == "error"
        assert builder.events[0].data["code"] == "runtime_error"

    def test_summary_structure(self):
        """Summary has run_id, duration_ms, event_count, events."""
        builder = RunLogBuilder(run_id="test-123")
        builder.append("test")
        summary = builder.summary()
        assert summary["run_id"] == "test-123"
        assert "duration_ms" in summary
        assert summary["event_count"] == 1
        assert len(summary["events"]) == 1

    def test_run_id_property(self):
        """run_id property returns the configured run ID."""
        builder = RunLogBuilder(run_id="my-run")
        assert builder.run_id == "my-run"


# ═══════════════════════════════════════════════════════════════════════
# RuntimePolicy
# ═══════════════════════════════════════════════════════════════════════


class TestRuntimePolicy:
    """RuntimePolicy resolution tests."""

    def test_default_policy(self):
        """Default policy has sensible defaults."""
        from dataclasses import fields

        policy = RuntimePolicy()
        defaults = {f.name: f.default for f in fields(RuntimePolicy)}
        assert policy.timeout_seconds == defaults["timeout_seconds"]
        assert policy.max_tool_iterations == defaults["max_tool_iterations"]
        assert policy.stdin_threshold_bytes == defaults["stdin_threshold_bytes"]

    def test_resolve_from_metadata(self):
        """Metadata overrides are applied."""
        metadata = {"runtime": {"timeout_seconds": 120, "max_tool_iterations": 10}}
        policy = resolve_runtime_policy(metadata)
        assert policy.timeout_seconds == 120
        assert policy.max_tool_iterations == 10

    def test_resolve_with_config_defaults(self):
        """Config defaults are applied when metadata doesn't override."""
        config = MagicMock()
        config.max_tool_iterations = 30
        policy = resolve_runtime_policy(None, config)
        assert policy.max_tool_iterations == 30

    def test_metadata_overrides_config(self):
        """Metadata takes priority over config defaults."""
        config = MagicMock()
        config.max_tool_iterations = 30
        metadata = {"runtime": {"max_tool_iterations": 5}}
        policy = resolve_runtime_policy(metadata, config)
        assert policy.max_tool_iterations == 5

    def test_empty_metadata(self):
        """Empty metadata returns defaults."""
        policy = resolve_runtime_policy({})
        assert policy.timeout_seconds == 60


# ═══════════════════════════════════════════════════════════════════════
# RuntimeResult
# ═══════════════════════════════════════════════════════════════════════


class TestRuntimeResult:
    """RuntimeResult dataclass tests."""

    def test_default_values(self):
        """RuntimeResult has sensible defaults."""
        result = RuntimeResult()
        assert result.output == ""
        assert result.error == ""
        assert result.tool_calls == []
        assert result.stderr == ""
        assert result.timed_out is False

    def test_with_values(self):
        """RuntimeResult stores values correctly."""
        result = RuntimeResult(
            output="hello",
            error="",
            tool_calls=[{"name": "fs", "args": {}, "result": "ok"}],
            timed_out=False,
        )
        assert result.output == "hello"
        assert len(result.tool_calls) == 1


# ═══════════════════════════════════════════════════════════════════════
# AmlaSandboxRuntimeBackend
# ═══════════════════════════════════════════════════════════════════════


class TestAmlaSandboxRuntimeBackend:
    """AmlaSandboxRuntimeBackend tests."""

    def test_execute_simple_script(self):
        """Execute a simple JS script that returns output."""
        backend = AmlaSandboxRuntimeBackend()
        result = backend.execute("return 'hello world';")
        assert result.output == "hello world"
        assert result.error == ""
        assert result.timed_out is False

    def test_execute_with_console_log(self):
        """Execute a script using console.log."""
        backend = AmlaSandboxRuntimeBackend()
        result = backend.execute("console.log('test output');")
        assert "test output" in result.output

    def test_execute_with_tool_handler(self):
        """Tool handler is called when script invokes a tool."""
        calls = []

        def handler(method, params):
            calls.append({"method": method, "params": params})
            return {"result": "ok"}

        backend = AmlaSandboxRuntimeBackend(tool_handler=handler)
        tool_schemas = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo a message",
                    "parameters": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                    },
                },
            }
        ]
        result = backend.execute(
            'const r = await echo({message: "hi"}); return JSON.stringify(r);',
            declared_tools=tool_schemas,
        )
        assert len(calls) == 1
        assert calls[0]["method"] == "echo"
        assert calls[0]["params"]["message"] == "hi"

    def test_execute_runtime_error(self):
        """Script runtime errors are captured."""
        backend = AmlaSandboxRuntimeBackend()
        result = backend.execute("throw new Error('boom');")
        # Should have an error or non-empty stderr
        assert result.error or result.stderr

    def test_tool_definitions_built_from_schemas(self):
        """Tool schemas are converted to ToolDefinitions."""
        defs = AmlaSandboxRuntimeBackend._build_tool_definitions(
            [
                {
                    "function": {
                        "name": "my_tool",
                        "description": "A test tool",
                        "parameters": {"type": "object"},
                    }
                }
            ]
        )
        assert len(defs) == 1
        assert defs[0].name == "my_tool"
        assert defs[0].description == "A test tool"

    def test_empty_tool_schemas(self):
        """Empty tool list produces empty definitions."""
        defs = AmlaSandboxRuntimeBackend._build_tool_definitions([])
        assert len(defs) == 0

    def test_result_contains_run_log(self):
        """Result contains run_log summary."""
        backend = AmlaSandboxRuntimeBackend()
        result = backend.execute("return 42;")
        assert "events" in result.run_log or "event_count" in result.run_log


# ═══════════════════════════════════════════════════════════════════════
# ClamRuntime
# ═══════════════════════════════════════════════════════════════════════


class TestClamRuntime:
    """ClamRuntime integration tests."""

    @pytest.mark.asyncio
    async def test_incompatible_language_rejected(self):
        """Non-JS language is rejected at pre-flight."""
        backend = AmlaSandboxRuntimeBackend()
        runtime = ClamRuntime(backend=backend)

        clam = {
            "script": "print('hello')",
            "language": "python",
            "declared_tools": [],
            "metadata": {},
        }
        result = await runtime.execute(clam)
        assert result.error
        assert "python" in result.error.lower() or "supported" in result.error.lower()

    @pytest.mark.asyncio
    async def test_javascript_accepted(self):
        """JavaScript clams execute successfully."""
        backend = AmlaSandboxRuntimeBackend()
        runtime = ClamRuntime(backend=backend)

        clam = {
            "script": "return 'hello from clam';",
            "language": "javascript",
            "declared_tools": [],
            "metadata": {},
        }
        result = await runtime.execute(clam)
        assert result.output == "hello from clam"
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_secret_preflight_blocks_on_missing(self):
        """Missing required secret blocks execution."""
        backend = AmlaSandboxRuntimeBackend()
        runtime = ClamRuntime(backend=backend)

        secret_store = MagicMock()
        secret_store.get.return_value = None

        clam = {
            "script": "return 'ok';",
            "language": "javascript",
            "declared_tools": [],
            "metadata": {"secret_requirements": ["MY_API_KEY"]},
        }
        result = await runtime.execute(clam, secret_store=secret_store)
        assert result.error
        assert "MY_API_KEY" in result.error

    @pytest.mark.asyncio
    async def test_timeout_produces_correct_error(self):
        """Timeout produces runtime_timeout_unresponsive error."""
        backend = AmlaSandboxRuntimeBackend()
        runtime = ClamRuntime(backend=backend)

        # Use an infinite loop script with a very short timeout
        clam = {
            "script": "while(true) {}",
            "language": "javascript",
            "declared_tools": [],
            "metadata": {"runtime": {"timeout_seconds": 2}},
        }
        result = await runtime.execute(clam)
        assert result.timed_out is True
        assert result.error
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_tool_dispatch_through_registry(self):
        """Tool calls from sandbox go through tool registry."""
        tool_registry = MagicMock()
        tool_registry.dispatch.return_value = {"data": "from_registry"}
        tool_registry.get_schemas.return_value = [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "Test",
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                    },
                },
            }
        ]
        tool_registry.get_tool.return_value = MagicMock(
            to_schema=lambda: tool_registry.get_schemas()[0]
        )

        backend = AmlaSandboxRuntimeBackend()
        runtime = ClamRuntime(
            backend=backend,
            tool_registry=tool_registry,
        )

        clam = {
            "script": 'const r = await test_tool({x: "hello"}); return JSON.stringify(r);',
            "language": "javascript",
            "declared_tools": ["test_tool"],
            "metadata": {},
        }
        result = await runtime.execute(clam)
        assert tool_registry.dispatch.called

    @pytest.mark.asyncio
    async def test_progress_events_emitted(self):
        """Progress events are emitted during execution."""
        backend = AmlaSandboxRuntimeBackend()
        runtime = ClamRuntime(backend=backend)

        events = []
        clam = {
            "script": "return 'ok';",
            "language": "javascript",
            "declared_tools": [],
            "metadata": {},
        }
        result = await runtime.execute(clam, on_event=lambda e: events.append(e))
        progress_events = [e for e in events if e.get("type") == "progress"]
        assert len(progress_events) >= 2  # VALIDATING + EXECUTING + COMPLETED/FAILED


# ═══════════════════════════════════════════════════════════════════════
# Error Detail Context
# ═══════════════════════════════════════════════════════════════════════


class TestErrorDetailContext:
    """Error detail context builder tests."""

    def test_with_error_payload(self):
        """Context includes error code, stage, message."""
        error = ClamErrorPayload(
            code=RUNTIME_EXECUTION_ERROR,
            stage=ClamErrorStage.RUNTIME,
            message="Script crashed",
            detail={"line": 42},
        )
        ctx = build_error_detail_context(error)
        assert RUNTIME_EXECUTION_ERROR in ctx
        assert "RUNTIME" in ctx
        assert "Script crashed" in ctx

    def test_with_result(self):
        """Context includes stderr and output from result."""
        result = RuntimeResult(
            output="partial output",
            error="exec error",
            stderr="TypeError: undefined is not a function",
        )
        ctx = build_error_detail_context(None, result=result)
        assert "TypeError" in ctx
        assert "partial output" in ctx

    def test_with_clam_script(self):
        """Context includes truncated script."""
        clam = {"script": "return 'hello world';"}
        ctx = build_error_detail_context(None, clam=clam)
        assert "hello world" in ctx

    def test_empty_context(self):
        """No error details returns fallback message."""
        ctx = build_error_detail_context(None)
        assert "No error details" in ctx

    def test_full_context(self):
        """Full context with all components."""
        error = ClamErrorPayload(
            code=RUNTIME_TIMEOUT_UNRESPONSIVE,
            stage=ClamErrorStage.RUNTIME,
            message="Timed out",
        )
        clam = {"script": "while(true){}"}
        result = RuntimeResult(stderr="infinite loop detected")
        ctx = build_error_detail_context(error, clam=clam, result=result)
        assert RUNTIME_TIMEOUT_UNRESPONSIVE in ctx
        assert "while(true)" in ctx
        assert "infinite loop" in ctx


# ═══════════════════════════════════════════════════════════════════════
# Input Injection
# ═══════════════════════════════════════════════════════════════════════


class TestInputInjection:
    """Tests for _inject_inputs() and the inputs= parameter on execute()."""

    # ------------------------------------------------------------------
    # _inject_inputs unit tests
    # ------------------------------------------------------------------

    def test_inject_inputs_none(self):
        """_inject_inputs with None inputs returns the script unchanged."""
        script = "return 1;"
        result = _inject_inputs(script, None)
        assert result == script

    def test_inject_inputs_empty(self):
        """_inject_inputs with an empty dict returns the script unchanged."""
        script = "return 1;"
        result = _inject_inputs(script, {})
        assert result == script

    def test_inject_inputs_prepends_args(self):
        """_inject_inputs prepends a const args binding for non-empty inputs."""
        script = "return args.x;"
        result = _inject_inputs(script, {"x": 42})
        assert result.startswith("const args")
        assert '"x": 42' in result or '"x":42' in result
        assert "return args.x;" in result

    def test_inject_inputs_with_run_function(self):
        """When the script defines async function run(args), a trailing
        'return await run(args);' call is appended."""
        script = "async function run(args) { return args.text; }"
        result = _inject_inputs(script, {"text": "hello"})
        assert result.startswith("const args")
        assert "return await run(args);" in result

    def test_inject_inputs_no_run_function(self):
        """When the script has no run() entry point, no trailing call is appended."""
        script = "return args.text.toUpperCase();"
        result = _inject_inputs(script, {"text": "hello"})
        assert result.startswith("const args")
        assert "return await run(args);" not in result

    # ------------------------------------------------------------------
    # Integration test — real sandbox execution with inputs=
    # ------------------------------------------------------------------

    def test_backend_execute_passes_inputs_to_sandbox(self):
        """execute() with inputs= injects args and the sandbox returns the
        correct computed value."""
        backend = AmlaSandboxRuntimeBackend()
        result = backend.execute(
            script="return String(args.x + args.y);",
            inputs={"x": 2, "y": 3},
        )
        assert result.error == "", f"Unexpected error: {result.error}"
        assert result.output == "5"

    def test_inject_inputs_run_function_no_inputs(self):
        """When run() is detected but inputs is None, _inject_inputs prepends
        'const args = {};' and appends 'return await run(args);'."""
        script = 'async function run() { return "hello"; }'
        result = _inject_inputs(script, None)
        assert result.startswith("const args = {};"), (
            f"Expected preamble 'const args = {{}};', got: {result!r}"
        )
        assert "return await run(args);" in result, f"Expected trailing call, got: {result!r}"

    def test_inject_inputs_run_function_empty_inputs(self):
        """When run() is detected but inputs is {}, _inject_inputs prepends
        'const args = {};' and appends 'return await run(args);'."""
        script = 'async function run() { return "hello"; }'
        result = _inject_inputs(script, {})
        assert result.startswith("const args = {};"), (
            f"Expected preamble 'const args = {{}};', got: {result!r}"
        )
        assert "return await run(args);" in result, f"Expected trailing call, got: {result!r}"

    def test_backend_execute_run_function_auto_called(self):
        """execute() with a script that only defines run() (no explicit call)
        and inputs=None should auto-call run() and return its value."""
        backend = AmlaSandboxRuntimeBackend()
        result = backend.execute(
            script='async function run() { return "auto-called"; }',
            inputs=None,
        )
        assert result.error == "", f"Unexpected error: {result.error}"
        assert result.output == "auto-called", f"Expected 'auto-called', got: {result.output!r}"

    def test_inject_inputs_no_duplicate_run_call(self):
        """When a script already contains 'return await run(args);',
        _inject_inputs must NOT append another one — exactly one occurrence."""
        script = "async function run(args) { return args.x; }\nreturn await run(args);"
        result = _inject_inputs(script, {"x": 1})
        count = result.count("return await run(args);")
        assert count == 1, (
            f"Expected exactly one 'return await run(args);', found {count}.\nResult:\n{result!r}"
        )

    def test_inject_inputs_await_run_already_present(self):
        """When a script already contains 'await run(args);' (without 'return'),
        _inject_inputs must NOT append another run call."""
        script = "async function run(args) { return args.x; }\nawait run(args);"
        result = _inject_inputs(script, {"x": 1})
        # The regex _RUN_CALL_RE matches both 'await run(args);' and
        # 'return await run(args);', so no extra call should be appended.
        import re as _re

        run_call_re = _re.compile(r"(?:return\s+)?await\s+run\s*\(\s*args\s*\)\s*;")
        matches = run_call_re.findall(result)
        assert len(matches) == 1, (
            f"Expected exactly one run call, found {len(matches)}.\nResult:\n{result!r}"
        )
