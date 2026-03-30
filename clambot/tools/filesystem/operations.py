"""Pure filesystem operation functions for the ClamBot fs tool.

Each function is a standalone, side-effect-only helper that performs a single
filesystem action and returns a human-readable result string.  They are
intentionally free of tool-framework concerns so they can be unit-tested in
isolation.
"""

from __future__ import annotations

import difflib
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "fs_disk_usage",
    "fs_list",
    "fs_read",
    "fs_write",
    "fs_edit",
]

# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def fs_list(path: Path) -> str:
    """List the contents of a directory with extended metadata.

    Returns a sorted, newline-separated string where each entry shows:
    - ``[DIR]`` or ``[FILE]`` prefix
    - Entry name
    - Human-readable size (files only)
    - Last-modified timestamp (ISO 8601, UTC)

    Args:
        path: Directory path to list.

    Returns:
        Formatted directory listing as a string.

    Raises:
        NotADirectoryError: If *path* exists but is not a directory.
        FileNotFoundError: If *path* does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: '{path}'")
    if not path.is_dir():
        raise NotADirectoryError(f"Path is not a directory: '{path}'")

    entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))

    if not entries:
        return f"(empty directory: {path})"

    lines: list[str] = []
    for entry in entries:
        try:
            st = entry.stat()
        except OSError:
            lines.append(f"[????] {entry.name}  (stat failed)")
            continue

        mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        if entry.is_dir():
            lines.append(f"[DIR]  {entry.name}  modified {mtime}")
        else:
            lines.append(f"[FILE] {entry.name}  {_human_size(st.st_size)}  modified {mtime}")

    return "\n".join(lines)


def _human_size(size_bytes: int) -> str:
    """Format *size_bytes* into a concise human-readable string.

    Uses binary units (KiB, MiB, GiB) for sizes ≥ 1 KiB; plain bytes
    for anything smaller.
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size_bytes /= 1024  # type: ignore[assignment]
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
    return f"{size_bytes:.1f} PiB"


def fs_disk_usage(path: Path, limit: int = 20) -> str:
    """Compute recursive disk usage for immediate subdirectories of *path*.

    Walks each child directory, sums file sizes, and returns entries sorted
    from largest to smallest.  Permission errors on individual subtrees are
    silently skipped so that partially-readable directories still produce
    useful output.

    Args:
        path: Root directory to scan.
        limit: Maximum number of entries to return (default 20).

    Returns:
        A formatted, newline-separated report of subdirectory sizes plus
        a total line.

    Raises:
        NotADirectoryError: If *path* exists but is not a directory.
        FileNotFoundError: If *path* does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Path does not exist: '{path}'")
    if not path.is_dir():
        raise NotADirectoryError(f"Path is not a directory: '{path}'")

    entries: list[tuple[str, int]] = []
    total = 0

    for child in sorted(path.iterdir(), key=lambda e: e.name.lower()):
        if child.is_dir():
            size = _dir_size(child)
            entries.append((child.name, size))
            total += size
        elif child.is_file():
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            total += size

    # Sort descending by size
    entries.sort(key=lambda e: e[1], reverse=True)
    entries = entries[:limit]

    if not entries:
        return f"(no subdirectories in {path})"

    lines: list[str] = []
    for i, (name, size) in enumerate(entries, 1):
        lines.append(f"{i:>3}. {_human_size(size):>10}  {name}")

    lines.append(f"\nTotal: {_human_size(total)}  ({path})")
    return "\n".join(lines)


def _dir_size(path: Path) -> int:
    """Recursively compute the total size of all files under *path*."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except (OSError, PermissionError):
        pass
    return total


