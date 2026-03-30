"""Approval gate — tool call gating with always_grants, one-time grants, and interactive approval."""  # noqa: E501

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .approvals import ApprovalRecord, CapabilityApprovalStore, compute_scope_fingerprint


class ApprovalDecision(str, Enum):
    """Possible outcomes of an approval evaluation."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    AWAITING = "AWAITING"


@dataclass
class ApprovalResult:
    """Result of evaluating an approval request."""

    decision: ApprovalDecision
    approval_id: str = ""
    record: ApprovalRecord | None = None


class ApprovalGate:
    """Gate that controls tool execution via a multi-stage approval flow.

    Evaluation order:
    1. Check always_grants (persistent, config-stored)
    2. Check one-time grants (per-run, consumed on use)
    3. If interactive enabled, emit AWAITING for external resolution
    4. If interactive disabled, DENY
    """

    def __init__(
        self,
        approvals_config: Any | None = None,
        config_path: Path | None = None,
    ) -> None:
        # Extract config values
        enabled = True
        interactive = True
        allow_always = True
        always_grants: list[dict] = []

        if approvals_config is not None:
            enabled = getattr(approvals_config, "enabled", True)
            interactive = getattr(approvals_config, "interactive", True)
            allow_always = getattr(approvals_config, "allow_always", True)
            always_grants = getattr(approvals_config, "always_grants", [])

        self._enabled = enabled
        self._interactive = interactive
        self._allow_always = allow_always
        self._config_path = config_path
        self._store = CapabilityApprovalStore(always_grants=always_grants)

        # Pending approvals: approval_id -> (record, asyncio.Event)
        self._pending: dict[str, tuple[ApprovalRecord, asyncio.Event]] = {}

        # Turn-scoped grants: approvals granted during the current
        # process_turn() call that carry across tool calls and self-fix
        # retries within the same user request.  Reset via begin_turn().
        self._turn_grants: list[dict[str, str]] = []

    @property
    def store(self) -> CapabilityApprovalStore:
        """Access the underlying approval store."""
        return self._store

    def begin_turn(self) -> None:
        """Reset turn-scoped grants.  Call at the start of each ``process_turn``."""
        self._turn_grants.clear()

    def evaluate_request(
        self,
        tool_name: str,
        args: dict[str, Any],
        run_id: str = "",
    ) -> ApprovalResult:
        """Evaluate whether a tool call should be allowed.

        Returns ApprovalResult with decision ALLOW, DENY, or AWAITING.
        """
        if not self._enabled:
            return ApprovalResult(decision=ApprovalDecision.ALLOW)

        fingerprint = compute_scope_fingerprint(tool_name, args)

        # Stage 1: Check always_grants (pass args for scope-based matching)
        if self._store.check_always_grant(tool_name, fingerprint, args=args):
            return ApprovalResult(decision=ApprovalDecision.ALLOW)

        # Stage 1.5: Check turn-scoped grants (carry approval across the
        # whole user request — multiple tool calls and self-fix retries)
        if self._check_turn_grant(tool_name, fingerprint, args):
            return ApprovalResult(decision=ApprovalDecision.ALLOW)

        # Stage 2: Check one-time grants
        if run_id and self._store.consume_one_time_grant(run_id, tool_name, fingerprint):
            return ApprovalResult(decision=ApprovalDecision.ALLOW)

        # Stage 3: Interactive or DENY
        if not self._interactive:
            return ApprovalResult(decision=ApprovalDecision.DENY)

        # Emit AWAITING — create a pending record
        record = ApprovalRecord(
            tool_name=tool_name,
            args=args,
            scope_fingerprint=fingerprint,
            decision="AWAITING",
        )
        event = asyncio.Event()
        self._pending[record.approval_id] = (record, event)

        return ApprovalResult(
            decision=ApprovalDecision.AWAITING,
            approval_id=record.approval_id,
            record=record,
        )

    def resolve(
        self,
        approval_id: str,
        decision: str,
        grant_scope: str = "",
    ) -> ApprovalRecord | None:
        """Resolve a pending approval.

        Args:
            approval_id: The ID of the pending approval.
            decision: "ALLOW" or "DENY".
            grant_scope: If non-empty, persist as always_grant with that scope.

        Returns:
            The resolved ApprovalRecord, or None if not found.
        """
        pending = self._pending.pop(approval_id, None)
        if pending is None:
            return None

        record, event = pending
        record.decision = decision

        if decision == "ALLOW":
            # Always register a turn-scoped grant so subsequent tool
            # calls within the same user request are auto-approved.
            turn_scope = grant_scope or self._infer_turn_scope(record)
            turn_entry: dict[str, str] = {"tool": record.tool_name}
            if turn_scope:
                turn_entry["scope"] = turn_scope
            turn_entry["fingerprint"] = record.scope_fingerprint
            self._turn_grants.append(turn_entry)

            # Persistent always_grant when user chose "Allow Always"
            if grant_scope:
                record.granted_scope = grant_scope
                if self._allow_always:
                    self._store.add_always_grant(
                        record.tool_name,
                        scope=grant_scope,
                        fingerprint=record.scope_fingerprint,
                    )
                    self._persist_always_grants()

        event.set()
        return record

    # ------------------------------------------------------------------
    # Turn-scoped grant helpers
    # ------------------------------------------------------------------

    def _check_turn_grant(
        self,
        tool_name: str,
        fingerprint: str,
        args: dict[str, Any],
    ) -> bool:
        """Check if a turn-scoped grant covers this tool call."""
        for grant in self._turn_grants:
            if grant.get("tool") != tool_name:
                continue
            # Exact fingerprint match (same args)
            if grant.get("fingerprint") == fingerprint:
                return True
            # Scope-based match (e.g. file:/tmp/test.txt covers read+write)
            scope = grant.get("scope", "")
            if scope and self._store._scope_matches_args(scope, args):
                return True
        return False

    @staticmethod
    def _infer_turn_scope(record: ApprovalRecord) -> str:
        """Infer the narrowest reasonable scope from a resolved approval.

        Used when the user clicks plain "Allow" (no explicit scope) so
        that subsequent calls on the same resource are auto-approved
        within the current turn.

        Returns:
            A scope string (``file:<path>`` or ``host:<hostname>``),
            or ``""`` if no scope can be inferred.
        """
        args = record.args

        # Filesystem: infer file-level scope from resolved path
        if "path" in args:
            try:
                from pathlib import Path as _Path

                resolved = str(_Path(args["path"]).resolve())
                return f"file:{resolved}"
            except Exception:  # noqa: BLE001
                pass

        # HTTP: infer host-level scope from URL
        if "url" in args:
            try:
                from urllib.parse import urlparse

                parsed = urlparse(args["url"])
                if parsed.hostname:
                    return f"host:{parsed.hostname}"
            except Exception:  # noqa: BLE001
                pass

        return ""

    async def wait_for_resolution(self, approval_id: str) -> ApprovalRecord | None:
        """Wait until a pending approval is resolved."""
        pending = self._pending.get(approval_id)
        if pending is None:
            return None
        record, event = pending
        await event.wait()
        return record

    def register_one_time_grants(self, run_id: str, grants: list[dict]) -> None:
        """Register one-time grants from InboundMessage metadata."""
        self._store.register_one_time_grants(run_id, grants)

    def persist_always_grant(self, tool_name: str, scope: str) -> None:
        """Manually persist an always_grant to config."""
        fingerprint = ""
        self._store.add_always_grant(tool_name, scope, fingerprint)
        self._persist_always_grants()

    def _persist_always_grants(self) -> None:
        """Write current always_grants back to config file."""
        if self._config_path is None or not self._config_path.exists():
            return

        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            raw = {}

        # Navigate to agents.approvals.alwaysGrants (camelCase in JSON)
        agents = raw.setdefault("agents", {})
        approvals = agents.setdefault("approvals", {})
        approvals["alwaysGrants"] = self._store.get_always_grants()

        self._config_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def get_pending_record(self, approval_id: str) -> ApprovalRecord | None:
        """Get a pending approval record without resolving it."""
        pending = self._pending.get(approval_id)
        if pending is None:
            return None
        return pending[0]
