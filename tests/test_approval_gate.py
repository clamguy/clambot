"""Tests for clambot.agent — Phase 6 Approval Gate."""

import asyncio
import json
from pathlib import Path

import pytest

from clambot.agent.approval_gate import (
    ApprovalDecision,
    ApprovalGate,
)
from clambot.agent.approvals import (
    CapabilityApprovalStore,
    compute_scope_fingerprint,
)
from clambot.agent.capabilities import (
    CapabilityConstraint,
    CapabilityEvaluator,
    CapabilityPolicy,
)
from clambot.agent.policy_violations import PolicyViolationCode, PolicyViolationPayload

# ---------------------------------------------------------------------------
# Fingerprint tests
# ---------------------------------------------------------------------------


class TestScopeFingerprint:
    """Tests for compute_scope_fingerprint — stability and sensitivity."""

    def test_same_args_same_fingerprint(self) -> None:
        """Same tool name + args always produce the same fingerprint."""
        fp1 = compute_scope_fingerprint("fs", {"operation": "read", "path": "foo.txt"})
        fp2 = compute_scope_fingerprint("fs", {"operation": "read", "path": "foo.txt"})
        assert fp1 == fp2

    def test_different_args_different_fingerprint(self) -> None:
        """Different args produce different fingerprints."""
        fp1 = compute_scope_fingerprint("fs", {"operation": "read", "path": "foo.txt"})
        fp2 = compute_scope_fingerprint("fs", {"operation": "read", "path": "bar.txt"})
        assert fp1 != fp2

    def test_different_tool_different_fingerprint(self) -> None:
        """Different tool names produce different fingerprints."""
        fp1 = compute_scope_fingerprint("fs", {"path": "foo.txt"})
        fp2 = compute_scope_fingerprint("http_request", {"path": "foo.txt"})
        assert fp1 != fp2

    def test_arg_order_does_not_matter(self) -> None:
        """Canonical JSON ensures arg key order does not affect fingerprint."""
        fp1 = compute_scope_fingerprint("fs", {"operation": "read", "path": "foo.txt"})
        fp2 = compute_scope_fingerprint("fs", {"path": "foo.txt", "operation": "read"})
        assert fp1 == fp2

    def test_fingerprint_is_16_hex_chars(self) -> None:
        """Fingerprint is exactly 16 hex characters."""
        fp = compute_scope_fingerprint("echo", {"message": "hello"})
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_empty_args(self) -> None:
        """Empty args produce a valid fingerprint."""
        fp = compute_scope_fingerprint("echo", {})
        assert len(fp) == 16


# ---------------------------------------------------------------------------
# CapabilityApprovalStore tests
# ---------------------------------------------------------------------------


class TestCapabilityApprovalStore:
    """Tests for the in-memory approval store."""

    def test_always_grant_match(self) -> None:
        """An always_grant with matching fingerprint returns True."""
        fp = compute_scope_fingerprint("fs", {"operation": "read", "path": "data.txt"})
        store = CapabilityApprovalStore(always_grants=[{"tool": "fs", "fingerprint": fp}])
        assert store.check_always_grant("fs", fp) is True

    def test_always_grant_wildcard(self) -> None:
        """An always_grant with wildcard scope matches any fingerprint."""
        store = CapabilityApprovalStore(always_grants=[{"tool": "fs", "scope": "*"}])
        assert store.check_always_grant("fs", "anyfingerprint") is True

    def test_always_grant_no_match(self) -> None:
        """No matching always_grant returns False."""
        store = CapabilityApprovalStore(
            always_grants=[{"tool": "http_request", "fingerprint": "abcdef1234567890"}]
        )
        fp = compute_scope_fingerprint("fs", {"path": "foo.txt"})
        assert store.check_always_grant("fs", fp) is False

    def test_one_time_grant_consumed(self) -> None:
        """One-time grant is consumed on use — second call returns False."""
        fp = compute_scope_fingerprint("fs", {"path": "foo.txt"})
        store = CapabilityApprovalStore()
        store.register_one_time_grants("run-1", [{"tool": "fs", "fingerprint": fp}])
        # First call consumes the grant
        assert store.consume_one_time_grant("run-1", "fs", fp) is True
        # Second call — grant already consumed
        assert store.consume_one_time_grant("run-1", "fs", fp) is False

    def test_one_time_grant_wrong_run_id(self) -> None:
        """One-time grant for a different run_id does not match."""
        fp = compute_scope_fingerprint("fs", {"path": "x"})
        store = CapabilityApprovalStore()
        store.register_one_time_grants("run-1", [{"tool": "fs", "fingerprint": fp}])
        assert store.consume_one_time_grant("run-2", "fs", fp) is False

    def test_add_always_grant(self) -> None:
        """Adding an always_grant makes it available for matching."""
        store = CapabilityApprovalStore()
        fp = compute_scope_fingerprint("fs", {"path": "new.txt"})
        store.add_always_grant("fs", "file:new.txt", fingerprint=fp)
        assert store.check_always_grant("fs", fp) is True

    def test_add_always_grant_no_duplicates(self) -> None:
        """Adding the same always_grant twice does not create duplicates."""
        store = CapabilityApprovalStore()
        store.add_always_grant("fs", "file:x", fingerprint="abc")
        store.add_always_grant("fs", "file:x", fingerprint="abc")
        assert len(store.get_always_grants()) == 1