def fs_read(path: Path, max_bytes: int = 1_048_576) -> str:
    """Read a file's text content, optionally truncating at *max_bytes*.

    Args:
        path: Path to the file to read.
        max_bytes: Maximum number of bytes to read.  Content beyond this
            limit is dropped and a ``[TRUNCATED]`` notice is appended.

    Returns:
        File content as a string, possibly with a truncation notice.

    Raises:
        FileNotFoundError: If *path* does not exist.
        IsADirectoryError: If *path* is a directory, not a file.
    """
    if not path.exists():
        raise FileNotFoundError(f"File does not exist: '{path}'")
    if not path.is_file():
        raise IsADirectoryError(f"Path is a directory, not a file: '{path}'")

    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]

    # Decode with replacement so binary-ish files don't crash.
    content = raw.decode("utf-8", errors="replace")

    if truncated:
        content += (
            f"\n\n[TRUNCATED — file exceeds {max_bytes:,} bytes; only the first portion is shown]"
        )

    return content


def fs_write(path: Path, content: str) -> str:
    """Write *content* to *path*, creating parent directories as needed.

    Args:
        path: Destination file path.
        content: Text content to write.

    Returns:
        Success message including the resolved path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Successfully wrote {len(content):,} characters to '{path}'."


def fs_edit(path: Path, old_text: str, new_text: str) -> str:
    """Replace the first occurrence of *old_text* in *path* with *new_text*.

    Behaviour:
    - If *old_text* is not found, returns an error message.  When a close
      match (similarity > 0.5) exists in the file, a hint is included.
    - If *old_text* appears more than once, a warning is prepended but only
      the **first** occurrence is replaced.
    - On success, the file is overwritten and a success message is returned.

    Args:
        path: Path to the file to edit.
        old_text: Exact text to search for.
        new_text: Replacement text.

    Returns:
        A human-readable result string (success, warning, or error).

    Raises:
        FileNotFoundError: If *path* does not exist.
        IsADirectoryError: If *path* is a directory, not a file.
    """
    if not path.exists():
        raise FileNotFoundError(f"File does not exist: '{path}'")
    if not path.is_file():
        raise IsADirectoryError(f"Path is a directory, not a file: '{path}'")

    content = path.read_text(encoding="utf-8")

    if old_text not in content:
        hint = _find_closest_match(old_text, content)
        msg = f"Error: text not found in '{path}'."
        if hint:
            msg += f"\nClosest match found: {hint!r}"
        return msg

    count = content.count(old_text)
    prefix = ""
    if count > 1:
        prefix = (
            f"Warning: found {count} occurrences of the search text in '{path}'; "
            "only the first occurrence was replaced.\n"
        )

    new_content = content.replace(old_text, new_text, 1)
    path.write_text(new_content, encoding="utf-8")

    return f"{prefix}Successfully edited '{path}'."


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _find_closest_match(needle: str, haystack: str) -> str | None:
    """Return the substring of *haystack* most similar to *needle*, or None.

    Uses :class:`difflib.SequenceMatcher` to score candidate substrings of
    the same length as *needle*.  Only returns a candidate when the best
    similarity ratio exceeds 0.5.

    Args:
        needle: The text we were looking for.
        haystack: The full file content to search within.

    Returns:
        The best-matching substring, or ``None`` if no candidate is similar
        enough.
    """
    needle_len = len(needle)
    if needle_len == 0 or needle_len > len(haystack):
        return None

    best_ratio = 0.0
    best_candidate: str | None = None

    # Split haystack into lines and check line-length windows to keep this
    # O(lines) rather than O(chars²) for large files.
    lines = haystack.splitlines(keepends=True)
    # Build a list of (start_char, line_text) for a sliding window approach.
    # For simplicity we slide over lines and accumulate chunks.
    accumulated = ""
    for line in lines:
        accumulated += line
        # Check the last needle_len characters of the accumulated buffer.
        candidate = accumulated[-needle_len:] if len(accumulated) >= needle_len else accumulated
        ratio = difflib.SequenceMatcher(None, needle, candidate).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_candidate = candidate

    if best_ratio > 0.5:
        # Truncate long candidates for readability in the hint.
        if best_candidate and len(best_candidate) > 120:
            best_candidate = best_candidate[:120] + "…"
        return best_candidate
    return None
