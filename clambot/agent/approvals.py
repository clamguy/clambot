"""Approval data structures and scope fingerprinting."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class ApprovalOption:
    """An approval scope option."""

    id: str
    label: str
    scope_description: str


@dataclass
class ApprovalRecord:
    """Record of an approval decision."""

    approval_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    tool_name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    scope_fingerprint: str = ""
    decision: str = ""  # "ALLOW" | "DENY" | "AWAITING"
    granted_scope: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def compute_scope_fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    """Compute a stable scope fingerprint for a tool call.

    Uses SHA-256 of canonical JSON of {tool: name, args: sorted_args}.
    Returns the first 16 hex characters.

    Arg order does NOT matter — canonical JSON ensures deterministic serialization.
    """
    canonical = json.dumps(
        {"tool": tool_name, "args": args},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:16]


class CapabilityApprovalStore:
    """In-memory store for approval grants with config-persisted always_grants.

    Tracks:
    - always_grants: persistent grants loaded from config (list of {tool, scope} dicts)
    - one_time_grants: per-run grants consumed on first use
    """

    def __init__(self, always_grants: list[dict] | None = None) -> None:
        self._always_grants: list[dict] = list(always_grants or [])
        self._one_time_grants: dict[str, list[dict]] = {}  # run_id -> list of grants

    def check_always_grant(
        self,
        tool_name: str,
        scope_fingerprint: str,
        args: dict[str, Any] | None = None,
    ) -> bool:
        """Check if there's a matching always_grant for this tool call.

        Args:
            tool_name: The tool being called.
            scope_fingerprint: SHA-256 fingerprint of the canonical call.
            args: Optional raw tool args — used for scope-based matching
                  (e.g. extracting the hostname from ``args["url"]``).
        """
        for grant in self._always_grants:
            if grant.get("tool") == tool_name:
                grant_scope = grant.get("scope", "")
                # Wildcard scope matches everything
                if grant_scope == "*":
                    return True
                # Fingerprint match
                if grant.get("fingerprint") and grant["fingerprint"] == scope_fingerprint:
                    return True
                # Scope-based matching against the actual args
                if grant_scope and args and self._scope_matches_args(grant_scope, args):
                    return True
        return False

    @staticmethod
    def _scope_matches_args(grant_scope: str, args: dict[str, Any]) -> bool:
        """Check if a scope descriptor matches the tool call args.

        Supported:
        - ``file:<abs_path>`` — matches if ``args["path"]`` resolves to that exact path.
        - ``dir:<abs_path>`` — matches if ``args["path"]`` resolves to that dir or a child.
        - ``host:<hostname>`` — matches if ``args["url"]`` has that host.
        - ``path:<url_prefix>`` — matches if ``args["url"]`` starts with it.
        - ``exact:<method>:<url>`` — matches method + url exactly.
        """
        from pathlib import Path
        from urllib.parse import urlparse

        # ── Filesystem scope matching (file: and dir:) ──
        if grant_scope.startswith("file:") or grant_scope.startswith("dir:"):
            raw_path = args.get("path")
            if not raw_path:
                return False
            try:
                resolved = str(Path(raw_path).resolve())
            except (ValueError, OSError):
                return False

            if grant_scope.startswith("file:"):
                stored_path = grant_scope[5:]
                try:
                    stored_resolved = str(Path(stored_path).resolve())
                except (ValueError, OSError):
                    return False
                return resolved == stored_resolved

            # dir: scope — match dir itself or any child
            stored_dir = grant_scope[4:]
            try:
                stored_resolved = str(Path(stored_dir).resolve())
            except (ValueError, OSError):
                return False
            # Exact match on the dir itself, or child with "/" separator
            # to prevent prefix collision (e.g. dir:/tmp/data must not match /tmp/datafile)
            return resolved == stored_resolved or resolved.startswith(stored_resolved + "/")

        # ── URL-based scope matching (host:, path:, exact:) ──
        url = args.get("url", "")
        if not url:
            return False

        if grant_scope.startswith("host:"):
            grant_host = grant_scope[5:]
            parsed = urlparse(url)
            return parsed.hostname == grant_host

        if grant_scope.startswith("path:"):
            grant_path = grant_scope[5:]
            return url.startswith(grant_path)

        if grant_scope.startswith("exact:"):
            # exact:GET:https://example.com/api
            parts = grant_scope.split(":", 2)
            if len(parts) == 3:
                method = args.get("method", "GET").upper()
                return parts[1] == method and parts[2] == url

        return False

    def add_always_grant(self, tool_name: str, scope: str, fingerprint: str = "") -> None:
        """Add a persistent always-grant entry."""
        entry: dict[str, str] = {"tool": tool_name, "scope": scope}
        if fingerprint:
            entry["fingerprint"] = fingerprint
        # Avoid duplicates
        if entry not in self._always_grants:
            self._always_grants.append(entry)

    def get_always_grants(self) -> list[dict]:
        """Return the current always_grants list (for config persistence)."""
        return list(self._always_grants)

    def register_one_time_grants(self, run_id: str, grants: list[dict]) -> None:
        """Register one-time grants for a specific run."""
        self._one_time_grants[run_id] = list(grants)

    def consume_one_time_grant(self, run_id: str, tool_name: str, scope_fingerprint: str) -> bool:
        """Try to consume a one-time grant. Returns True if found and consumed."""
        grants = self._one_time_grants.get(run_id, [])
        for i, grant in enumerate(grants):
            if grant.get("tool") == tool_name:
                grant_fp = grant.get("fingerprint", "")
                grant_scope = grant.get("scope", "")
                if grant_fp == scope_fingerprint or grant_scope == "*":
                    grants.pop(i)
                    return True
        return False

    def clear_one_time_grants(self, run_id: str) -> None:
        """Clear all one-time grants for a run."""
        self._one_time_grants.pop(run_id, None)