# ---------------------------------------------------------------------------
# Filesystem scope matching tests (Phase 1 — dev_tasks.md)
# ---------------------------------------------------------------------------


class TestFilesystemScopeMatching:
    """Tests for file: and dir: scope matching in _scope_matches_args."""

    def test_file_scope_matches_exact_path(self) -> None:
        """file:/tmp/x.txt matches args={"path": "/tmp/x.txt"}."""
        result = CapabilityApprovalStore._scope_matches_args(
            "file:/tmp/x.txt", {"path": "/tmp/x.txt"}
        )
        assert result is True

    def test_file_scope_no_match_different_file(self) -> None:
        """file:/tmp/x.txt does NOT match args={"path": "/tmp/y.txt"}."""
        result = CapabilityApprovalStore._scope_matches_args(
            "file:/tmp/x.txt", {"path": "/tmp/y.txt"}
        )
        assert result is False

    def test_dir_scope_matches_child(self) -> None:
        """dir:/tmp/data matches args={"path": "/tmp/data/sub/file.txt"}."""
        result = CapabilityApprovalStore._scope_matches_args(
            "dir:/tmp/data", {"path": "/tmp/data/sub/file.txt"}
        )
        assert result is True

    def test_dir_scope_matches_dir_itself(self) -> None:
        """dir:/tmp/data matches args={"path": "/tmp/data"}."""
        result = CapabilityApprovalStore._scope_matches_args("dir:/tmp/data", {"path": "/tmp/data"})
        assert result is True

    def test_dir_scope_no_match_sibling_prefix(self) -> None:
        """dir:/tmp/data does NOT match args={"path": "/tmp/datafile"}."""
        result = CapabilityApprovalStore._scope_matches_args(
            "dir:/tmp/data", {"path": "/tmp/datafile"}
        )
        assert result is False

    def test_file_and_dir_scopes_skip_when_no_path_arg(self) -> None:
        """args={"url": "http://..."} returns False (no crash)."""
        result_file = CapabilityApprovalStore._scope_matches_args(
            "file:/tmp/x.txt", {"url": "http://example.com"}
        )
        result_dir = CapabilityApprovalStore._scope_matches_args(
            "dir:/tmp/data", {"url": "http://example.com"}
        )
        assert result_file is False
        assert result_dir is False


# ---------------------------------------------------------------------------
# ApprovalGate tests
# ---------------------------------------------------------------------------


