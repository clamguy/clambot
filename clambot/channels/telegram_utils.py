"""Telegram-specific utilities — MarkdownV2 conversion and message chunking.

These are pure functions (no I/O) suitable for unit testing in isolation.
"""

from __future__ import annotations

import re

__all__ = [
    "convert_to_markdownv2",
    "chunk_text",
]

# Characters that must be escaped in MarkdownV2 (outside code blocks).
_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!\\"


def _escape_mdv2(text: str) -> str:
    """Escape special MarkdownV2 characters in *text*."""
    return re.sub(r"([" + re.escape(_ESCAPE_CHARS) + r"])", r"\\\1", text)


def convert_to_markdownv2(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format.

    Handles:
    - Fenced code blocks (````` ``` ````` → pre-formatted)
    - Inline code (`` ` ``)
    - Bold (``**text**``)
    - Italic (``_text_``)
    - Strikethrough (``~~text~~``)
    - Links (``[text](url)``)
    - Blockquotes (``> text``)
    - Headers (stripped to plain bold text)
    - Bullet lists (``- item`` → ``• item``)
    """
    if not text:
        return ""

    # ── 1. Protect code blocks ────────────────────────────────
    code_blocks: list[tuple[str, str]] = []

    def _save_code_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = m.group(2)
        code_blocks.append((lang, code))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(\w*)\n?([\s\S]*?)```", _save_code_block, text)

    # ── 2. Protect inline code ────────────────────────────────
    inline_codes: list[str] = []

    def _save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _save_inline_code, text)

    # ── 3. Protect links ──────────────────────────────────────
    links: list[tuple[str, str]] = []

    def _save_link(m: re.Match) -> str:
        links.append((m.group(1), m.group(2)))
        return f"\x00LK{len(links) - 1}\x00"

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _save_link, text)

    # ── 4. Headers → bold text ────────────────────────────────
    def _header_to_bold(m: re.Match) -> str:
        return f"\x00BOLDSTART\x00{m.group(1)}\x00BOLDEND\x00"

    text = re.sub(r"^#{1,6}\s+(.+)$", _header_to_bold, text, flags=re.MULTILINE)

    # ── 5. Bold **text** / __text__ ───────────────────────────
    def _save_bold(m: re.Match) -> str:
        return f"\x00BOLDSTART\x00{m.group(1)}\x00BOLDEND\x00"

    text = re.sub(r"\*\*(.+?)\*\*", _save_bold, text)
    text = re.sub(r"__(.+?)__", _save_bold, text)

    # ── 6. Italic _text_ ─────────────────────────────────────
    def _save_italic(m: re.Match) -> str:
        return f"\x00ITALICSTART\x00{m.group(1)}\x00ITALICEND\x00"

    text = re.sub(r"(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])", _save_italic, text)

    # ── 7. Strikethrough ~~text~~ ─────────────────────────────
    def _save_strike(m: re.Match) -> str:
        return f"\x00STRIKESTART\x00{m.group(1)}\x00STRIKEEND\x00"

    text = re.sub(r"~~(.+?)~~", _save_strike, text)

    # ── 8. Blockquotes > text ─────────────────────────────────
    text = re.sub(r"^>\s*(.*)$", lambda m: f"\x00QUOTE\x00{m.group(1)}", text, flags=re.MULTILINE)

    # ── 9. Bullet lists ──────────────────────────────────────
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    # ── 10. Escape remaining text ─────────────────────────────
    text = _escape_mdv2(text)

    # ── 11. Restore formatting tokens ─────────────────────────

    # Bold
    text = text.replace("\x00BOLDSTART\x00", "*").replace("\x00BOLDEND\x00", "*")

    # Italic
    text = text.replace("\x00ITALICSTART\x00", "_").replace("\x00ITALICEND\x00", "_")

    # Strikethrough
    text = text.replace("\x00STRIKESTART\x00", "~").replace("\x00STRIKEEND\x00", "~")

    # Blockquotes
    text = text.replace("\x00QUOTE\x00", ">")

    # Links: [escaped_text](url)
    for i, (link_text, url) in enumerate(links):
        escaped_text = _escape_mdv2(link_text)
        # URL must NOT be escaped in MarkdownV2
        text = text.replace(f"\x00LK{i}\x00", f"[{escaped_text}]({url})")

    # Inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", f"`{code}`")

    # Code blocks
    for i, (lang, code) in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", f"```{lang}\n{code}```")

    return text


def chunk_text(text: str, max_len: int = 4096) -> list[str]:
    """Split *text* into chunks of at most *max_len* characters.

    Split preference order:
    1. Newline boundary
    2. Space boundary
    3. Hard cut at *max_len*
    """
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        window = text[:max_len]

        # Prefer newline boundary
        pos = window.rfind("\n")
        if pos <= 0:
            # Fallback to space boundary
            pos = window.rfind(" ")
        if pos <= 0:
            # Hard cut
            pos = max_len

        chunks.append(text[:pos])
        text = text[pos:].lstrip("\n")  # Strip leading newline from next chunk
        # If we split on space, strip the space
        if text and text[0] == " ":
            text = text[1:]

    return chunks
