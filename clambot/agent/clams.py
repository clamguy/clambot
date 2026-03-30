"""Clam registry — disk-based clam catalog with YAML frontmatter parsing.

Scans ``clams/*/CLAM.md`` in the workspace, parses YAML frontmatter for
metadata, and provides a registry for clam lookup and catalog generation.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ClamSummary:
    """Lightweight summary of a clam for catalog display."""

    name: str
    description: str = ""
    declared_tools: list[str] = field(default_factory=list)
    source_request: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    last_used: float = 0.0
    usage_count: int = 0


@dataclass
class Clam:
    """Full clam representation including script and metadata."""

    name: str
    script: str = ""
    declared_tools: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    language: str = "javascript"
    last_used: float = 0.0
    usage_count: int = 0

    @property
    def description(self) -> str:
        return self.metadata.get("description", "")

    @property
    def source_request(self) -> str:
        return self.metadata.get("source_request", "")

    @property
    def reusable(self) -> bool:
        return self.metadata.get("reusable", False)


# ---------------------------------------------------------------------------
# YAML frontmatter parser (no pyyaml dependency for simple frontmatter)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse simple YAML key-value pairs (flat + lists).

    Handles:
      key: value
      key:
        - item1
        - item2
    """
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item
        if stripped.startswith("- ") and current_key is not None:
            if current_list is None:
                current_list = []
                result[current_key] = current_list
            current_list.append(stripped[2:].strip().strip('"').strip("'"))
            continue

        # Key-value pair
        if ":" in stripped:
            if current_list is not None:
                current_list = None

            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()

            current_key = key

            if val:
                # Strip quotes
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]

                # Parse booleans
                if val.lower() == "true":
                    result[key] = True
                elif val.lower() == "false":
                    result[key] = False
                else:
                    # Try JSON (for dicts/lists like inputs: {"a": 1})
                    if val.startswith(("{", "[")):
                        try:
                            result[key] = json.loads(val)
                            current_list = None
                            continue
                        except (json.JSONDecodeError, ValueError):
                            pass
                    # Try integer
                    try:
                        result[key] = int(val)
                    except ValueError:
                        result[key] = val

                current_list = None
            else:
                # Value on next lines (list)
                current_list = None

    return result


def parse_clam_md(content: str) -> dict[str, Any]:
    """Parse a CLAM.md file, extracting YAML frontmatter and description."""
    metadata: dict[str, Any] = {}

    match = _FRONTMATTER_RE.match(content)
    if match:
        yaml_text = match.group(1)
        metadata = _parse_simple_yaml(yaml_text)

        # Body after frontmatter as description if not in frontmatter
        body = content[match.end() :].strip()
        if body and "description" not in metadata:
            # First paragraph as description
            first_para = body.split("\n\n")[0].strip()
            if first_para:
                metadata["description"] = first_para

    return metadata


# ---------------------------------------------------------------------------
# Clam Registry
# ---------------------------------------------------------------------------


class ClamRegistry:
    """Registry for promoted clams in the workspace.

    Scans ``clams/*/CLAM.md`` for clam metadata and provides catalog
    generation and individual clam loading.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)
        self._clams_dir = self._workspace / "clams"
        self._catalog_cache: list[ClamSummary] | None = None
        self._usage_path = self._clams_dir / ".usage.json"
        self._usage_cache: dict[str, Any] | None = None

    def get_catalog(self) -> list[ClamSummary]:
        """Scan clams directory and return catalog of all promoted clams."""
        if self._catalog_cache is not None:
            return self._catalog_cache

        summaries: list[ClamSummary] = []

        if not self._clams_dir.exists():
            self._catalog_cache = summaries
            return summaries

        for clam_dir in sorted(self._clams_dir.iterdir()):
            if not clam_dir.is_dir():
                continue

            clam_md = clam_dir / "CLAM.md"
            if not clam_md.exists():
                continue

            try:
                content = clam_md.read_text(encoding="utf-8")
                metadata = parse_clam_md(content)

                raw_inputs = metadata.get("inputs", {})
                last_used, usage_count = self.get_usage(clam_dir.name)
                summaries.append(
                    ClamSummary(
                        name=clam_dir.name,
                        description=metadata.get("description", ""),
                        declared_tools=metadata.get("declared_tools", []),
                        source_request=metadata.get("source_request", ""),
                        inputs=raw_inputs if isinstance(raw_inputs, dict) else {},
                        last_used=last_used,
                        usage_count=usage_count,
                    )
                )
            except Exception:
                # Skip malformed clams
                continue

        self._catalog_cache = summaries
        return summaries

    def load(self, clam_id: str) -> Clam | None:
        """Load a full clam by its ID (directory name).

        Returns None if the clam doesn't exist.
        """
        clam_dir = self._clams_dir / clam_id
        if not clam_dir.is_dir():
            return None

        # Load CLAM.md metadata
        clam_md = clam_dir / "CLAM.md"
        metadata: dict[str, Any] = {}
        if clam_md.exists():
            try:
                content = clam_md.read_text(encoding="utf-8")
                metadata = parse_clam_md(content)
            except Exception:
                pass

        # Load script
        script = ""
        script_path = clam_dir / "run.js"
        if script_path.exists():
            try:
                script = script_path.read_text(encoding="utf-8")
            except Exception:
                pass

        last_used, usage_count = self.get_usage(clam_id)
        return Clam(
            name=clam_id,
            script=script,
            declared_tools=metadata.get("declared_tools", []),
            inputs=metadata.get("inputs", {}),
            metadata=metadata,
            language=metadata.get("language", "javascript"),
            last_used=last_used,
            usage_count=usage_count,
        )

    # ------------------------------------------------------------------
    # Usage tracking
    # ------------------------------------------------------------------

    def _load_usage(self) -> dict[str, Any]:
        """Load usage stats from the sidecar JSON file."""
        if self._usage_cache is not None:
            return self._usage_cache

        if self._usage_path.exists():
            try:
                self._usage_cache = json.loads(self._usage_path.read_text(encoding="utf-8"))
            except Exception:
                logger.debug("Failed to read usage stats, starting fresh")
                self._usage_cache = {}
        else:
            self._usage_cache = {}

        return self._usage_cache

    def _save_usage(self, data: dict[str, Any]) -> None:
        """Persist usage stats to disk."""
        self._usage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._usage_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("Failed to write usage stats", exc_info=True)

    def record_usage(self, clam_id: str) -> None:
        """Record a successful execution of a clam.

        Increments ``usage_count`` and sets ``last_used`` to the
        current Unix timestamp.
        """
        usage = self._load_usage()
        entry = usage.get(clam_id, {"last_used": 0.0, "usage_count": 0})
        entry["last_used"] = time.time()
        entry["usage_count"] = entry.get("usage_count", 0) + 1
        usage[clam_id] = entry
        self._usage_cache = usage
        self._save_usage(usage)
        # Invalidate catalog cache so next read picks up new stats
        self._catalog_cache = None

    def get_usage(self, clam_id: str) -> tuple[float, int]:
        """Return ``(last_used, usage_count)`` for a clam."""
        usage = self._load_usage()
        entry = usage.get(clam_id, {})
        return entry.get("last_used", 0.0), entry.get("usage_count", 0)

    def invalidate_cache(self) -> None:
        """Clear the catalog cache, forcing a rescan on next access."""
        self._catalog_cache = None
        self._usage_cache = None