class TestApprovalGate:
    """Tests for the ApprovalGate evaluation flow."""

    def test_disabled_gate_allows_all(self) -> None:
        """When approvals.enabled=False, all requests are allowed."""
        from clambot.config.schema import ApprovalsConfig

        config = ApprovalsConfig(enabled=False)
        gate = ApprovalGate(approvals_config=config)
        result = gate.evaluate_request("fs", {"operation": "read", "path": "x"})
        assert result.decision == ApprovalDecision.ALLOW

    def test_always_grant_immediate_allow(self) -> None:
        """A matching always_grant produces immediate ALLOW."""
        from clambot.config.schema import ApprovalsConfig

        fp = compute_scope_fingerprint("fs", {"operation": "read"})
        config = ApprovalsConfig(always_grants=[{"tool": "fs", "fingerprint": fp}])
        gate = ApprovalGate(approvals_config=config)
        result = gate.evaluate_request("fs", {"operation": "read"})
        assert result.decision == ApprovalDecision.ALLOW

    def test_one_time_grant_consumed(self) -> None:
        """One-time grant allows first call but AWAITING on second."""
        from clambot.config.schema import ApprovalsConfig

        config = ApprovalsConfig()
        gate = ApprovalGate(approvals_config=config)

        fp = compute_scope_fingerprint("fs", {"operation": "write"})
        gate.register_one_time_grants("run-1", [{"tool": "fs", "fingerprint": fp}])

        # First call — consumed
        result1 = gate.evaluate_request("fs", {"operation": "write"}, run_id="run-1")
        assert result1.decision == ApprovalDecision.ALLOW

        # Second call — no grant left → AWAITING
        result2 = gate.evaluate_request("fs", {"operation": "write"}, run_id="run-1")
        assert result2.decision == ApprovalDecision.AWAITING

    def test_interactive_disabled_denies(self) -> None:
        """When interactive=False and no grants match, request is DENIED."""
        from clambot.config.schema import ApprovalsConfig

        config = ApprovalsConfig(interactive=False)
        gate = ApprovalGate(approvals_config=config)
        result = gate.evaluate_request("fs", {"operation": "delete"})
        assert result.decision == ApprovalDecision.DENY

    def test_awaiting_emits_approval_id(self) -> None:
        """AWAITING result contains a valid approval_id UUID."""
        from clambot.config.schema import ApprovalsConfig

        config = ApprovalsConfig()
        gate = ApprovalGate(approvals_config=config)
        result = gate.evaluate_request("fs", {"operation": "write"})
        assert result.decision == ApprovalDecision.AWAITING
        assert result.approval_id != ""
        assert len(result.approval_id) > 0

    def test_resolve_returns_record(self) -> None:
        """Resolving a pending approval returns the ApprovalRecord."""
        from clambot.config.schema import ApprovalsConfig

        config = ApprovalsConfig()
        gate = ApprovalGate(approvals_config=config)
        result = gate.evaluate_request("fs", {"operation": "write"})
        assert result.decision == ApprovalDecision.AWAITING

        record = gate.resolve(result.approval_id, "ALLOW")
        assert record is not None
        assert record.decision == "ALLOW"

    def test_resolve_nonexistent_returns_none(self) -> None:
        """Resolving a non-existent approval_id returns None."""
        from clambot.config.schema import ApprovalsConfig

        gate = ApprovalGate(approvals_config=ApprovalsConfig())
        assert gate.resolve("nonexistent-id", "ALLOW") is None

    def test_persist_always_grant_to_config(self, tmp_path: Path) -> None:
        """persist_always_grant writes to config file."""
        config_path = tmp_path / "config.json"
        config_path.write_text("{}", encoding="utf-8")

        from clambot.config.schema import ApprovalsConfig

        gate = ApprovalGate(
            approvals_config=ApprovalsConfig(),
            config_path=config_path,
        )
        gate.persist_always_grant("fs", "file:test.txt")

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        grants = raw["agents"]["approvals"]["alwaysGrants"]
        assert any(g["tool"] == "fs" and g["scope"] == "file:test.txt" for g in grants)

    @pytest.mark.asyncio
    async def test_resolve_wakes_waiter(self) -> None:
        """Resolving a pending approval wakes the wait_for_resolution coroutine."""
        from clambot.config.schema import ApprovalsConfig

        gate = ApprovalGate(approvals_config=ApprovalsConfig())
        result = gate.evaluate_request("fs", {"operation": "write"})
        assert result.decision == ApprovalDecision.AWAITING

        async def resolve_later():
            await asyncio.sleep(0.05)
            gate.resolve(result.approval_id, "ALLOW", grant_scope="")

        asyncio.create_task(resolve_later())
        record = await gate.wait_for_resolution(result.approval_id)
        assert record is not None
        assert record.decision == "ALLOW"


