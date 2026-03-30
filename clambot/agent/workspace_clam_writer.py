"""Workspace clam writer — writes generated clams to build/ and promotes to clams/.

Handles the two-stage persistence model:
  1. ``write_to_build()`` — write generated script + CLAM.md to ``build/<name>/``
  2. ``promote()`` — move from ``build/<name>/`` to ``clams/<name>/``
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path


class WorkspaceClamPersistenceWriter:
    """Manages clam persistence in the workspace build/ and clams/ directories."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)
        self._build_dir = self._workspace / "build"
        self._clams_dir = self._workspace / "clams"

    def write_to_build(
        self,
        clam_name: str,
        script: str,
        clam_md_content: str,
    ) -> Path:
        """Write a generated clam to the build directory.

        Args:
            clam_name: Slug name for the clam directory.
            script: JavaScript source code for ``run.js``.
            clam_md_content: Full content for ``CLAM.md``.

        Returns:
            Path to the clam's build directory.
        """
        clam_dir = self._build_dir / clam_name
        clam_dir.mkdir(parents=True, exist_ok=True)

        # Write script
        script_path = clam_dir / "run.js"
        script_path.write_text(script, encoding="utf-8")

        # Write CLAM.md
        clam_md_path = clam_dir / "CLAM.md"
        clam_md_path.write_text(clam_md_content, encoding="utf-8")

        return clam_dir

    def promote(self, clam_name: str) -> Path | None:
        """Promote a clam from build/ to clams/.

        Moves the entire ``build/<name>/`` directory to ``clams/<name>/``.
        If the target already exists, it is replaced.

        Args:
            clam_name: The clam directory name.

        Returns:
            Path to the promoted clam directory, or None if build dir missing.
        """
        source = self._build_dir / clam_name
        if not source.exists():
            return None

        target = self._clams_dir / clam_name
        self._clams_dir.mkdir(parents=True, exist_ok=True)

        # Replace existing if present
        if target.exists():
            shutil.rmtree(target)

        shutil.move(str(source), str(target))
        return target

    @staticmethod
    def generate_clam_name(request: str) -> str:
        """Generate a slug directory name from a request string.

        Converts the request to a filesystem-safe slug:
          - Lowercase
          - Replace non-alphanumeric with hyphens
          - Collapse multiple hyphens
          - Truncate to 60 chars
          - Strip leading/trailing hyphens

        Examples:
            >>> WorkspaceClamPersistenceWriter.generate_clam_name("What's the weather?")
            'whats-the-weather'
        """
        slug = request.lower()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        slug = slug.strip("-")
        return slug[:60]
