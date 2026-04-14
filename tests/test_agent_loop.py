"""Tests for Phase 8 — Agent Loop.

Tests:
- normalize_request NFKC + punctuation stripping
- Pre-selection: exact match skips LLM selector call
- Selector JSON parse failure → retry with repair prompt
- Self-fix loop: SELF_FIX decision re-enters with correct prefix (max 3)
- Clam promotion on ACCEPT: file moved from build/ to clams/
- Fire-and-forget: _background_extract_durable_facts scheduled as task
- ClamRegistry: catalog scan and clam loading
- WorkspaceClamPersistenceWriter: build/promote workflow
- Memory store: recall, save, append history, search
- GenerationAdapter: JSON, code block, and raw parsing
- AnalysisAdapter: JSON and fallback parsing
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clambot.agent.analysis_trace import AnalysisTraceBuilder
from clambot.agent.chat_mode import ChatModeFallbackResponder
from clambot.agent.clams import ClamRegistry, ClamSummary, parse_clam_md
from clambot.agent.context import ContextBuilder
from clambot.agent.final_response import select_final_response
from clambot.agent.generation_adapter import (
    GenerationResult,
    normalize_generation_response,
)
from clambot.agent.generation_grounding import apply_grounding_rules
from clambot.agent.loop import AgentLoop, AgentResult
from clambot.agent.post_runtime_analysis import PostRuntimeAnalysisDecision
from clambot.agent.post_runtime_analysis_adapter import (
    AnalysisResult,
    normalize_analysis_response,
)
from clambot.agent.request_normalization import normalize_request
from clambot.agent.selector import ProviderBackedClamSelector, SelectionResult
from clambot.agent.workspace_clam_writer import WorkspaceClamPersistenceWriter
from clambot.memory.store import (
    memory_append_history,
    memory_recall,
    memory_save,
    memory_search_history,
)
from clambot.providers.base import LLMResponse

# ---------------------------------------------------------------------------
# Request normalization tests
# ---------------------------------------------------------------------------


class TestNormalizeRequest:
    """Tests for normalize_request()."""

    def test_nfkc_normalization(self) -> None:
        """NFKC normalization converts compatibility characters."""
        # \uff37 is fullwidth 'W', NFKC normalizes to 'W'
        result = normalize_request("\uff37hat")
        assert result == "what"

    def test_punctuation_stripped(self) -> None:
        """Punctuation is removed from the request."""
        result = normalize_request("What's the weather?")
        assert result == "whats the weather"

    def test_lowercase(self) -> None:
        """Request is lowercased."""
        result = normalize_request("HELLO WORLD")
        assert result == "hello world"

    def test_whitespace_collapsed(self) -> None:
        """Multiple whitespace is collapsed to single spaces."""
        result = normalize_request("  hello   world  ")
        assert result == "hello world"

    def test_combined_normalization(self) -> None:
        """All normalization steps apply together."""
        result = normalize_request("  What's the Weather?!  ")
        assert result == "whats the weather"

    def test_empty_string(self) -> None:
        """Empty string stays empty."""
        assert normalize_request("") == ""

    def test_only_punctuation(self) -> None:
        """String of only punctuation becomes empty."""
        assert normalize_request("!@#$%") == ""


# ---------------------------------------------------------------------------
# ClamRegistry tests
# ---------------------------------------------------------------------------


class TestClamRegistry:
    """Tests for ClamRegistry."""

    def test_empty_catalog(self, tmp_path: Path) -> None:
        """Empty workspace returns empty catalog."""
        registry = ClamRegistry(tmp_path)
        assert registry.get_catalog() == []

    def test_catalog_scan(self, tmp_path: Path) -> None:
        """Scans clams directory and returns summaries."""
        clams_dir = tmp_path / "clams" / "test-clam"
        clams_dir.mkdir(parents=True)

        clam_md = clams_dir / "CLAM.md"
        clam_md.write_text(
            "---\n"
            "description: Test clam\n"
            "declared_tools:\n"
            "  - fs\n"
            "  - http_request\n"
            "source_request: list files\n"
            "---\n"
            "\nA test clam.\n"
        )

        registry = ClamRegistry(tmp_path)
        catalog = registry.get_catalog()

        assert len(catalog) == 1
        assert catalog[0].name == "test-clam"
        assert catalog[0].description == "Test clam"
        assert catalog[0].declared_tools == ["fs", "http_request"]
        assert catalog[0].source_request == "list files"

    def test_load_clam(self, tmp_path: Path) -> None:
        """Load a full clam with script and metadata."""
        clam_dir = tmp_path / "clams" / "my-clam"
        clam_dir.mkdir(parents=True)

        (clam_dir / "CLAM.md").write_text(
            "---\ndescription: My clam\nlanguage: javascript\ndeclared_tools:\n  - fs\n---\n"
        )
        (clam_dir / "run.js").write_text('console.log("hello");')

        registry = ClamRegistry(tmp_path)
        clam = registry.load("my-clam")

        assert clam is not None
        assert clam.name == "my-clam"
        assert clam.script == 'console.log("hello");'
        assert clam.declared_tools == ["fs"]
        assert clam.language == "javascript"

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        """Loading a nonexistent clam returns None."""
        registry = ClamRegistry(tmp_path)
        assert registry.load("nonexistent") is None

    def test_cache_invalidation(self, tmp_path: Path) -> None:
        """Cache invalidation forces rescan."""
        registry = ClamRegistry(tmp_path)
        catalog1 = registry.get_catalog()
        assert len(catalog1) == 0

        # Add a clam
        clam_dir = tmp_path / "clams" / "new-clam"
        clam_dir.mkdir(parents=True)
        (clam_dir / "CLAM.md").write_text("---\ndescription: New\n---\n")

        # Still cached
        assert len(registry.get_catalog()) == 0

        # Invalidate
        registry.invalidate_cache()
        assert len(registry.get_catalog()) == 1


# ---------------------------------------------------------------------------
# CLAM.md parsing tests
# ---------------------------------------------------------------------------


class TestParseClamMd:
    """Tests for parse_clam_md()."""

    def test_parse_full_frontmatter(self) -> None:
        content = (
            "---\n"
            "description: My description\n"
            "language: javascript\n"
            "reusable: true\n"
            "declared_tools:\n"
            "  - fs\n"
            "  - http_request\n"
            "---\n"
            "\nBody text.\n"
        )
        result = parse_clam_md(content)
        assert result["description"] == "My description"
        assert result["language"] == "javascript"
        assert result["reusable"] is True
        assert result["declared_tools"] == ["fs", "http_request"]

    def test_parse_no_frontmatter(self) -> None:
        result = parse_clam_md("Just some text")
        assert result == {}

    def test_body_as_description(self) -> None:
        """Body text used as description if not in frontmatter."""
        content = "---\nlanguage: javascript\n---\n\nBody description here.\n"
        result = parse_clam_md(content)
        assert result.get("description") == "Body description here."

    def test_parse_clam_md_json_inputs(self) -> None:
        """inputs: {"a": 1, "b": 2} is parsed as a dict, not a string."""
        content = '---\ndescription: Test clam\ninputs: {"a": 1, "b": 2}\n---\n'
        result = parse_clam_md(content)
        assert isinstance(result["inputs"], dict), (
            f"Expected inputs to be a dict, got {type(result['inputs'])!r}: {result['inputs']!r}"
        )
        assert result["inputs"] == {"a": 1, "b": 2}, (
            f"Expected {{'a': 1, 'b': 2}}, got {result['inputs']!r}"
        )


# ---------------------------------------------------------------------------
# WorkspaceClamPersistenceWriter tests
# ---------------------------------------------------------------------------


class TestWorkspaceClamWriter:
    """Tests for WorkspaceClamPersistenceWriter."""

    def test_write_to_build(self, tmp_path: Path) -> None:
        """Writing to build creates script and CLAM.md."""
        writer = WorkspaceClamPersistenceWriter(tmp_path)
        path = writer.write_to_build(
            "test-clam",
            'console.log("test");',
            "---\ndescription: test\n---\n",
        )

        assert (path / "run.js").exists()
        assert (path / "CLAM.md").exists()
        assert (path / "run.js").read_text() == 'console.log("test");'

    def test_promote(self, tmp_path: Path) -> None:
        """Promoting moves clam from build/ to clams/."""
        writer = WorkspaceClamPersistenceWriter(tmp_path)
        writer.write_to_build("test-clam", "code", "metadata")

        target = writer.promote("test-clam")
        assert target is not None
        assert target == tmp_path / "clams" / "test-clam"
        assert (target / "run.js").exists()
        assert not (tmp_path / "build" / "test-clam").exists()

    def test_promote_replaces_existing(self, tmp_path: Path) -> None:
        """Promoting replaces existing clam in clams/."""
        writer = WorkspaceClamPersistenceWriter(tmp_path)

        # Create existing clam
        existing = tmp_path / "clams" / "test-clam"
        existing.mkdir(parents=True)
        (existing / "run.js").write_text("old code")

        # Write new version to build
        writer.write_to_build("test-clam", "new code", "metadata")
        writer.promote("test-clam")

        assert (existing / "run.js").read_text() == "new code"

    def test_promote_nonexistent(self, tmp_path: Path) -> None:
        """Promoting nonexistent build returns None."""
        writer = WorkspaceClamPersistenceWriter(tmp_path)
        assert writer.promote("nonexistent") is None

    def test_generate_clam_name(self) -> None:
        """Slug generation from request text."""
        assert (
            WorkspaceClamPersistenceWriter.generate_clam_name("What's the weather?")
            == "what-s-the-weather"
        )
        assert (
            WorkspaceClamPersistenceWriter.generate_clam_name("list all files") == "list-all-files"
        )

    def test_generate_clam_name_truncation(self) -> None:
        """Long names are truncated to 60 characters."""
        long_request = "a" * 100
        name = WorkspaceClamPersistenceWriter.generate_clam_name(long_request)
        assert len(name) <= 60


# ---------------------------------------------------------------------------
# Pre-selection tests
# ---------------------------------------------------------------------------


class TestPreSelection:
    """Tests for selector pre-selection (exact match)."""

    @pytest.mark.asyncio
    async def test_exact_match_skips_llm(self) -> None:
        """Pre-selection exact match returns immediately without LLM call."""
        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock()

        selector = ProviderBackedClamSelector(provider=mock_provider)

        catalog = [
            ClamSummary(
                name="list-files",
                description="Lists files",
                declared_tools=["fs"],
                source_request="list files",
            ),
        ]

        result = await selector.select(
            message="list files",
            clam_catalog=catalog,
        )

        assert result.decision == "select_existing"
        assert result.clam_id == "list-files"
        assert result.reason == "Pre-selection exact match"
        # LLM was NOT called
        mock_provider.acomplete.assert_not_called()

    @pytest.mark.asyncio
    async def test_normalized_match(self) -> None:
        """Pre-selection matches after normalization."""
        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock()

        selector = ProviderBackedClamSelector(provider=mock_provider)

        catalog = [
            ClamSummary(
                name="list-files",
                source_request="List Files!",
            ),
        ]

        result = await selector.select(
            message="  list  files  ",
            clam_catalog=catalog,
        )

        assert result.decision == "select_existing"
        assert result.clam_id == "list-files"
        mock_provider.acomplete.assert_not_called()


# ---------------------------------------------------------------------------
# Selector JSON parsing tests
# ---------------------------------------------------------------------------


class TestSelectorParsing:
    """Tests for selector LLM response parsing."""

    @pytest.mark.asyncio
    async def test_valid_json_response(self) -> None:
        """Valid JSON response is parsed correctly."""
        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    {
                        "decision": "generate_new",
                        "clam_id": None,
                        "reason": "New request",
                        "chat_response": "",
                    }
                )
            )
        )

        selector = ProviderBackedClamSelector(provider=mock_provider)
        result = await selector.select("do something new")

        assert result.decision == "generate_new"
        assert result.reason == "New request"

    @pytest.mark.asyncio
    async def test_bad_json_retries(self) -> None:
        """Bad JSON triggers repair prompt retry."""
        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(
            side_effect=[
                LLMResponse(content="not json at all"),
                LLMResponse(
                    content=json.dumps(
                        {
                            "decision": "chat",
                            "clam_id": None,
                            "reason": "Greeting",
                            "chat_response": "Hello!",
                        }
                    )
                ),
            ]
        )

        selector = ProviderBackedClamSelector(provider=mock_provider, retries=1)
        result = await selector.select("hello")

        assert result.decision == "chat"
        assert result.chat_response == "Hello!"
        assert mock_provider.acomplete.call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self) -> None:
        """After all retries, falls back to generate_new."""
        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(return_value=LLMResponse(content="invalid"))

        selector = ProviderBackedClamSelector(provider=mock_provider, retries=1)
        result = await selector.select("something")

        assert result.decision == "generate_new"
        assert "failed" in result.reason.lower()


# ---------------------------------------------------------------------------
# Generation adapter tests
# ---------------------------------------------------------------------------


class TestGenerationAdapter:
    """Tests for normalize_generation_response()."""

    def test_json_format(self) -> None:
        """JSON response with script field is parsed."""
        raw = json.dumps(
            {
                "script": 'console.log("hello");',
                "declared_tools": ["fs"],
                "metadata": {"reusable": True},
            }
        )
        result = normalize_generation_response(raw)
        assert result.script == 'console.log("hello");'
        assert result.declared_tools == ["fs"]
        assert result.metadata.get("reusable") is True

    def test_code_block_format(self) -> None:
        """Markdown code block is extracted."""
        raw = "Here is the code:\n```javascript\nconsole.log('hi');\n```\n"
        result = normalize_generation_response(raw)
        assert result.script == "console.log('hi');"

    def test_raw_javascript(self) -> None:
        """Raw JavaScript is used as-is."""
        raw = "const x = 42;\nconsole.log(x);"
        result = normalize_generation_response(raw)
        assert result.script == raw

    def test_json_in_code_fence(self) -> None:
        """JSON wrapped in code fences is parsed."""
        inner = json.dumps(
            {
                "script": "return 42;",
                "declared_tools": [],
            }
        )
        raw = f"```json\n{inner}\n```"
        result = normalize_generation_response(raw)
        assert result.script == "return 42;"


# ---------------------------------------------------------------------------
# Generation grounding tests
# ---------------------------------------------------------------------------


class TestGenerationGrounding:
    """Tests for apply_grounding_rules()."""

    def test_removes_workspace_prefix(self) -> None:
        """Removes /workspace/ prefix from paths."""
        result = GenerationResult(script='await fs({path: "/workspace/test.txt"});')
        grounded = apply_grounding_rules(result)
        assert "/workspace/" not in grounded.script
        assert 'path: "test.txt"' in grounded.script

    def test_forces_javascript_language(self) -> None:
        """Forces language to javascript."""
        result = GenerationResult(script="code", language="python")
        grounded = apply_grounding_rules(result)
        assert grounded.language == "javascript"

    def test_strips_code_fences(self) -> None:
        """Strips markdown code fences from script."""
        result = GenerationResult(script="```javascript\ncode here\n```")
        grounded = apply_grounding_rules(result)
        assert grounded.script == "code here"


# ---------------------------------------------------------------------------
# Analysis adapter tests
# ---------------------------------------------------------------------------


class TestAnalysisAdapter:
    """Tests for normalize_analysis_response()."""

    def test_accept_json(self) -> None:
        """Valid ACCEPT JSON is parsed."""
        raw = json.dumps(
            {
                "decision": "ACCEPT",
                "output": "Result here",
                "reason": "Looks good",
            }
        )
        result = normalize_analysis_response(raw)
        assert result.decision == PostRuntimeAnalysisDecision.ACCEPT
        assert result.output == "Result here"

    def test_self_fix_json(self) -> None:
        """SELF_FIX JSON is parsed."""
        raw = json.dumps(
            {
                "decision": "SELF_FIX",
                "fix_instructions": "Fix the error",
            }
        )
        result = normalize_analysis_response(raw)
        assert result.decision == PostRuntimeAnalysisDecision.SELF_FIX
        assert result.fix_instructions == "Fix the error"

    def test_fallback_self_fix(self) -> None:
        """Non-JSON with SELF_FIX keyword triggers fallback."""
        raw = "The script needs SELF_FIX because of the error"
        result = normalize_analysis_response(raw)
        assert result.decision == PostRuntimeAnalysisDecision.SELF_FIX

    def test_fallback_accept_keyword(self) -> None:
        """Non-JSON with ACCEPT keyword is detected."""
        raw = "ACCEPT - the output correctly answers the question."
        result = normalize_analysis_response(raw)
        assert result.decision == PostRuntimeAnalysisDecision.ACCEPT
        assert result.output == raw

    def test_fallback_no_keyword_rejects(self) -> None:
        """Non-JSON without any decision keyword defaults to REJECT."""
        raw = "Looks good, everything worked fine."
        result = normalize_analysis_response(raw)
        assert result.decision == PostRuntimeAnalysisDecision.REJECT
        assert result.output == raw

    def test_json_embedded_in_prose(self) -> None:
        """JSON object embedded in prose text is extracted."""
        raw = (
            'Here is my analysis:\n{"decision": "ACCEPT", "output": "The result", "reason": "good"}'
        )
        result = normalize_analysis_response(raw)
        assert result.decision == PostRuntimeAnalysisDecision.ACCEPT
        assert result.output == "The result"


# ---------------------------------------------------------------------------
# Final response tests
# ---------------------------------------------------------------------------


class TestFinalResponse:
    """Tests for select_final_response()."""

    def test_accept_with_output(self) -> None:
        """ACCEPT analysis with output uses analysis output."""
        analysis = AnalysisResult(
            decision=PostRuntimeAnalysisDecision.ACCEPT,
            output="Clean output",
        )
        runtime = MagicMock(output="Raw output", error="")
        result = select_final_response(analysis, runtime)
        assert result == "Clean output"

    def test_fallback_to_runtime(self) -> None:
        """Falls back to runtime output if no analysis."""
        runtime = MagicMock(output="Runtime output", error="")
        result = select_final_response(None, runtime)
        assert result == "Runtime output"

    def test_error_message(self) -> None:
        """Shows error message if no output."""
        runtime = MagicMock(output="", error="Something broke")
        result = select_final_response(None, runtime)
        assert "Something broke" in result


# ---------------------------------------------------------------------------
# Memory store tests
# ---------------------------------------------------------------------------


class TestMemoryStore:
    """Tests for memory/store.py."""

    def test_recall_empty(self, tmp_path: Path) -> None:
        """Recall returns empty string when no memory file."""
        assert memory_recall(tmp_path) == ""

    def test_save_and_recall(self, tmp_path: Path) -> None:
        """Save then recall returns the saved content."""
        memory_save(tmp_path, "Important fact")
        assert memory_recall(tmp_path) == "Important fact"

    def test_append_history(self, tmp_path: Path) -> None:
        """Append to history log."""
        memory_append_history(tmp_path, "First entry")
        memory_append_history(tmp_path, "Second entry")

        history_path = tmp_path / "memory" / "HISTORY.md"
        content = history_path.read_text()
        assert "First entry" in content
        assert "Second entry" in content

    def test_search_history(self, tmp_path: Path) -> None:
        """Search finds matching entries."""
        memory_append_history(tmp_path, "Meeting about project X")
        memory_append_history(tmp_path, "Lunch break")
        memory_append_history(tmp_path, "Review project X progress")

        results = memory_search_history(tmp_path, "project X")
        assert len(results) == 2

    def test_search_no_results(self, tmp_path: Path) -> None:
        """Search with no matches returns empty list."""
        memory_append_history(tmp_path, "Some entry")
        results = memory_search_history(tmp_path, "nonexistent")
        assert results == []

    def test_search_limit(self, tmp_path: Path) -> None:
        """Search respects limit."""
        for i in range(20):
            memory_append_history(tmp_path, f"Entry {i} with keyword")
        results = memory_search_history(tmp_path, "keyword", limit=5)
        assert len(results) == 5


# ---------------------------------------------------------------------------
# Analysis trace tests
# ---------------------------------------------------------------------------


class TestAnalysisTrace:
    """Tests for AnalysisTraceBuilder."""

    def test_record_and_summary(self) -> None:
        """Records entries and produces summary."""
        trace = AnalysisTraceBuilder()
        trace.record(0, "SELF_FIX", reason="Error in script")
        trace.record(1, "ACCEPT", reason="Fixed")

        summary = trace.summary()
        assert summary["total_attempts"] == 2
        assert summary["final_decision"] == "ACCEPT"
        assert len(summary["entries"]) == 2

    def test_last_decision(self) -> None:
        """last_decision returns the most recent decision."""
        trace = AnalysisTraceBuilder()
        assert trace.last_decision is None
        trace.record(0, "SELF_FIX")
        assert trace.last_decision == "SELF_FIX"


# ---------------------------------------------------------------------------
# Self-fix loop test
# ---------------------------------------------------------------------------


class TestSelfFixLoop:
    """Tests for the self-fix loop in AgentLoop."""

    @pytest.mark.asyncio
    async def test_self_fix_retries_max_3(self, tmp_path: Path) -> None:
        """Self-fix loop respects max_self_fix_attempts = 3."""
        # Create workspace structure
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        # Mock provider that always generates code
        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    {
                        "script": "async function run() { return broken(); }",
                        "declared_tools": [],
                        "metadata": {"description": "broken"},
                    }
                )
            )
        )

        # Mock selector that always generates new
        mock_selector = AsyncMock()
        mock_selector.select = AsyncMock(
            return_value=SelectionResult(
                decision="generate_new",
                reason="Test",
            )
        )

        # Mock runtime that always fails
        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=MagicMock(output="", error="SyntaxError", stderr="", timed_out=False)
        )

        # Mock analyzer — always SELF_FIX
        mock_analyzer = AsyncMock()
        mock_analyzer.analyze = AsyncMock(
            return_value=AnalysisResult(
                decision=PostRuntimeAnalysisDecision.SELF_FIX,
                fix_instructions="Fix the syntax",
            )
        )

        from clambot.agent.provider_generation import ProviderBackedClamGenerator

        generator = ProviderBackedClamGenerator(provider=mock_provider)

        # Config with max 3 self-fix attempts
        config = MagicMock()
        config.agents.defaults.max_self_fix_attempts = 3

        loop = AgentLoop(
            selector=mock_selector,
            generator=generator,
            runtime=mock_runtime,
            analyzer=mock_analyzer,
            tool_registry=None,
            context_builder=ContextBuilder(workspace=tmp_path),
            clam_registry=ClamRegistry(tmp_path),
            memory_workspace=tmp_path,
            config=config,
        )

        result = await loop.process_turn("do something")

        assert result.status == "failed"
        assert mock_runtime.execute.call_count == 4  # 1 initial + 3 self-fix retries

    @pytest.mark.asyncio
    async def test_self_fix_prefix_in_regeneration(self, tmp_path: Path) -> None:
        """Self-fix regeneration includes SELF_FIX_RUNTIME prefix."""
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        call_messages: list[list] = []

        async def mock_acomplete(messages, **kwargs):
            call_messages.append(messages)
            return LLMResponse(
                content=json.dumps(
                    {
                        "script": "async function run() { return 'fixed'; }",
                        "declared_tools": [],
                        "metadata": {"description": "fixed"},
                    }
                )
            )

        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(side_effect=mock_acomplete)

        # First call: runtime error. Second call: success
        runtime_results = [
            MagicMock(output="", error="Error!", stderr="err", timed_out=False),
            MagicMock(output="Success!", error="", stderr="", timed_out=False),
        ]
        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(side_effect=runtime_results)

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze = AsyncMock(
            return_value=AnalysisResult(
                decision=PostRuntimeAnalysisDecision.ACCEPT,
                output="Success!",
            )
        )

        from clambot.agent.provider_generation import ProviderBackedClamGenerator

        generator = ProviderBackedClamGenerator(provider=mock_provider)

        config = MagicMock()
        config.agents.defaults.max_self_fix_attempts = 3

        mock_selector = AsyncMock()
        mock_selector.select = AsyncMock(
            return_value=SelectionResult(decision="generate_new", reason="Test")
        )

        loop = AgentLoop(
            selector=mock_selector,
            generator=generator,
            runtime=mock_runtime,
            analyzer=mock_analyzer,
            tool_registry=None,
            context_builder=ContextBuilder(workspace=tmp_path),
            clam_registry=ClamRegistry(tmp_path),
            memory_workspace=tmp_path,
            config=config,
        )

        result = await loop.process_turn("do something")

        assert result.status == "completed"
        # Second generation call should have SELF_FIX_RUNTIME prefix
        assert len(call_messages) >= 2
        second_call = call_messages[1]
        user_msg = [m for m in second_call if m["role"] == "user"]
        assert any("SELF_FIX_RUNTIME" in m["content"] for m in user_msg)
        assert any("Return ONLY a JSON object" in m["content"] for m in user_msg)

    @pytest.mark.asyncio
    async def test_grounding_rejection_self_fix_includes_bad_output(self, tmp_path: Path) -> None:
        """Grounding retries include rejected non-code output in self-fix context."""
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        call_messages: list[list] = []

        first_bad = "Вот краткое содержание видео"

        responses = [
            LLMResponse(content=first_bad),
            LLMResponse(
                content=json.dumps(
                    {
                        "script": 'async function run() { return "ok"; }',
                        "declared_tools": [],
                        "metadata": {"description": "fixed"},
                    }
                )
            ),
        ]

        async def mock_acomplete(messages, **kwargs):
            call_messages.append(messages)
            return responses[len(call_messages) - 1]

        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(side_effect=mock_acomplete)

        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=MagicMock(output="ok", error="", stderr="", timed_out=False)
        )

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze = AsyncMock(
            return_value=AnalysisResult(
                decision=PostRuntimeAnalysisDecision.ACCEPT,
                output="ok",
            )
        )

        from clambot.agent.provider_generation import ProviderBackedClamGenerator

        generator = ProviderBackedClamGenerator(provider=mock_provider)

        config = MagicMock()
        config.agents.defaults.max_self_fix_attempts = 3

        mock_selector = AsyncMock()
        mock_selector.select = AsyncMock(
            return_value=SelectionResult(decision="generate_new", reason="Test")
        )

        loop = AgentLoop(
            selector=mock_selector,
            generator=generator,
            runtime=mock_runtime,
            analyzer=mock_analyzer,
            tool_registry=None,
            context_builder=ContextBuilder(workspace=tmp_path),
            clam_registry=ClamRegistry(tmp_path),
            memory_workspace=tmp_path,
            config=config,
        )

        result = await loop.process_turn("fetch transcript")

        assert result.status == "completed"
        assert mock_provider.acomplete.call_count == 2
        assert mock_runtime.execute.call_count == 1

        second_call = call_messages[1]
        user_msg = [m for m in second_call if m["role"] == "user"]
        assert any("GROUNDING ERROR" in m["content"] for m in user_msg)
        assert any("BAD OUTPUT" in m["content"] for m in user_msg)
        assert any(first_bad in m["content"] for m in user_msg)


class TestTaskPlanningExecution:
    """Tests for planned multi-step execution behavior."""

    def test_direct_tools_only_cron(self) -> None:
        """Only cron is treated as an agent-only direct tool."""
        assert AgentLoop._DIRECT_TOOLS == frozenset({"cron"})

    @pytest.mark.asyncio
    async def test_execute_subtask_drops_top_level_link_context(self, tmp_path: Path) -> None:
        """Execute subtasks should not inherit top-level pre-fetched link context."""
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        loop = AgentLoop(
            selector=AsyncMock(),
            generator=AsyncMock(),
            runtime=AsyncMock(),
            analyzer=AsyncMock(),
            tool_registry=None,
            context_builder=ContextBuilder(workspace=tmp_path),
            clam_registry=ClamRegistry(tmp_path),
            memory_workspace=tmp_path,
            config=MagicMock(),
        )

        mock_single_task = AsyncMock(
            return_value=AgentResult(
                content="raw transcript",
                status="completed",
            )
        )
        loop._execute_single_task = mock_single_task  # type: ignore[method-assign]

        result = await loop._execute_planned_tasks(
            plan=[{"action": "execute", "task": "Fetch transcript from URL"}],
            history=None,
            system_prompt="system",
            link_context="TOP_LEVEL_LINK_CONTEXT",
            on_event=None,
            events=[],
        )

        assert result.status == "completed"
        assert mock_single_task.call_count == 1
        call_kwargs = mock_single_task.call_args.kwargs
        assert call_kwargs["link_context"] == ""

    @pytest.mark.asyncio
    async def test_execute_transform_returns_transformed_output_only(self, tmp_path: Path) -> None:
        """Execute+transform plans should not include raw execute output in final reply."""
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        loop = AgentLoop(
            selector=AsyncMock(),
            generator=AsyncMock(),
            runtime=AsyncMock(),
            analyzer=AsyncMock(),
            tool_registry=None,
            context_builder=ContextBuilder(workspace=tmp_path),
            clam_registry=ClamRegistry(tmp_path),
            memory_workspace=tmp_path,
            config=MagicMock(),
        )

        loop._execute_single_task = AsyncMock(  # type: ignore[method-assign]
            return_value=AgentResult(content="RAW TRANSCRIPT", status="completed")
        )
        loop._handle_transform_action = AsyncMock(  # type: ignore[method-assign]
            return_value=AgentResult(content="SHORT SUMMARY", status="completed")
        )

        result = await loop._execute_planned_tasks(
            plan=[
                {"action": "execute", "task": "Transcribe URL"},
                {"action": "transform", "instruction": "Summarize in Russian"},
            ],
            history=None,
            system_prompt="system",
            link_context="",
            on_event=None,
            events=[],
        )

        assert result.status == "completed"
        assert result.content == "SHORT SUMMARY"

    @pytest.mark.asyncio
    async def test_plan_rewrite_prefers_transcribe_for_youtube_summary(self, tmp_path: Path) -> None:
        """Planner output for YouTube summaries is rewritten to explicit transcribe task."""
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        planned = [
            {
                "action": "execute",
                "task": "Fetch video transcript or content from https://youtu.be/nIqdV91Sdmw",
            },
            {"action": "transform", "instruction": "Summarize the content in Russian"},
        ]

        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(return_value=LLMResponse(content=json.dumps(planned)))

        mock_selector = MagicMock()
        mock_selector._provider = mock_provider

        loop = AgentLoop(
            selector=mock_selector,
            generator=AsyncMock(),
            runtime=AsyncMock(),
            analyzer=AsyncMock(),
            tool_registry=None,
            context_builder=ContextBuilder(workspace=tmp_path),
            clam_registry=ClamRegistry(tmp_path),
            memory_workspace=tmp_path,
            config=MagicMock(),
        )

        plan = await loop._plan_tasks("Summarize in Russian https://youtu.be/nIqdV91Sdmw")

        assert plan[0]["action"] == "execute"
        assert "transcribe tool" in plan[0]["task"].lower()
        assert "https://youtu.be/nIqdV91Sdmw" in plan[0]["task"]
        assert plan[1]["action"] == "transform"

    @pytest.mark.asyncio
    async def test_plan_fallback_rewrite_prefers_transcribe(self, tmp_path: Path) -> None:
        """Planner fallback still rewrites media transcript requests to transcribe task."""
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(side_effect=RuntimeError("planner failed"))

        mock_selector = MagicMock()
        mock_selector._provider = mock_provider

        loop = AgentLoop(
            selector=mock_selector,
            generator=AsyncMock(),
            runtime=AsyncMock(),
            analyzer=AsyncMock(),
            tool_registry=None,
            context_builder=ContextBuilder(workspace=tmp_path),
            clam_registry=ClamRegistry(tmp_path),
            memory_workspace=tmp_path,
            config=MagicMock(),
        )

        plan = await loop._plan_tasks("Please summarize https://youtu.be/nIqdV91Sdmw in Russian")

        assert len(plan) == 1
        assert plan[0]["action"] == "execute"
        assert "transcribe tool" in plan[0]["task"].lower()


# ---------------------------------------------------------------------------
# Clam promotion test
# ---------------------------------------------------------------------------


class TestClamPromotion:
    """Tests for clam promotion on ACCEPT."""

    @pytest.mark.asyncio
    async def test_accept_promotes_clam(self, tmp_path: Path) -> None:
        """ACCEPT decision promotes clam from build/ to clams/."""
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(
            return_value=LLMResponse(
                content=json.dumps(
                    {
                        "script": "console.log('hi');",
                        "declared_tools": [],
                        "metadata": {"description": "test"},
                    }
                )
            )
        )

        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=MagicMock(output="hi", error="", stderr="", timed_out=False)
        )

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze = AsyncMock(
            return_value=AnalysisResult(
                decision=PostRuntimeAnalysisDecision.ACCEPT,
                output="hi",
            )
        )

        from clambot.agent.provider_generation import ProviderBackedClamGenerator

        mock_selector = AsyncMock()
        mock_selector.select = AsyncMock(
            return_value=SelectionResult(decision="generate_new", reason="Test")
        )

        config = MagicMock()
        config.agents.defaults.max_self_fix_attempts = 3

        loop = AgentLoop(
            selector=mock_selector,
            generator=ProviderBackedClamGenerator(provider=mock_provider),
            runtime=mock_runtime,
            analyzer=mock_analyzer,
            tool_registry=None,
            context_builder=ContextBuilder(workspace=tmp_path),
            clam_registry=ClamRegistry(tmp_path),
            memory_workspace=tmp_path,
            config=config,
        )

        result = await loop.process_turn("say hi")

        assert result.status == "completed"
        # Clam should be in clams/ directory now
        clams_dir = tmp_path / "clams"
        promoted = list(clams_dir.iterdir())
        assert len(promoted) >= 1


# ---------------------------------------------------------------------------
# Background fact extraction test
# ---------------------------------------------------------------------------


class TestBackgroundFactExtraction:
    """Tests for fire-and-forget fact extraction."""

    @pytest.mark.asyncio
    async def test_background_task_scheduled(self, tmp_path: Path) -> None:
        """Background fact extraction is scheduled as a task (not awaited)."""
        from clambot.agent.turn_execution import _background_extract_durable_facts

        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(
            return_value=LLMResponse(content='{"facts": ["User likes Python"]}')
        )

        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        # Run the background task directly
        await _background_extract_durable_facts(
            turn={"role": "assistant", "content": "I see you prefer Python!"},
            session_key="test:123",
            provider=mock_provider,
            workspace=tmp_path,
        )

        # Check that memory was updated
        content = memory_recall(tmp_path)
        assert "User likes Python" in content


# ---------------------------------------------------------------------------
# Chat mode tests
# ---------------------------------------------------------------------------


class TestChatMode:
    """Tests for ChatModeFallbackResponder."""

    @pytest.mark.asyncio
    async def test_chat_response(self) -> None:
        """Chat mode returns LLM response."""
        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(
            return_value=LLMResponse(content="Hello! How can I help?")
        )

        responder = ChatModeFallbackResponder(provider=mock_provider)
        result = await responder.respond("Hi there")
        assert result == "Hello! How can I help?"

    @pytest.mark.asyncio
    async def test_chat_error_fallback(self) -> None:
        """Chat mode returns fallback on error."""
        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(side_effect=Exception("API error"))

        responder = ChatModeFallbackResponder(provider=mock_provider)
        result = await responder.respond("Hi")
        assert "trouble" in result.lower()


# ---------------------------------------------------------------------------
# Bootstrap runtime creation tests
# ---------------------------------------------------------------------------


class TestBootstrapRuntimeCreation:
    """Tests for build_provider_backed_agent_loop_from_config() runtime creation."""

    def test_bootstrap_creates_runtime_when_not_provided(self, tmp_path: Path) -> None:
        """bootstrap creates a ClamRuntime when no runtime is passed."""
        from clambot.agent.bootstrap import build_provider_backed_agent_loop_from_config
        from clambot.agent.runtime import ClamRuntime
        from clambot.config.schema import ClamBotConfig

        # Create required workspace subdirectories
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        config = ClamBotConfig()
        tool_registry = None

        with (
            patch("clambot.agent.bootstrap.create_provider") as mock_create_provider,
            patch("clambot.agent.bootstrap.AmlaSandboxRuntimeBackend") as mock_backend_cls,
        ):
            mock_create_provider.return_value = MagicMock()
            mock_backend_cls.return_value = MagicMock()

            agent_loop = build_provider_backed_agent_loop_from_config(
                config=config,
                tool_registry=tool_registry,
                workspace=tmp_path,
            )

        assert agent_loop._runtime is not None
        assert isinstance(agent_loop._runtime, ClamRuntime)

    def test_bootstrap_uses_provided_runtime(self, tmp_path: Path) -> None:
        """bootstrap uses the caller-supplied runtime without creating a new one."""
        from clambot.agent.bootstrap import build_provider_backed_agent_loop_from_config
        from clambot.config.schema import ClamBotConfig

        # Create required workspace subdirectories
        (tmp_path / "clams").mkdir(parents=True, exist_ok=True)
        (tmp_path / "build").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        config = ClamBotConfig()
        mock_runtime = MagicMock()

        with (
            patch("clambot.agent.bootstrap.create_provider") as mock_create_provider,
            patch("clambot.agent.bootstrap.AmlaSandboxRuntimeBackend") as mock_backend_cls,
        ):
            mock_create_provider.return_value = MagicMock()
            mock_backend_cls.return_value = MagicMock()

            agent_loop = build_provider_backed_agent_loop_from_config(
                config=config,
                runtime=mock_runtime,
                workspace=tmp_path,
            )

        assert agent_loop._runtime is mock_runtime