# ---------------------------------------------------------------------------
# CapabilityEvaluator tests
# ---------------------------------------------------------------------------


class TestCapabilityEvaluator:
    """Tests for capability constraint DSL evaluation."""

    def test_is_in_allowed(self) -> None:
        """is_in constraint passes when value is in the allowed set."""
        evaluator = CapabilityEvaluator(
            policies=[
                CapabilityPolicy(
                    method="fs",
                    constraints=[CapabilityConstraint("operation", "is_in", ["read", "list"])],
                )
            ]
        )
        result = evaluator.evaluate("fs", {"operation": "read"})
        assert result is None  # No violation

    def test_is_in_violation(self) -> None:
        """is_in constraint fails when value is not in the allowed set."""
        evaluator = CapabilityEvaluator(
            policies=[
                CapabilityPolicy(
                    method="fs",
                    constraints=[CapabilityConstraint("operation", "is_in", ["read", "list"])],
                )
            ]
        )
        result = evaluator.evaluate("fs", {"operation": "write"})
        assert result is not None
        assert result.code == PolicyViolationCode.CONSTRAINT_VIOLATION
        assert "operation" in result.message

    def test_starts_with_allowed(self) -> None:
        """starts_with constraint passes when value starts with prefix."""
        evaluator = CapabilityEvaluator(
            policies=[
                CapabilityPolicy(
                    method="fs",
                    constraints=[CapabilityConstraint("path", "starts_with", "/data")],
                )
            ]
        )
        result = evaluator.evaluate("fs", {"path": "/data/file.csv"})
        assert result is None

    def test_starts_with_violation(self) -> None:
        """starts_with constraint fails when value doesn't start with prefix."""
        evaluator = CapabilityEvaluator(
            policies=[
                CapabilityPolicy(
                    method="fs",
                    constraints=[CapabilityConstraint("path", "starts_with", "/data")],
                )
            ]
        )
        result = evaluator.evaluate("fs", {"path": "/etc/passwd"})
        assert result is not None
        assert result.code == PolicyViolationCode.CONSTRAINT_VIOLATION

    def test_max_calls_exceeded(self) -> None:
        """max_calls constraint triggers after exceeding the limit."""
        evaluator = CapabilityEvaluator(policies=[CapabilityPolicy(method="fs", max_calls=2)])
        # First two calls ok
        assert evaluator.evaluate("fs", {"operation": "read"}, call_count=1) is None
        assert evaluator.evaluate("fs", {"operation": "read"}, call_count=2) is None
        # Third call exceeds
        result = evaluator.evaluate("fs", {"operation": "read"}, call_count=3)
        assert result is not None
        assert result.code == PolicyViolationCode.MAX_CALLS_EXCEEDED

    def test_max_calls_internal_tracking(self) -> None:
        """max_calls tracks call count internally when not provided."""
        evaluator = CapabilityEvaluator(policies=[CapabilityPolicy(method="fs", max_calls=2)])
        assert evaluator.evaluate("fs", {}) is None  # call 1
        assert evaluator.evaluate("fs", {}) is None  # call 2
        result = evaluator.evaluate("fs", {})  # call 3
        assert result is not None
        assert result.code == PolicyViolationCode.MAX_CALLS_EXCEEDED

    def test_no_policy_allows_all(self) -> None:
        """Tools without a declared policy are always allowed."""
        evaluator = CapabilityEvaluator(policies=[])
        result = evaluator.evaluate("any_tool", {"any": "args"})
        assert result is None

    def test_lte_constraint_allowed(self) -> None:
        """<= constraint passes when value is within bounds."""
        evaluator = CapabilityEvaluator(
            policies=[
                CapabilityPolicy(
                    method="http_request",
                    constraints=[CapabilityConstraint("timeout", "<=", 30)],
                )
            ]
        )
        result = evaluator.evaluate("http_request", {"timeout": 25})
        assert result is None

    def test_lte_constraint_violation(self) -> None:
        """<= constraint fails when value exceeds the bound."""
        evaluator = CapabilityEvaluator(
            policies=[
                CapabilityPolicy(
                    method="http_request",
                    constraints=[CapabilityConstraint("timeout", "<=", 30)],
                )
            ]
        )
        result = evaluator.evaluate("http_request", {"timeout": 50})
        assert result is not None
        assert result.code == PolicyViolationCode.CONSTRAINT_VIOLATION

    def test_gte_constraint_allowed(self) -> None:
        """>= constraint passes when value meets the minimum."""
        evaluator = CapabilityEvaluator(
            policies=[
                CapabilityPolicy(
                    method="fs",
                    constraints=[CapabilityConstraint("min_size", ">=", 100)],
                )
            ]
        )
        result = evaluator.evaluate("fs", {"min_size": 200})
        assert result is None

    def test_gte_constraint_violation(self) -> None:
        """>= constraint fails when value is below the minimum."""
        evaluator = CapabilityEvaluator(
            policies=[
                CapabilityPolicy(
                    method="fs",
                    constraints=[CapabilityConstraint("min_size", ">=", 100)],
                )
            ]
        )
        result = evaluator.evaluate("fs", {"min_size": 50})
        assert result is not None
        assert result.code == PolicyViolationCode.CONSTRAINT_VIOLATION

    def test_multiple_constraints_all_must_pass(self) -> None:
        """All constraints must pass — first violation stops evaluation."""
        evaluator = CapabilityEvaluator(
            policies=[
                CapabilityPolicy(
                    method="fs",
                    constraints=[
                        CapabilityConstraint("operation", "is_in", ["read"]),
                        CapabilityConstraint("path", "starts_with", "/safe"),
                    ],
                )
            ]
        )
        # operation ok, path bad
        result = evaluator.evaluate("fs", {"operation": "read", "path": "/etc/passwd"})
        assert result is not None

    def test_from_clam_metadata(self) -> None:
        """from_clam_metadata correctly parses policy from metadata dict."""
        metadata = {
            "capabilities": [
                {
                    "method": "fs",
                    "constraints": [
                        {"param": "operation", "op": "is_in", "value": ["read"]},
                    ],
                    "max_calls": 5,
                }
            ]
        }
        evaluator = CapabilityEvaluator.from_clam_metadata(metadata)
        # Should allow read
        assert evaluator.evaluate("fs", {"operation": "read"}) is None
        # Should deny write
        result = evaluator.evaluate("fs", {"operation": "write"})
        assert result is not None

    def test_reset_counts(self) -> None:
        """reset_counts clears all tracked call counts."""
        evaluator = CapabilityEvaluator(policies=[CapabilityPolicy(method="fs", max_calls=1)])
        evaluator.evaluate("fs", {})  # call 1
        evaluator.reset_counts()
        # After reset, should be allowed again
        assert evaluator.evaluate("fs", {}) is None


