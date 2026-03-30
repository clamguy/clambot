"""Tests for clambot.session — Phase 4 Session Management."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from clambot.config.schema import CompactionConfig
from clambot.providers.base import LLMResponse
from clambot.session import (
    SessionManager,
    SessionTurn,
    decode_session_key,
    encode_session_key,
    maybe_auto_compact_session,
    turns_to_llm_history,
)
from clambot.session.key import find_legacy_path

# ---------------------------------------------------------------------------
# Key encoding / decoding
# ---------------------------------------------------------------------------


class TestSessionKey:
    def test_roundtrip_simple(self) -> None:
        """Plain strings survive an encode → decode roundtrip."""
        key = "telegram:12345"
        assert decode_session_key(encode_session_key(key)) == key

    def test_roundtrip_with_colon(self) -> None:
        """Keys containing ':' survive a roundtrip without corruption."""
        key = "telegram:99999"
        assert decode_session_key(encode_session_key(key)) == key

    def test_roundtrip_various_keys(self) -> None:
        """Multiple key formats all survive encode → decode."""
        keys = [
            "cli:room1",
            "telegram:12345",
            "slack:C01234567:U09876543",
            "simple",
        ]
        for key in keys:
            assert decode_session_key(encode_session_key(key)) == key, key

    def test_encoded_is_url_safe(self) -> None:
        """Encoded key must not contain '+', '/', or '=' characters."""
        key = "telegram:12345"
        encoded = encode_session_key(key)
        assert "+" not in encoded
        assert "/" not in encoded
        assert "=" not in encoded

    def test_encoded_is_alphanumeric_and_dash_underscore(self) -> None:
        """Encoded key contains only URL-safe base64 characters."""
        key = "telegram:12345"
        encoded = encode_session_key(key)
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", encoded), f"Unexpected chars in {encoded!r}"

    def test_different_keys_produce_different_encodings(self) -> None:
        """Two distinct keys must not collide after encoding."""
        assert encode_session_key("telegram:1") != encode_session_key("telegram:2")


# ---------------------------------------------------------------------------
# Legacy path detection
# ---------------------------------------------------------------------------


class TestFindLegacyPath:
    def test_finds_legacy_file(self, tmp_path: Path) -> None:
        """find_legacy_path returns the path when a legacy file exists."""
        key = "telegram:12345"
        legacy_name = key.replace(":", "_") + ".jsonl"
        legacy_file = tmp_path / legacy_name
        legacy_file.write_text("")

        result = find_legacy_path(tmp_path, key)

        assert result == legacy_file

    def test_returns_none_when_no_legacy_file(self, tmp_path: Path) -> None:
        """find_legacy_path returns None when no legacy file is present."""
        result = find_legacy_path(tmp_path, "telegram:12345")
        assert result is None

    def test_legacy_migration_on_load(self, tmp_path: Path) -> None:
        """SessionManager reads a legacy-named file when the canonical file is absent."""
        key = "telegram:12345"
        legacy_name = key.replace(":", "_") + ".jsonl"
        legacy_file = tmp_path / "sessions" / legacy_name
        legacy_file.parent.mkdir(parents=True)

        turn_data = {
            "role": "user",
            "content": "hello from legacy",
            "timestamp": 1_700_000_000.0,
            "metadata": {},
        }
        legacy_file.write_text(json.dumps(turn_data) + "\n")

        manager = SessionManager(tmp_path)
        turns = manager.load_history(key)

        assert len(turns) == 1
        assert turns[0].content == "hello from legacy"
        assert turns[0].role == "user"


# ---------------------------------------------------------------------------
# JSONL append + reload consistency
# ---------------------------------------------------------------------------


class TestSessionManagerPersistence:
    def test_append_and_reload(self, tmp_path: Path) -> None:
        """Turns appended in one manager instance are visible after reload."""
        key = "telegram:42"
        manager = SessionManager(tmp_path)

        manager.append_turn(key, "user", "first message")
        manager.append_turn(key, "assistant", "first reply")
        manager.append_turn(key, "user", "second message")

        # Reload from a fresh manager (no cache)
        fresh = SessionManager(tmp_path)
        turns = fresh.load_history(key)

        assert len(turns) == 3
        assert turns[0].role == "user"
        assert turns[0].content == "first message"
        assert turns[1].role == "assistant"
        assert turns[1].content == "first reply"
        assert turns[2].content == "second message"

    def test_order_preserved_across_reload(self, tmp_path: Path) -> None:
        """Turn order is preserved exactly after a disk round-trip."""
        key = "cli:order-test"
        manager = SessionManager(tmp_path)

        contents = [f"turn-{i}" for i in range(10)]
        for i, content in enumerate(contents):
            role = "user" if i % 2 == 0 else "assistant"
            manager.append_turn(key, role, content)

        fresh = SessionManager(tmp_path)
        turns = fresh.load_history(key)

        assert [t.content for t in turns] == contents

    def test_metadata_persisted(self, tmp_path: Path) -> None:
        """Turn metadata survives a disk round-trip."""
        key = "cli:meta-test"
        manager = SessionManager(tmp_path)
        manager.append_turn(key, "tool", "result", metadata={"tool_call_id": "abc123"})

        fresh = SessionManager(tmp_path)
        turns = fresh.load_history(key)

        assert turns[0].metadata["tool_call_id"] == "abc123"

    def test_empty_session_returns_empty_list(self, tmp_path: Path) -> None:
        """Loading a session that has never been written returns []."""
        manager = SessionManager(tmp_path)
        turns = manager.load_history("nonexistent:session")
        assert turns == []

    def test_metadata_lines_skipped(self, tmp_path: Path) -> None:
        """Lines with _type='metadata' are silently skipped during load."""
        key = "cli:skip-meta"
        manager = SessionManager(tmp_path)
        # Manually write a metadata line followed by a real turn
        path = manager._session_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            fh.write(json.dumps({"_type": "metadata", "created_at": 0}) + "\n")
            fh.write(
                json.dumps(
                    {"role": "user", "content": "real turn", "timestamp": 0.0, "metadata": {}}
                )
                + "\n"
            )

        turns = manager.load_history(key)
        assert len(turns) == 1
        assert turns[0].content == "real turn"

    def test_malformed_lines_skipped_gracefully(self, tmp_path: Path) -> None:
        """Malformed JSON lines are skipped without raising an exception."""
        key = "cli:bad-json"
        manager = SessionManager(tmp_path)
        path = manager._session_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            fh.write("not valid json\n")
            fh.write(
                json.dumps(
                    {"role": "user", "content": "good turn", "timestamp": 0.0, "metadata": {}}
                )
                + "\n"
            )

        turns = manager.load_history(key)
        assert len(turns) == 1
        assert turns[0].content == "good turn"


# ---------------------------------------------------------------------------
# reset_session
# ---------------------------------------------------------------------------


class TestResetSession:
    def test_reset_clears_cache(self, tmp_path: Path) -> None:
        """reset_session removes the key from the in-memory cache."""
        key = "telegram:reset-test"
        manager = SessionManager(tmp_path)
        manager.append_turn(key, "user", "hello")

        assert key in manager._cache

        manager.reset_session(key)

        assert key not in manager._cache

    def test_reset_does_not_delete_jsonl(self, tmp_path: Path) -> None:
        """reset_session leaves the JSONL file on disk intact."""
        key = "telegram:persist-test"
        manager = SessionManager(tmp_path)
        manager.append_turn(key, "user", "hello")

        jsonl_path = manager._session_path(key)
        assert jsonl_path.exists()

        manager.reset_session(key)

        assert jsonl_path.exists(), "JSONL file must not be deleted by reset_session"

    def test_reset_then_reload_reads_from_disk(self, tmp_path: Path) -> None:
        """After reset, load_history re-reads from disk and returns the same turns."""
        key = "telegram:reload-after-reset"
        manager = SessionManager(tmp_path)
        manager.append_turn(key, "user", "persisted turn")

        manager.reset_session(key)
        turns = manager.load_history(key)

        assert len(turns) == 1
        assert turns[0].content == "persisted turn"

    def test_reset_nonexistent_key_is_noop(self, tmp_path: Path) -> None:
        """reset_session on an unknown key does not raise."""
        manager = SessionManager(tmp_path)
        manager.reset_session("does:not:exist")  # must not raise


# ---------------------------------------------------------------------------
# turns_to_llm_history
# ---------------------------------------------------------------------------


class TestTurnsToLlmHistory:
    def test_basic_conversion(self) -> None:
        """Standard user/assistant turns are converted to role+content dicts."""
        turns = [
            SessionTurn(role="user", content="hello"),
            SessionTurn(role="assistant", content="hi there"),
        ]
        result = turns_to_llm_history(turns)

        assert result == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

    def test_tool_turn_includes_tool_call_id_and_name(self) -> None:
        """Tool turns include tool_call_id and name from metadata when present."""
        turns = [
            SessionTurn(
                role="tool",
                content="42",
                metadata={"tool_call_id": "call_abc", "name": "calculator"},
            )
        ]
        result = turns_to_llm_history(turns)

        assert result[0]["tool_call_id"] == "call_abc"
        assert result[0]["name"] == "calculator"
        assert result[0]["role"] == "tool"
        assert result[0]["content"] == "42"

    def test_tool_turn_without_metadata_fields(self) -> None:
        """Tool turns without tool_call_id/name in metadata omit those keys."""
        turns = [SessionTurn(role="tool", content="result")]
        result = turns_to_llm_history(turns)

        assert "tool_call_id" not in result[0]
        assert "name" not in result[0]

    def test_system_turn_no_extra_fields(self) -> None:
        """System turns are not given tool-specific fields even if metadata exists."""
        turns = [
            SessionTurn(
                role="system",
                content="You are helpful.",
                metadata={"tool_call_id": "should-not-appear"},
            )
        ]
        result = turns_to_llm_history(turns)

        assert "tool_call_id" not in result[0]

    def test_empty_turns_returns_empty_list(self) -> None:
        """An empty turns list produces an empty messages list."""
        assert turns_to_llm_history([]) == []

    def test_preserves_order(self) -> None:
        """Output order matches input order."""
        turns = [SessionTurn(role="user", content=str(i)) for i in range(5)]
        result = turns_to_llm_history(turns)
        assert [m["content"] for m in result] == ["0", "1", "2", "3", "4"]


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def _make_provider(summary: str = "Summary of older turns.") -> MagicMock:
    """Return a mock LLMProvider whose acomplete returns *summary*."""
    provider = MagicMock()
    provider.acomplete = AsyncMock(return_value=LLMResponse(content=summary))
    return provider


def _big_content(chars: int) -> str:
    """Return a string of *chars* 'x' characters."""
    return "x" * chars


class TestCompaction:
    @pytest.mark.asyncio
    async def test_compaction_triggered_above_threshold(self, tmp_path: Path) -> None:
        """Compaction runs and injects a summary turn when above the token threshold."""
        key = "telegram:compact-me"
        manager = SessionManager(tmp_path)

        # 6 turns × 10 000 chars = 60 000 chars ≈ 15 000 tokens
        # With max_context_tokens=10 000 and target_ratio=0.5 → threshold=5 000 tokens
        for i in range(6):
            role = "user" if i % 2 == 0 else "assistant"
            manager.append_turn(key, role, _big_content(10_000))

        config = CompactionConfig(
            enabled=True,
            target_ratio=0.5,
            keep_recent_turns=2,
            summary_max_tokens=200,
        )
        provider = _make_provider("Older turns summarised here.")

        compacted = await maybe_auto_compact_session(
            manager, key, config, provider, max_context_tokens=10_000
        )

        assert compacted is True
        provider.acomplete.assert_awaited_once()

        turns = manager.load_history(key)
        # 1 summary + 2 recent
        assert len(turns) == 3
        assert turns[0].role == "system"
        assert "AUTO-COMPACTION SUMMARY" in turns[0].content
        assert "Older turns summarised here." in turns[0].content
        assert turns[0].metadata.get("_type") == "compaction_summary"

    @pytest.mark.asyncio
    async def test_compaction_skipped_below_threshold(self, tmp_path: Path) -> None:
        """Compaction is not triggered when the history is within the token budget."""
        key = "telegram:small-session"
        manager = SessionManager(tmp_path)

        # 2 turns × 10 chars = 20 chars ≈ 5 tokens — well below any threshold
        manager.append_turn(key, "user", "hi")
        manager.append_turn(key, "assistant", "hello")

        config = CompactionConfig(enabled=True, target_ratio=0.5, keep_recent_turns=4)
        provider = _make_provider()

        compacted = await maybe_auto_compact_session(
            manager, key, config, provider, max_context_tokens=100_000
        )

        assert compacted is False
        provider.acomplete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_compaction_disabled_by_config(self, tmp_path: Path) -> None:
        """Compaction is skipped entirely when config.enabled is False."""
        key = "telegram:disabled"
        manager = SessionManager(tmp_path)
        for _i in range(10):
            manager.append_turn(key, "user", _big_content(10_000))

        config = CompactionConfig(enabled=False)
        provider = _make_provider()

        compacted = await maybe_auto_compact_session(
            manager, key, config, provider, max_context_tokens=1_000
        )

        assert compacted is False
        provider.acomplete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_compaction_summary_persisted_to_disk(self, tmp_path: Path) -> None:
        """The compaction summary turn is appended to the JSONL file."""
        key = "telegram:persist-compact"
        manager = SessionManager(tmp_path)

        for _i in range(6):
            manager.append_turn(key, "user", _big_content(10_000))

        config = CompactionConfig(
            enabled=True,
            target_ratio=0.5,
            keep_recent_turns=2,
            summary_max_tokens=200,
        )
        provider = _make_provider("Persisted summary.")

        await maybe_auto_compact_session(manager, key, config, provider, max_context_tokens=10_000)

        # Read the JSONL file directly and look for the summary line
        path = manager._session_path(key)
        lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        summary_lines = [
            l for l in lines if l.get("metadata", {}).get("_type") == "compaction_summary"
        ]
        assert len(summary_lines) == 1
        assert "AUTO-COMPACTION SUMMARY" in summary_lines[0]["content"]

    @pytest.mark.asyncio
    async def test_compaction_keeps_correct_recent_turns(self, tmp_path: Path) -> None:
        """The most-recent keep_recent_turns turns are preserved verbatim."""
        key = "telegram:keep-recent"
        manager = SessionManager(tmp_path)

        contents = [f"turn-content-{i}" for i in range(8)]
        for _i, content in enumerate(contents):
            manager.append_turn(key, "user", content + _big_content(5_000))

        config = CompactionConfig(
            enabled=True,
            target_ratio=0.5,
            keep_recent_turns=3,
            summary_max_tokens=200,
        )
        provider = _make_provider("Summary.")

        await maybe_auto_compact_session(manager, key, config, provider, max_context_tokens=10_000)

        turns = manager.load_history(key)
        # summary + 3 recent
        assert len(turns) == 4
        # The last 3 original turns should be the recent ones
        for i, turn in enumerate(turns[1:], start=5):
            assert f"turn-content-{i}" in turn.content

    @pytest.mark.asyncio
    async def test_compaction_empty_session_returns_false(self, tmp_path: Path) -> None:
        """Compaction on an empty session returns False without calling the provider."""
        key = "telegram:empty"
        manager = SessionManager(tmp_path)

        config = CompactionConfig(enabled=True, target_ratio=0.5)
        provider = _make_provider()

        compacted = await maybe_auto_compact_session(
            manager, key, config, provider, max_context_tokens=1_000
        )

        assert compacted is False
        provider.acomplete.assert_not_awaited()
