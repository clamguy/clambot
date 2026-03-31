"""Clam runtime — orchestrates pre-flight checks, WASM execution, and timeout.

The ``ClamRuntime`` ties together:
- Pre-flight: secret check, compatibility check, capability policy
- Execution: WASM sandbox in a daemon thread
- Timeout: watchdog with grace period
- Tool dispatch: approval gate + tool registry
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import uuid
from collections.abc import Callable
from typing import Any

from .compatibility import CompatibilityChecker
from .errors import (
    RUNTIME_EXECUTION_ERROR,
    RUNTIME_TIMEOUT_UNRESPONSIVE,
    ClamErrorPayload,
    ClamErrorStage,
)
from .progress import ProgressState
from .runtime_backend_amla_sandbox import AmlaSandboxRuntimeBackend, RuntimeResult
from .runtime_policy import resolve_runtime_policy
from .secret_preflight import resolve_pre_runtime_secret_requirements


class ClamRuntime:
    """Orchestrates the full lifecycle of a clam execution.

    Pre-flight:
        1. Secret requirements check
        2. Language compatibility check
        3. Capability policy evaluation (if declared)

    Execution:
        - Runs the WASM sandbox in a daemon thread
        - Tool calls are routed through the approval gate and tool registry
        - ``asyncio.Event`` bridges thread → event loop signaling

    Timeout:
        - ``asyncio.wait_for`` on the done event
        - On timeout: signals cancellation → grace period → abort
    """

    def __init__(
        self,
        backend: AmlaSandboxRuntimeBackend,
        approval_gate: Any | None = None,
        tool_registry: Any | None = None,
        config: Any | None = None,
    ) -> None:
        self._backend = backend
        self._approval_gate = approval_gate
        self._tool_registry = tool_registry
        self._config = config
        self._compatibility = CompatibilityChecker()

    def begin_turn(self) -> None:
        """Reset turn-scoped approval grants.

        Call at the start of each ``process_turn`` so that approvals
        from the previous user request do not leak into the next one.
        """
        if self._approval_gate is not None and hasattr(self._approval_gate, "begin_turn"):
            self._approval_gate.begin_turn()

    async def execute(
        self,
        clam: Any,
        inputs: dict[str, Any] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        one_time_approval_grants: list[dict] | None = None,
        secret_store: Any | None = None,
    ) -> RuntimeResult:
        """Execute a clam through the full pipeline.

        Args:
            clam: Clam object/dict with ``script``, ``language``,
                  ``declared_tools``, ``metadata``.
            inputs: Optional input arguments for the clam's ``run(args)`` function.
            on_event: Optional callback for progress and tool events.
            one_time_approval_grants: Grants to register for this execution.
            secret_store: SecretStore for pre-flight secret checking.

        Returns:
            ``RuntimeResult`` with output, errors, and execution metadata.
        """
        run_id = str(uuid.uuid4())

        # Extract clam fields
        script = self._get_clam_field(clam, "script", "")
        language = self._get_clam_field(clam, "language", "javascript")
        declared_tools = self._get_clam_field(clam, "declared_tools", [])
        metadata = self._get_clam_field(clam, "metadata", {})

        # Resolve runtime policy
        config_defaults = getattr(self._config, "agents", None)
        if config_defaults:
            config_defaults = getattr(config_defaults, "defaults", None)
        policy = resolve_runtime_policy(metadata, config_defaults)

        # ── Pre-flight checks ──────────────────────────────────────

        self._emit_event(on_event, "progress", {"state": ProgressState.VALIDATING.value})

        # 1. Secret requirements
        if secret_store is not None:
            error = resolve_pre_runtime_secret_requirements(clam, secret_store)
            if error is not None:
                return RuntimeResult(
                    error=error.message,
                    run_log={"error": error.to_dict()},
                )

        # 2. Compatibility check
        # Build a temp object with language for the checker
        compat_obj = {"language": language}
        error = self._compatibility.check(compat_obj)
        if error is not None:
            return RuntimeResult(
                error=error.message,
                run_log={"error": error.to_dict()},
            )

        # 3. Register one-time approval grants
        if one_time_approval_grants and self._approval_gate:
            self._approval_gate.register_one_time_grants(run_id, one_time_approval_grants)

        # ── Execution ──────────────────────────────────────────────

        self._emit_event(on_event, "progress", {"state": ProgressState.EXECUTING.value})

        # Capture the event loop for thread→async bridging (approvals)
        event_loop = asyncio.get_event_loop()

        # Shared flag: set by tool handler when waiting for user approval
        approval_pending_flag = threading.Event()

        # Build tool handler that goes through approval gate + registry
        def tool_handler(method: str, params: dict[str, Any]) -> Any:
            return self._handle_tool_call(
                method,
                params,
                run_id,
                on_event,
                event_loop,
                approval_pending_flag,
            )

        # Build tool schemas for sandbox from declared_tools
        tool_schemas = self._resolve_tool_schemas(declared_tools)

        # Create a backend with our handler
        backend = AmlaSandboxRuntimeBackend(
            tool_handler=tool_handler,
            on_event=on_event,
        )

        # Run in daemon thread with timeout
        loop = asyncio.get_event_loop()
        done_event = asyncio.Event()
        result_holder: list[RuntimeResult] = []
        error_holder: list[str] = []

        def run_in_thread() -> None:
            try:
                result = backend.execute(
                    script=script,
                    declared_tools=tool_schemas,
                    inputs=inputs,
                )
                result_holder.append(result)
            except Exception as exc:
                error_holder.append(str(exc))
            finally:
                loop.call_soon_threadsafe(done_event.set)

        # Copy the current context (including bus.context vars like
        # current_channel / current_chat_id) so tools running in the
        # daemon thread can read them.
        ctx = contextvars.copy_context()
        thread = threading.Thread(
            target=lambda: ctx.run(run_in_thread),
            daemon=True,
        )
        thread.start()

        # Wait with timeout — extend if a human approval is pending
        approval_timeout = 300  # 5 min for human interaction
        try:
            await asyncio.wait_for(
                done_event.wait(),
                timeout=policy.timeout_seconds,
            )
        except TimeoutError:
            if approval_pending_flag.is_set():
                # Approval is pending — extend wait for human interaction
                try:
                    await asyncio.wait_for(
                        done_event.wait(),
                        timeout=approval_timeout,
                    )
                except TimeoutError:
                    pass  # fall through to timeout handling below

        if not done_event.is_set():
            # Truly timed out — abort
            backend.abort()

            # Grace period (1 second)
            try:
                await asyncio.wait_for(done_event.wait(), timeout=1.0)
            except TimeoutError:
                pass

            self._emit_event(on_event, "progress", {"state": ProgressState.FAILED.value})

            return RuntimeResult(
                error=f"Execution timed out after {policy.timeout_seconds}s",
                timed_out=True,
                run_log={
                    "error": ClamErrorPayload(
                        code=RUNTIME_TIMEOUT_UNRESPONSIVE,
                        stage=ClamErrorStage.RUNTIME,
                        message=f"Execution timed out after {policy.timeout_seconds}s",
                    ).to_dict()
                },
            )

        # Collect result
        if result_holder:
            result = result_holder[0]
        elif error_holder:
            result = RuntimeResult(
                error=error_holder[0],
                run_log={
                    "error": ClamErrorPayload(
                        code=RUNTIME_EXECUTION_ERROR,
                        stage=ClamErrorStage.RUNTIME,
                        message=error_holder[0],
                    ).to_dict()
                },
            )
        else:
            result = RuntimeResult(error="Unknown execution error")

        # Emit completion
        state = ProgressState.COMPLETED if not result.error else ProgressState.FAILED
        self._emit_event(on_event, "progress", {"state": state.value})

        return result

    def _handle_tool_call(
        self,
        method: str,
        params: dict[str, Any],
        run_id: str,
        on_event: Callable[[dict[str, Any]], None] | None,
        event_loop: asyncio.AbstractEventLoop | None = None,
        approval_pending_flag: threading.Event | None = None,
    ) -> Any:
        """Handle a tool call from the sandbox.

        Routes through approval gate then dispatches via tool registry.
        When the gate returns AWAITING, emits an ``approval_pending``
        event and blocks (from the sandbox thread) until the approval
        is resolved externally via *event_loop*.

        .. note:: This method runs in the daemon thread, so all event
           emissions must go through ``call_soon_threadsafe`` to avoid
           corrupting the ``asyncio.Queue`` in the event bus.
        """

        def _thread_safe_emit(event_type: str, data: dict[str, Any]) -> None:
            """Emit an event safely from the daemon thread."""
            if on_event is None:
                return
            payload = {"type": event_type, **data}
            if event_loop is not None and event_loop.is_running():
                event_loop.call_soon_threadsafe(on_event, payload)
            else:
                on_event(payload)

        _thread_safe_emit("tool_call", {"tool_name": method, "args": params})

        # Normalize args for approval fingerprinting / scope matching.
        # The tool may resolve relative paths to absolute form so that
        # file:/dir: scope grants match correctly.
        normalized_params = params
        if self._tool_registry is not None:
            tool_obj = self._tool_registry.get_tool(method)
            if tool_obj is not None and hasattr(tool_obj, "normalize_args_for_approval"):
                try:
                    normalized_params = tool_obj.normalize_args_for_approval(params)
                except Exception:  # noqa: BLE001
                    normalized_params = params

        # Check approval gate
        if self._approval_gate is not None:
            from .approval_gate import ApprovalDecision

            approval_result = self._approval_gate.evaluate_request(
                method, normalized_params, run_id
            )

            if approval_result.decision == ApprovalDecision.DENY:
                return {"error": f"Tool call '{method}' denied by approval gate"}

            if approval_result.decision == ApprovalDecision.AWAITING:
                # Build approval options from the tool
                options: list[dict[str, str]] = []
                if self._tool_registry is not None:
                    tool = self._tool_registry.get_tool(method)
                    if tool is not None:
                        for opt in tool.get_approval_options(normalized_params):
                            options.append(
                                {
                                    "id": opt.id,
                                    "label": opt.label,
                                    "scope": opt.scope,
                                }
                            )

                # Emit approval_pending event → orchestrator stores
                # the request and sends buttons to the channel
                _thread_safe_emit(
                    "approval_pending",
                    {
                        "approval_id": approval_result.approval_id,
                        "tool_name": method,
                        "args": params,
                        "options": options,
                    },
                )

                # Block this thread until the approval is resolved.
                # The event loop in the main thread is still running
                # (waiting on done_event) so it can process the
                # resolution callback from the orchestrator.
                # Signal the main thread to extend its timeout.
                import concurrent.futures

                if approval_pending_flag is not None:
                    approval_pending_flag.set()

                if event_loop is not None and event_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self._approval_gate.wait_for_resolution(
                            approval_result.approval_id,
                        ),
                        event_loop,
                    )
                    try:
                        record = future.result(timeout=300)  # 5 min
                    except (concurrent.futures.TimeoutError, Exception):
                        return {"error": f"Approval timed out for '{method}'"}
                    finally:
                        if approval_pending_flag is not None:
                            approval_pending_flag.clear()

                    if record is not None and record.decision == "ALLOW":
                        return self._dispatch_tool(method, params)
                    return {"error": f"Tool call '{method}' denied by approval gate"}

                # No event loop — fall back to immediate error
                return {"error": f"Tool call '{method}' awaiting approval", "awaiting": True}

        # Dispatch via tool registry
        return self._dispatch_tool(method, params)

    def _dispatch_tool(self, method: str, params: dict[str, Any]) -> Any:
        """Dispatch a tool call via the registry."""
        if self._tool_registry is not None:
            try:
                return self._tool_registry.dispatch(method, params)
            except ValueError as exc:
                return {"error": str(exc)}
            except Exception as exc:
                return {"error": f"Tool execution error: {exc}"}
        return {"error": f"No tool registry configured for: {method}"}

    # Tools that are handled by the agent loop itself and must NOT be
    def _resolve_tool_schemas(
        self,
        declared_tools: list[str] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Resolve tool schemas from declared tool names or schema dicts."""
        if not declared_tools:
            if self._tool_registry is not None:
                return list(self._tool_registry.get_schemas())
            return []

        schemas: list[dict[str, Any]] = []
        for tool in declared_tools:
            if isinstance(tool, dict):
                schemas.append(tool)
            elif isinstance(tool, str) and self._tool_registry is not None:
                tool_obj = self._tool_registry.get_tool(tool)
                if tool_obj is not None:
                    schemas.append(tool_obj.to_schema())
        return schemas

    @staticmethod
    def _get_clam_field(clam: Any, field_name: str, default: Any = None) -> Any:
        """Extract a field from a clam object or dict."""
        if isinstance(clam, dict):
            return clam.get(field_name, default)
        return getattr(clam, field_name, default)

    @staticmethod
    def _emit_event(
        on_event: Callable[[dict[str, Any]], None] | None,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Emit an event via the callback if provided."""
        if on_event is not None:
            on_event({"type": event_type, **data})