# ---------------------------------------------------------------------------
# PolicyViolationPayload tests
# ---------------------------------------------------------------------------


class TestPolicyViolationPayload:
    """Tests for the PolicyViolationPayload data structure."""

    def test_payload_is_frozen(self) -> None:
        """PolicyViolationPayload is immutable (frozen dataclass)."""
        payload = PolicyViolationPayload(
            code=PolicyViolationCode.CONSTRAINT_VIOLATION,
            tool_name="fs",
            message="test",
        )
        with pytest.raises(AttributeError):
            payload.message = "changed"  # type: ignore[misc]

    def test_payload_fields(self) -> None:
        """PolicyViolationPayload has all required fields."""
        payload = PolicyViolationPayload(
            code=PolicyViolationCode.MAX_CALLS_EXCEEDED,
            tool_name="http_request",
            message="Too many calls",
            detail={"max": 10, "actual": 11},
        )
        assert payload.code == PolicyViolationCode.MAX_CALLS_EXCEEDED
        assert payload.tool_name == "http_request"
        assert payload.message == "Too many calls"
        assert payload.detail == {"max": 10, "actual": 11}

    def test_violation_codes_are_strings(self) -> None:
        """All PolicyViolationCode values are stable strings."""
        assert PolicyViolationCode.CONSTRAINT_VIOLATION == "constraint_violation"
        assert PolicyViolationCode.MAX_CALLS_EXCEEDED == "max_calls_exceeded"
        assert PolicyViolationCode.UNDECLARED_TOOL == "undeclared_tool"
        assert PolicyViolationCode.POLICY_PARSE_ERROR == "policy_parse_error"
