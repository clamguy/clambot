"""Integration tests for the approval flow — Phase 14.

End-to-end tests verifying tool call approval gating: blocking, resolution,
and always_grant persistence.

Tests:
- Tool call blocked (no always_grant) → AWAITING → resolve → execution resumes
- always_grant persisted to config → subsequent call immediately ALLOW
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from clambot.agent.approval_gate import ApprovalDecision, ApprovalGate
from clambot.agent.approvals import compute_scope_fingerprint
from clambot.bus.events import InboundMessage
from clambot.bus.queue import MessageBus
from clambot.config.schema import ApprovalsConfig
from clambot.gateway.orchestrator import GatewayOrchestrator
from clambot.session.manager import SessionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace directory structure."""
    ws = tmp_path / "workspace"
    for subdir in ("clams", "build", "sessions", "logs", "docs", "memory"):
        (ws / subdir).mkdir(parents=True, exist_ok=True)
    (ws / "memory" / "MEMORY.md").write_text("", encoding="utf-8")
    (ws / "memory" / "HISTORY.md").write_text("", encoding="utf-8")
    return ws


def _make_config_file(tmp_path: Path, config_data: dict | None = None) -> Path:
    """Write a config.json and return its path."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(config_data or {}, indent=2),
        encoding="utf-8",
    )
    return config_path


def _make_inbound(
    content: str = "hello",
    channel: str = "telegram",
    source: str = "user1",
    chat_id: str = "123",
    metadata: dict | None = None,
) -> InboundMessage:
    """Create an InboundMessage with defaults."""
    return InboundMessage(
        channel=channel,
        source=source,
        chat_id=chat_id,
        content=content,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Test: Tool call blocked → AWAITING → resolve → resumes
# ---------------------------------------------------------------------------


class TestApprovalBlocking:
    """Tool call without matching always_grant is blocked and AWAITING."""

    @pytest.mark.asyncio
    async def test_tool_call_blocked_without_always_grant(self, tmp_path: Path) -> None:
        """A tool call with no matching always_grant returns AWAITING
        when interactive mode is enabled."""
        config_path = _make_config_file(tmp_path)

        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
            config_path=config_path,
        )

        result = gate.evaluate_request(
            tool_name="fs",
            args={"action": "read", "path": "/etc/passwd"},
            run_id="run-1",
        )

        assert result.decision == ApprovalDecision.AWAITING
        assert result.approval_id != ""

    @pytest.mark.asyncio
    async def test_awaiting_resolved_allows_execution(self, tmp_path: Path) -> None:
        """An AWAITING approval can be resolved with ALLOW, unblocking execution."""
        config_path = _make_config_file(tmp_path)

        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
            config_path=config_path,
        )

        # First request → AWAITING
        result = gate.evaluate_request(
            tool_name="http_request",
            args={"url": "https://api.example.com", "method": "GET"},
            run_id="run-1",
        )
        assert result.decision == ApprovalDecision.AWAITING
        approval_id = result.approval_id

        # Resolve the approval
        record = gate.resolve(approval_id, "ALLOW")
        assert record is not None
        assert record.decision == "ALLOW"

    @pytest.mark.asyncio
    async def test_awaiting_resolved_with_always_grant_persists(self, tmp_path: Path) -> None:
        """Resolving with 'always' grant_scope persists for future calls."""
        config_path = _make_config_file(tmp_path, {"agents": {"approvals": {"alwaysGrants": []}}})

        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
            config_path=config_path,
        )

        # First request → AWAITING
        result = gate.evaluate_request(
            tool_name="web_fetch",
            args={"url": "https://example.com"},
            run_id="run-1",
        )
        assert result.decision == ApprovalDecision.AWAITING
        approval_id = result.approval_id

        # Resolve with "always" scope
        record = gate.resolve(approval_id, "ALLOW", grant_scope="always")
        assert record is not None
        assert record.granted_scope == "always"

        # Second identical request → immediate ALLOW (always_grant matched)
        result2 = gate.evaluate_request(
            tool_name="web_fetch",
            args={"url": "https://example.com"},
            run_id="run-2",
        )
        assert result2.decision == ApprovalDecision.ALLOW

    @pytest.mark.asyncio
    async def test_deny_when_interactive_disabled(self, tmp_path: Path) -> None:
        """Without interactive mode, unmatched tool calls are DENIED."""
        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=False,
                allow_always=True,
                always_grants=[],
            ),
        )

        result = gate.evaluate_request(
            tool_name="fs",
            args={"action": "write", "path": "test.txt", "content": "hello"},
            run_id="run-1",
        )

        assert result.decision == ApprovalDecision.DENY

    @pytest.mark.asyncio
    async def test_wait_for_resolution_unblocks_after_resolve(self, tmp_path: Path) -> None:
        """wait_for_resolution coroutine unblocks when resolve() is called."""
        config_path = _make_config_file(tmp_path)

        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
            config_path=config_path,
        )

        result = gate.evaluate_request(
            tool_name="fs",
            args={"action": "read", "path": "notes.txt"},
            run_id="run-1",
        )
        approval_id = result.approval_id

        # Start waiting in background
        wait_task = asyncio.create_task(gate.wait_for_resolution(approval_id))

        # Not resolved yet
        await asyncio.sleep(0.05)
        assert not wait_task.done()

        # Resolve
        gate.resolve(approval_id, "ALLOW")

        # Should now complete
        resolved = await asyncio.wait_for(wait_task, timeout=1.0)
        assert resolved is not None
        assert resolved.decision == "ALLOW"


# ---------------------------------------------------------------------------
# Test: always_grant persisted to config → subsequent call allowed
# ---------------------------------------------------------------------------


class TestAlwaysGrantPersistence:
    """always_grant persisted to config file allows subsequent calls."""

    @pytest.mark.asyncio
    async def test_always_grant_from_config_allows_immediately(self, tmp_path: Path) -> None:
        """Pre-configured always_grants in the config allow calls immediately."""
        tool_name = "fs"
        args = {"action": "read", "path": "readme.txt"}
        fingerprint = compute_scope_fingerprint(tool_name, args)

        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[
                    {
                        "tool": tool_name,
                        "scope": "always",
                        "fingerprint": fingerprint,
                    }
                ],
            ),
        )

        result = gate.evaluate_request(
            tool_name=tool_name,
            args=args,
            run_id="run-1",
        )

        assert result.decision == ApprovalDecision.ALLOW

    @pytest.mark.asyncio
    async def test_always_grant_persisted_to_config_file(self, tmp_path: Path) -> None:
        """After resolving with 'always', the always_grant is written to the config file."""
        config_data = {"agents": {"approvals": {"alwaysGrants": []}}}
        config_path = _make_config_file(tmp_path, config_data)

        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
            config_path=config_path,
        )

        # First request → AWAITING
        result = gate.evaluate_request(
            tool_name="http_request",
            args={"url": "https://api.data.com/v1", "method": "GET"},
            run_id="run-1",
        )
        assert result.decision == ApprovalDecision.AWAITING

        # Resolve with "always" scope
        gate.resolve(result.approval_id, "ALLOW", grant_scope="always")

        # Config file should now contain the always_grant
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        always_grants = raw.get("agents", {}).get("approvals", {}).get("alwaysGrants", [])
        assert len(always_grants) >= 1
        assert any(g["tool"] == "http_request" for g in always_grants)

    @pytest.mark.asyncio
    async def test_always_grant_survives_new_gate_instance(self, tmp_path: Path) -> None:
        """An always_grant persisted to config works with a new ApprovalGate instance."""
        config_data = {"agents": {"approvals": {"alwaysGrants": []}}}
        config_path = _make_config_file(tmp_path, config_data)

        tool_name = "web_fetch"
        args = {"url": "https://news.example.com"}

        # First gate: block and resolve with "always"
        gate1 = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
            config_path=config_path,
        )

        result = gate1.evaluate_request(tool_name, args, run_id="run-1")
        assert result.decision == ApprovalDecision.AWAITING
        gate1.resolve(result.approval_id, "ALLOW", grant_scope="always")

        # Read persisted config
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        persisted_grants = raw.get("agents", {}).get("approvals", {}).get("alwaysGrants", [])

        # Second gate: loaded from persisted config
        gate2 = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=persisted_grants,
            ),
            config_path=config_path,
        )

        result2 = gate2.evaluate_request(tool_name, args, run_id="run-2")
        assert result2.decision == ApprovalDecision.ALLOW


# ---------------------------------------------------------------------------
# Test: Approval flow through orchestrator
# ---------------------------------------------------------------------------


class TestApprovalFlowOrchestrator:
    """Approval flow through the gateway orchestrator."""

    @pytest.mark.asyncio
    async def test_approve_command_resolves_and_cleans_up(self, tmp_path: Path) -> None:
        """The /approve command resolves the gate (unblocking the sandbox
        thread) and cleans up the pending entry.  No re-queue — the
        original execution continues in-flight."""
        workspace = _make_workspace(tmp_path)
        bus = MessageBus()
        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(enabled=True, interactive=True),
        )

        orch = GatewayOrchestrator(
            bus=bus,
            session_manager=SessionManager(workspace),
            approval_gate=gate,
            workspace=workspace,
        )

        # Simulate a pending approval
        original = _make_inbound(content="Read my files")
        approval_id = "test-approval-001"
        orch.store_pending_approval(approval_id, original)

        # Send /approve
        approve_msg = _make_inbound(
            content="/approve",
            metadata={"approval_id": approval_id, "decision": "ALLOW", "grant_scope": "once"},
        )

        result = await orch._process_inbound(approve_msg)

        # No outbound — the original agent turn delivers the result.
        assert result is None

        # Pending cleaned up, no re-queue (execution continues in-flight)
        assert bus.inbound.empty()
        assert approval_id not in orch._pending_approvals

    @pytest.mark.asyncio
    async def test_approval_resume_skips_session_append(self, tmp_path: Path) -> None:
        """When a message has approval_resume=True, the turn_execution
        skips appending duplicate turns to the session."""
        workspace = _make_workspace(tmp_path)
        session_manager = SessionManager(workspace)
        session_key = "test:user1"

        # Pre-populate session with the original turn
        session_manager.append_turn(session_key, "user", "Read my files")

        # Mock agent loop
        mock_agent_loop = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = "Here are your files: a.txt, b.txt"
        mock_result.status = "completed"
        mock_agent_loop.process_turn = AsyncMock(return_value=mock_result)

        # Create resumed inbound (approval_resume=True)
        inbound = InboundMessage(
            channel="test",
            source="user1",
            chat_id="123",
            content="Read my files",
            metadata={"approval_resume": True},
        )

        outbound = await process_turn_with_persistence_and_execution(
            inbound=inbound,
            agent_loop=mock_agent_loop,
            session_manager=session_manager,
            workspace=workspace,
        )

        assert outbound.content == "Here are your files: a.txt, b.txt"

        # Session should NOT have duplicate user turn appended
        turns = session_manager.load_history(session_key)
        user_turns = [t for t in turns if t.role == "user" and t.content == "Read my files"]
        assert len(user_turns) == 1, "approval_resume should skip appending duplicate user turn"

    @pytest.mark.asyncio
    async def test_one_time_grant_consumed_on_use(self, tmp_path: Path) -> None:
        """A one-time grant allows a call once, then reverts to AWAITING."""
        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
        )

        tool_name = "fs"
        args = {"action": "write", "path": "output.txt", "content": "data"}

        fingerprint = compute_scope_fingerprint(tool_name, args)

        # Register a one-time grant
        gate.register_one_time_grants(
            "run-1",
            [
                {
                    "tool": tool_name,
                    "fingerprint": fingerprint,
                }
            ],
        )

        # First call: one-time grant → ALLOW
        result1 = gate.evaluate_request(tool_name, args, run_id="run-1")
        assert result1.decision == ApprovalDecision.ALLOW

        # Second call with same run_id: one-time consumed → AWAITING
        result2 = gate.evaluate_request(tool_name, args, run_id="run-1")
        assert result2.decision == ApprovalDecision.AWAITING


# Import for turn_execution test
from clambot.agent.turn_execution import process_turn_with_persistence_and_execution  # noqa: E402

# ---------------------------------------------------------------------------
# Test: End-to-end fs approval with file: and dir: scopes (Phase 4)
# ---------------------------------------------------------------------------


class TestFsApprovalScopeMatching:
    """End-to-end tests for fs tool approval with file:/dir: scope grants.

    These tests verify the full integration of Phases 1-3:
    - Phase 1: file: and dir: scope matching in _scope_matches_args
    - Phase 2: normalize_args_for_approval resolves relative paths
    - Phase 3: Approval options emit absolute resolved paths
    """

    @pytest.mark.asyncio
    async def test_fs_approval_dir_grant_covers_subsequent_calls(self, tmp_path: Path) -> None:
        """A dir: scope grant covers subsequent calls to different files
        within that directory, without prompting again.

        1. First fs call → AWAITING (no grants)
        2. Resolve with dir:<workspace> scope grant
        3. Second fs call (different file, same workspace) → ALLOW
        """
        workspace = _make_workspace(tmp_path)
        config_path = _make_config_file(tmp_path)
        ws_str = str(workspace.resolve())

        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
            config_path=config_path,
        )

        # First call: read a file in workspace → AWAITING (no grants)
        first_path = str((workspace / "data" / "file1.txt").resolve())
        result1 = gate.evaluate_request(
            tool_name="fs",
            args={"operation": "read", "path": first_path},
            run_id="run-1",
        )
        assert result1.decision == ApprovalDecision.AWAITING

        # Resolve with dir:<workspace> scope grant
        gate.resolve(
            result1.approval_id,
            "ALLOW",
            grant_scope=f"dir:{ws_str}",
        )

        # Second call: DIFFERENT file in SAME workspace → immediate ALLOW
        second_path = str((workspace / "src" / "main.py").resolve())
        result2 = gate.evaluate_request(
            tool_name="fs",
            args={"operation": "write", "path": second_path},
            run_id="run-2",
        )
        assert result2.decision == ApprovalDecision.ALLOW

    @pytest.mark.asyncio
    async def test_fs_approval_file_grant_exact_match(self, tmp_path: Path) -> None:
        """A file: scope grant only matches the exact path.

        1. Grant file:/abs/path/data.txt
        2. Call with same path → ALLOW
        3. Call with different path → AWAITING
        """
        workspace = _make_workspace(tmp_path)
        config_path = _make_config_file(tmp_path)

        # Resolve a specific file path
        target_path = str((workspace / "data.txt").resolve())

        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[
                    {
                        "tool": "fs",
                        "scope": f"file:{target_path}",
                    }
                ],
            ),
            config_path=config_path,
        )

        # Call with the SAME path → immediate ALLOW
        result1 = gate.evaluate_request(
            tool_name="fs",
            args={"operation": "read", "path": target_path},
            run_id="run-1",
        )
        assert result1.decision == ApprovalDecision.ALLOW

        # Call with a DIFFERENT path → AWAITING
        other_path = str((workspace / "other.txt").resolve())
        result2 = gate.evaluate_request(
            tool_name="fs",
            args={"operation": "read", "path": other_path},
            run_id="run-2",
        )
        assert result2.decision == ApprovalDecision.AWAITING


# ---------------------------------------------------------------------------
# Test: Turn-scoped approval grants (carry across tool calls & retries)
# ---------------------------------------------------------------------------


class TestTurnScopedApprovalGrants:
    """Approvals granted during a turn carry across subsequent tool calls
    and self-fix retries within the same user request."""

    @pytest.mark.asyncio
    async def test_allow_carries_to_next_fs_call_same_file(self, tmp_path: Path) -> None:
        """Plain 'Allow' on fs write auto-approves a subsequent fs read
        on the same file within the same turn (inferred file: scope)."""
        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
        )
        gate.begin_turn()

        target = str((tmp_path / "test.txt").resolve())

        # First call: fs write → AWAITING
        r1 = gate.evaluate_request("fs", {"operation": "write", "path": target, "content": "hi"})
        assert r1.decision == ApprovalDecision.AWAITING

        # User clicks plain "Allow" (no scope)
        gate.resolve(r1.approval_id, "ALLOW")

        # Second call: fs read on same file → ALLOW via turn grant
        r2 = gate.evaluate_request("fs", {"operation": "read", "path": target})
        assert r2.decision == ApprovalDecision.ALLOW

    @pytest.mark.asyncio
    async def test_allow_always_carries_within_turn(self, tmp_path: Path) -> None:
        """'Allow Always' with file: scope auto-approves within the same turn
        AND persists to config."""
        config_path = _make_config_file(tmp_path)
        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
            config_path=config_path,
        )
        gate.begin_turn()

        target = str((tmp_path / "data.txt").resolve())

        # First call → AWAITING
        r1 = gate.evaluate_request("fs", {"operation": "write", "path": target, "content": "x"})
        assert r1.decision == ApprovalDecision.AWAITING

        # User clicks "Allow Always: file:<path>"
        gate.resolve(r1.approval_id, "ALLOW", grant_scope=f"file:{target}")

        # Second call (different operation, same file) → ALLOW via turn grant
        r2 = gate.evaluate_request("fs", {"operation": "read", "path": target})
        assert r2.decision == ApprovalDecision.ALLOW

        # Verify also persisted to config
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        persisted = raw.get("agents", {}).get("approvals", {}).get("alwaysGrants", [])
        assert any(g.get("scope") == f"file:{target}" for g in persisted)

    @pytest.mark.asyncio
    async def test_turn_grant_covers_self_fix_retry(self, tmp_path: Path) -> None:
        """Approval from the first execution carries to a self-fix retry
        (different run_id, same turn)."""
        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
        )
        gate.begin_turn()

        target = str((tmp_path / "output.txt").resolve())

        # First execution attempt: fs write → AWAITING → user approves
        r1 = gate.evaluate_request(
            "fs", {"operation": "write", "path": target, "content": "v1"}, run_id="run-1"
        )
        assert r1.decision == ApprovalDecision.AWAITING
        gate.resolve(r1.approval_id, "ALLOW")

        # Self-fix retry (new run_id): same path, different content → ALLOW
        r2 = gate.evaluate_request(
            "fs", {"operation": "write", "path": target, "content": "v2"}, run_id="run-2"
        )
        assert r2.decision == ApprovalDecision.ALLOW

    @pytest.mark.asyncio
    async def test_turn_grant_does_not_leak_across_turns(self, tmp_path: Path) -> None:
        """Turn grants are cleared by begin_turn() and do not leak
        into the next user request."""
        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
        )

        target = str((tmp_path / "secret.txt").resolve())

        # Turn 1: approve fs write
        gate.begin_turn()
        r1 = gate.evaluate_request("fs", {"operation": "write", "path": target, "content": "data"})
        assert r1.decision == ApprovalDecision.AWAITING
        gate.resolve(r1.approval_id, "ALLOW")

        # Same turn: read → ALLOW
        r2 = gate.evaluate_request("fs", {"operation": "read", "path": target})
        assert r2.decision == ApprovalDecision.ALLOW

        # Turn 2: begin_turn clears grants → AWAITING again
        gate.begin_turn()
        r3 = gate.evaluate_request("fs", {"operation": "read", "path": target})
        assert r3.decision == ApprovalDecision.AWAITING

    @pytest.mark.asyncio
    async def test_turn_grant_different_file_not_covered(self, tmp_path: Path) -> None:
        """Plain 'Allow' on file A does NOT auto-approve file B."""
        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
        )
        gate.begin_turn()

        file_a = str((tmp_path / "a.txt").resolve())
        file_b = str((tmp_path / "b.txt").resolve())

        # Approve file A
        r1 = gate.evaluate_request("fs", {"operation": "read", "path": file_a})
        assert r1.decision == ApprovalDecision.AWAITING
        gate.resolve(r1.approval_id, "ALLOW")

        # File B → still AWAITING
        r2 = gate.evaluate_request("fs", {"operation": "read", "path": file_b})
        assert r2.decision == ApprovalDecision.AWAITING

    @pytest.mark.asyncio
    async def test_http_turn_grant_infers_host_scope(self) -> None:
        """Plain 'Allow' on http_request infers host: scope, covering
        subsequent requests to the same host."""
        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(
                enabled=True,
                interactive=True,
                allow_always=True,
                always_grants=[],
            ),
        )
        gate.begin_turn()

        # First call → AWAITING
        r1 = gate.evaluate_request(
            "http_request",
            {
                "method": "GET",
                "url": "https://api.example.com/v1/data",
            },
        )
        assert r1.decision == ApprovalDecision.AWAITING
        gate.resolve(r1.approval_id, "ALLOW")

        # Second call: same host, different path → ALLOW via inferred host: scope
        r2 = gate.evaluate_request(
            "http_request",
            {
                "method": "POST",
                "url": "https://api.example.com/v2/submit",
            },
        )
        assert r2.decision == ApprovalDecision.ALLOW

        # Third call: different host → AWAITING
        r3 = gate.evaluate_request(
            "http_request",
            {
                "method": "GET",
                "url": "https://evil.example.org/steal",
            },
        )
        assert r3.decision == ApprovalDecision.AWAITING
