"""Integration tests for the full agent pipeline — Phase 14.

End-to-end tests verifying the agent pipeline from inbound message through
clam generation, WASM execution, analysis, and response.  Uses mocked LLM
providers but exercises real orchestration, session management, file I/O,
clam registry scanning, and memory persistence.

Tests:
- Full agent turn: message → clam generated → WASM executed → response
- Clam reuse: identical message skips LLM selector (pre-selection match)
- Chat mode: conversational question → no clam written
- Self-fix loop: failing clam → SELF_FIX → re-generation attempted
- Memory persistence: fact stored → /new → fact in system prompt
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from clambot.agent.clams import ClamRegistry
from clambot.agent.context import ContextBuilder
from clambot.agent.generation_adapter import GenerationResult
from clambot.agent.loop import AgentLoop, AgentResult
from clambot.agent.post_runtime_analysis import PostRuntimeAnalysisDecision
from clambot.agent.post_runtime_analysis_adapter import AnalysisResult
from clambot.agent.runtime_backend_amla_sandbox import RuntimeResult
from clambot.agent.selector import ProviderBackedClamSelector, SelectionResult
from clambot.agent.turn_execution import process_turn_with_persistence_and_execution
from clambot.bus.events import InboundMessage
from clambot.bus.queue import MessageBus
from clambot.config.schema import ClamBotConfig
from clambot.gateway.orchestrator import GatewayOrchestrator
from clambot.memory.store import memory_recall, memory_save
from clambot.providers.base import LLMResponse
from clambot.session.manager import SessionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace directory structure."""
    ws = tmp_path / "workspace"
    for subdir in ("clams", "build", "sessions", "logs", "docs", "memory"):
        (ws / subdir).mkdir(parents=True, exist_ok=True)
    # Create empty MEMORY.md and HISTORY.md
    (ws / "memory" / "MEMORY.md").write_text("", encoding="utf-8")
    (ws / "memory" / "HISTORY.md").write_text("", encoding="utf-8")
    return ws


def _make_config(workspace: Path) -> ClamBotConfig:
    """Create a minimal ClamBotConfig pointing at the workspace."""
    return ClamBotConfig.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(workspace),
                    "model": "test-model",
                    "maxTokens": 4096,
                    "temperature": 0.7,
                    "maxSelfFixAttempts": 3,
                },
                "selector": {
                    "model": "test-selector-model",
                    "retries": 1,
                    "maxTokens": 1024,
                    "temperature": 0.0,
                },
                "memoryPromptBudget": {
                    "maxTokens": 2000,
                    "minTokens": 0,
                    "reserveTokens": 500,
                    "maxContextRatio": 0.3,
                    "durableFactsRatio": 0.5,
                },
                "approvals": {
                    "enabled": False,
                },
            },
        }
    )


def _make_mock_provider(responses: list[str] | None = None) -> AsyncMock:
    """Create a mock LLM provider that returns predefined responses."""
    provider = AsyncMock()
    if responses:
        provider.acomplete = AsyncMock(side_effect=[LLMResponse(content=r) for r in responses])
    else:
        provider.acomplete = AsyncMock(return_value=LLMResponse(content="Mock response"))
    return provider


def _make_inbound(
    content: str = "hello",
    channel: str = "test",
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


def _write_promoted_clam(
    workspace: Path,
    name: str,
    script: str,
    *,
    description: str = "",
    declared_tools: list[str] | None = None,
    source_request: str = "",
    reusable: bool = True,
) -> None:
    """Write a promoted clam directly into clams/ directory."""
    clam_dir = workspace / "clams" / name
    clam_dir.mkdir(parents=True, exist_ok=True)

    (clam_dir / "run.js").write_text(script, encoding="utf-8")

    lines = ["---"]
    lines.append(f'description: "{description or name}"')
    lines.append("language: javascript")
    if declared_tools:
        lines.append("declared_tools:")
        for t in declared_tools:
            lines.append(f"  - {t}")
    if reusable:
        lines.append("reusable: true")
    if source_request:
        lines.append(f'source_request: "{source_request}"')
    lines.append("---")
    lines.append("")
    lines.append(description or name)

    (clam_dir / "CLAM.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Test: Full agent turn (generate → execute → analyze → response)
# ---------------------------------------------------------------------------


class TestFullAgentTurn:
    """Full pipeline: message → generate clam → execute → analyze → response."""

    @pytest.mark.asyncio
    async def test_full_turn_generates_and_produces_response(self, tmp_path: Path) -> None:
        """A normal user request flows through the complete agent pipeline
        and produces a response with clam promotion."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        # Mock selector: decide to generate new
        mock_selector = AsyncMock()
        mock_selector.select = AsyncMock(
            return_value=SelectionResult(
                decision="generate_new",
                reason="No matching clam found",
            )
        )

        # Mock generator: return a simple script
        mock_generator = AsyncMock()
        mock_generator.generate = AsyncMock(
            return_value=GenerationResult(
                language="javascript",
                script='console.log("Hello from clam");',
                declared_tools=[],
                inputs={},
                metadata={
                    "description": "test clam",
                    "reusable": True,
                    "source_request": "say hello",
                },
            )
        )

        # Mock runtime: return successful result
        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="Hello from clam",
                error="",
            )
        )

        # Mock analyzer: ACCEPT the result
        mock_analyzer = AsyncMock()
        mock_analyzer.analyze = AsyncMock(
            return_value=AnalysisResult(
                decision=PostRuntimeAnalysisDecision.ACCEPT,
                output="Hello from clam",
                reason="Output is correct",
            )
        )

        # Build the agent loop
        clam_registry = ClamRegistry(workspace=workspace)
        context_builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )

        agent_loop = AgentLoop(
            selector=mock_selector,
            generator=mock_generator,
            runtime=mock_runtime,
            analyzer=mock_analyzer,
            tool_registry=None,
            context_builder=context_builder,
            clam_registry=clam_registry,
            memory_workspace=workspace,
            config=config,
        )

        # Execute the turn
        result = await agent_loop.process_turn(
            message="say hello",
            session_key="test:user1",
        )

        # Verify pipeline executed completely
        assert result.status == "completed"
        assert "Hello from clam" in result.content
        mock_selector.select.assert_called_once()
        mock_generator.generate.assert_called_once()
        mock_runtime.execute.assert_called_once()
        mock_analyzer.analyze.assert_called_once()

        # Verify clam was promoted (build/ → clams/)
        clams_dir = workspace / "clams"
        promoted = list(clams_dir.iterdir())
        assert len(promoted) >= 1, "Clam should be promoted to clams/ directory"

    @pytest.mark.asyncio
    async def test_full_turn_with_session_persistence(self, tmp_path: Path) -> None:
        """Agent turn persists user and assistant turns to session."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)
        session_manager = SessionManager(workspace)

        # Mock agent loop
        mock_agent_loop = AsyncMock()
        mock_result = AgentResult(
            content="Here is the answer.",
            status="completed",
        )
        mock_agent_loop.process_turn = AsyncMock(return_value=mock_result)

        inbound = _make_inbound(content="What is 2+2?")

        outbound = await process_turn_with_persistence_and_execution(
            inbound=inbound,
            agent_loop=mock_agent_loop,
            session_manager=session_manager,
            config=config,
            workspace=workspace,
        )

        # Verify outbound response
        assert outbound.content == "Here is the answer."
        assert outbound.channel == inbound.channel
        assert outbound.correlation_id == inbound.correlation_id

        # Verify session persistence (user + assistant turns appended)
        session_key = inbound.session_key or f"{inbound.channel}:{inbound.source}"
        turns = session_manager.load_history(session_key)
        assert len(turns) >= 2
        roles = [t.role for t in turns]
        assert "user" in roles
        assert "assistant" in roles


# ---------------------------------------------------------------------------
# Test: Clam reuse (pre-selection)
# ---------------------------------------------------------------------------


class TestClamReuse:
    """Identical message reuses existing clam — no LLM selector call needed."""

    @pytest.mark.asyncio
    async def test_preselection_skips_llm_selector(self, tmp_path: Path) -> None:
        """When a promoted clam's source_request matches the normalized
        request, pre-selection fires and the LLM selector is never called."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        # Write a promoted clam whose source_request matches
        _write_promoted_clam(
            workspace,
            "check-weather",
            'console.log("Sunny 72°F");',
            description="Check weather",
            source_request="check the weather",
            reusable=True,
        )

        # Mock provider — if called, it means pre-selection failed
        mock_provider = _make_mock_provider()

        selector = ProviderBackedClamSelector(
            provider=mock_provider,
            max_tokens=1024,
            temperature=0.0,
            retries=1,
        )

        clam_registry = ClamRegistry(workspace=workspace)
        catalog = clam_registry.get_catalog()

        # Send the exact same request
        result = await selector.select(
            message="check the weather",
            clam_catalog=catalog,
        )

        # Pre-selection should have fired
        assert result.decision == "select_existing"
        assert result.clam_id == "check-weather"

        # LLM provider should NOT have been called
        mock_provider.acomplete.assert_not_called()

    @pytest.mark.asyncio
    async def test_second_identical_request_uses_cached_clam(self, tmp_path: Path) -> None:
        """Full pipeline: first request generates a clam, second identical
        request finds it via pre-selection and skips generation entirely."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        # Simulate first run: promote a clam
        _write_promoted_clam(
            workspace,
            "list-files",
            'const result = await fs({action: "list", path: "."});',
            description="List files in current directory",
            declared_tools=["fs"],
            source_request="list all files",
            reusable=True,
        )

        # Mock runtime
        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="file1.txt\nfile2.txt",
                error="",
            )
        )

        # Mock selector with a real instance
        mock_provider = _make_mock_provider()
        selector = ProviderBackedClamSelector(
            provider=mock_provider,
            max_tokens=1024,
            temperature=0.0,
        )

        clam_registry = ClamRegistry(workspace=workspace)
        context_builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )

        agent_loop = AgentLoop(
            selector=selector,
            generator=AsyncMock(),  # Should not be called
            runtime=mock_runtime,
            analyzer=AsyncMock(),  # Skipped for reusable clams
            tool_registry=None,
            context_builder=context_builder,
            clam_registry=clam_registry,
            memory_workspace=workspace,
            config=config,
        )

        result = await agent_loop.process_turn(
            message="list all files",
            session_key="test:user1",
        )

        assert result.status == "completed"
        assert result.clam_name == "list-files"
        # Pre-selection means LLM was not called
        mock_provider.acomplete.assert_not_called()
        # Runtime was called to execute the existing clam
        mock_runtime.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Test: Chat mode
# ---------------------------------------------------------------------------


class TestChatMode:
    """Conversational questions should not generate code."""

    @pytest.mark.asyncio
    async def test_chat_mode_no_clam_written(self, tmp_path: Path) -> None:
        """When the selector decides 'chat', no clam is written to disk."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        # Mock selector: decide chat
        mock_selector = AsyncMock()
        mock_selector.select = AsyncMock(
            return_value=SelectionResult(
                decision="chat",
                reason="Conversational question",
                chat_response="I'm doing well, thank you for asking!",
            )
        )

        mock_generator = AsyncMock()
        mock_runtime = AsyncMock()

        clam_registry = ClamRegistry(workspace=workspace)
        context_builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )

        agent_loop = AgentLoop(
            selector=mock_selector,
            generator=mock_generator,
            runtime=mock_runtime,
            analyzer=AsyncMock(),
            tool_registry=None,
            context_builder=context_builder,
            clam_registry=clam_registry,
            memory_workspace=workspace,
            config=config,
        )

        result = await agent_loop.process_turn(
            message="How are you today?",
            session_key="test:user1",
        )

        # Chat response returned
        assert result.status == "chat"
        assert "doing well" in result.content.lower() or result.content

        # Generator and runtime were NOT called
        mock_generator.generate.assert_not_called()
        mock_runtime.execute.assert_not_called()

        # No clams written to build/ or clams/
        build_contents = list((workspace / "build").iterdir())
        assert len(build_contents) == 0, "No clam should be written in chat mode"


# ---------------------------------------------------------------------------
# Test: Self-fix loop
# ---------------------------------------------------------------------------


class TestSelfFixLoop:
    """Failing clam triggers SELF_FIX re-generation."""

    @pytest.mark.asyncio
    async def test_self_fix_triggered_on_execution_error(self, tmp_path: Path) -> None:
        """When execution fails, the self-fix loop re-enters generation
        with error context, up to max attempts."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        # Mock selector: generate new
        mock_selector = AsyncMock()
        mock_selector.select = AsyncMock(
            return_value=SelectionResult(
                decision="generate_new",
                reason="New code needed",
            )
        )

        # Generator: first attempt bad, second attempt good
        call_count = 0

        async def mock_generate(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return GenerationResult(
                    language="javascript",
                    script='throw new Error("oops");',
                    metadata={"description": "broken clam"},
                )
            else:
                return GenerationResult(
                    language="javascript",
                    script='console.log("fixed!");',
                    metadata={
                        "description": "fixed clam",
                        "reusable": True,
                        "source_request": "do the thing",
                    },
                )

        mock_generator = AsyncMock()
        mock_generator.generate = AsyncMock(side_effect=mock_generate)

        # Runtime: first call fails, second succeeds
        runtime_call = 0

        async def mock_execute(**kwargs):
            nonlocal runtime_call
            runtime_call += 1
            if runtime_call == 1:
                return RuntimeResult(
                    output="",
                    error="Error: oops",
                )
            else:
                return RuntimeResult(
                    output="fixed!",
                    error="",
                )

        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(side_effect=mock_execute)

        # Analyzer: accept second attempt
        mock_analyzer = AsyncMock()
        mock_analyzer.analyze = AsyncMock(
            return_value=AnalysisResult(
                decision=PostRuntimeAnalysisDecision.ACCEPT,
                output="fixed!",
                reason="Output is correct",
            )
        )

        clam_registry = ClamRegistry(workspace=workspace)
        context_builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )

        agent_loop = AgentLoop(
            selector=mock_selector,
            generator=mock_generator,
            runtime=mock_runtime,
            analyzer=mock_analyzer,
            tool_registry=None,
            context_builder=context_builder,
            clam_registry=clam_registry,
            memory_workspace=workspace,
            config=config,
        )

        result = await agent_loop.process_turn(
            message="do the thing",
            session_key="test:user1",
        )

        # Verify self-fix occurred
        assert result.status == "completed"
        assert "fixed" in result.content.lower()
        # Generator called twice (first attempt + self-fix)
        assert mock_generator.generate.call_count == 2
        # Runtime called twice
        assert mock_runtime.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_self_fix_analysis_driven(self, tmp_path: Path) -> None:
        """SELF_FIX decision from analyzer triggers re-generation."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        mock_selector = AsyncMock()
        mock_selector.select = AsyncMock(
            return_value=SelectionResult(
                decision="generate_new",
                reason="New code needed",
            )
        )

        gen_count = 0

        async def mock_generate(**kwargs):
            nonlocal gen_count
            gen_count += 1
            return GenerationResult(
                language="javascript",
                script=f'console.log("attempt {gen_count}");',
                metadata={"description": f"attempt {gen_count}"},
            )

        mock_generator = AsyncMock()
        mock_generator.generate = AsyncMock(side_effect=mock_generate)

        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="attempt output",
                error="",
            )
        )

        # Analyzer: first SELF_FIX, then ACCEPT
        analysis_count = 0

        async def mock_analyze(**kwargs):
            nonlocal analysis_count
            analysis_count += 1
            if analysis_count == 1:
                return AnalysisResult(
                    decision=PostRuntimeAnalysisDecision.SELF_FIX,
                    fix_instructions="Improve the output format",
                    reason="Output doesn't match expected format",
                )
            return AnalysisResult(
                decision=PostRuntimeAnalysisDecision.ACCEPT,
                output="attempt output",
                reason="Output is now correct",
            )

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze = AsyncMock(side_effect=mock_analyze)

        clam_registry = ClamRegistry(workspace=workspace)
        context_builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )

        agent_loop = AgentLoop(
            selector=mock_selector,
            generator=mock_generator,
            runtime=mock_runtime,
            analyzer=mock_analyzer,
            tool_registry=None,
            context_builder=context_builder,
            clam_registry=clam_registry,
            memory_workspace=workspace,
            config=config,
        )

        result = await agent_loop.process_turn(
            message="format data nicely",
            session_key="test:user1",
        )

        assert result.status == "completed"
        # Generator called twice (initial + self-fix)
        assert mock_generator.generate.call_count == 2
        # Second generation should have self_fix_context
        second_call = mock_generator.generate.call_args_list[1]
        assert second_call.kwargs.get("self_fix_context", "") != ""


# ---------------------------------------------------------------------------
# Test: Memory persistence across /new
# ---------------------------------------------------------------------------


class TestMemoryPersistence:
    """Memory is persisted: fact → /new consolidation → visible in next turn."""

    @pytest.mark.asyncio
    async def test_memory_saved_and_visible_in_system_prompt(self, tmp_path: Path) -> None:
        """After saving a fact to MEMORY.md, the ContextBuilder includes
        it in the system prompt for the next turn."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        # Save a fact to memory
        memory_save(workspace, "# Memory\n\n- User's favorite color is blue.\n")

        # Verify it's readable
        recalled = memory_recall(workspace)
        assert "favorite color is blue" in recalled

        # Build system prompt — should include the memory
        context_builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )
        prompt = context_builder.build_system_prompt(
            docs="",
            memory=recalled,
            tools=[],
            clam_catalog=[],
        )
        assert "favorite color is blue" in prompt

    @pytest.mark.asyncio
    async def test_new_session_triggers_consolidation_and_resets(self, tmp_path: Path) -> None:
        """/new triggers memory consolidation and resets the session."""
        workspace = _make_workspace(tmp_path)
        session_manager = SessionManager(workspace)

        session_key = "test:user1"

        # Add some conversation turns
        session_manager.append_turn(session_key, "user", "My dog's name is Rex.")
        session_manager.append_turn(session_key, "assistant", "Nice! Rex is a great name.")
        session_manager.append_turn(session_key, "user", "He loves fetch.")
        session_manager.append_turn(session_key, "assistant", "That's a fun activity for dogs!")

        turns_before = session_manager.load_history(session_key)
        assert len(turns_before) >= 4

        # Mock the provider for consolidation
        mock_provider = _make_mock_provider(
            [
                json.dumps(
                    {
                        "history_entry": "User discussed their dog Rex who loves fetch.",
                        "memory_update": (
                            "# Memory\n\n- User has a dog named Rex.\n- Rex loves fetch.\n"
                        ),
                    }
                )
            ]
        )

        # Build a minimal orchestrator with /new support
        bus = MessageBus()
        orch = GatewayOrchestrator(
            bus=bus,
            session_manager=session_manager,
            approval_gate=MagicMock(),
            provider=mock_provider,
            workspace=workspace,
        )

        # Send /new
        new_msg = _make_inbound(content="/new", source="user1", chat_id="user1")
        result = await orch._process_inbound(new_msg)

        assert result is not None
        assert result.content == "New session started."

        # Memory should now contain the consolidated facts
        memory = memory_recall(workspace)
        assert "Rex" in memory

    @pytest.mark.asyncio
    async def test_memory_from_previous_session_in_next_turn_prompt(self, tmp_path: Path) -> None:
        """After /new, the next turn's system prompt includes consolidated memory."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        # Pre-populate memory as if consolidation already ran
        memory_save(workspace, "# Memory\n\n- User works at Acme Corp.\n- Prefers dark mode.\n")

        # Build context for the next turn
        context_builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )
        memory = memory_recall(workspace)
        prompt = context_builder.build_system_prompt(
            docs="",
            memory=memory,
            tools=[],
            clam_catalog=[],
        )

        assert "Acme Corp" in prompt
        assert "dark mode" in prompt


# ---------------------------------------------------------------------------
# Test: Chat mode uses memory context
# ---------------------------------------------------------------------------


class TestChatModeMemory:
    """Chat mode responses must include long-term memory in the prompt."""

    @pytest.mark.asyncio
    async def test_chat_responder_receives_memory_in_system_prompt(self, tmp_path: Path) -> None:
        """When the user asks 'what's my name?', the chat responder must see
        the memory containing the user's name and respond correctly."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        # Seed memory with user's name
        memory_save(workspace, "### Facts\n- The user's name is Alex.\n")

        # Mock selector: route to chat
        mock_selector = AsyncMock()
        mock_selector.select = AsyncMock(
            return_value=SelectionResult(
                decision="chat",
                reason="Personal/memory question",
                chat_response="",
            )
        )

        # Mock chat provider — capture what system prompt is sent
        captured_messages: list[list[dict[str, Any]]] = []

        async def _capture_acomplete(messages, **kwargs):
            captured_messages.append(list(messages))
            return LLMResponse(content="Your name is Alex!")

        mock_provider = AsyncMock()
        mock_provider.acomplete = AsyncMock(side_effect=_capture_acomplete)

        from clambot.agent.chat_mode import ChatModeFallbackResponder

        chat_responder = ChatModeFallbackResponder(provider=mock_provider)

        clam_registry = ClamRegistry(workspace=workspace)
        context_builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )

        agent_loop = AgentLoop(
            selector=mock_selector,
            generator=AsyncMock(),
            runtime=AsyncMock(),
            analyzer=AsyncMock(),
            tool_registry=None,
            context_builder=context_builder,
            clam_registry=clam_registry,
            memory_workspace=workspace,
            chat_responder=chat_responder,
            config=config,
        )

        result = await agent_loop.process_turn(
            message="what's my name?",
            session_key="test:memory",
        )

        # Chat responder was used (not selector's chat_response)
        assert result.status == "chat"
        assert "Alex" in result.content

        # Verify system prompt sent to LLM contained memory
        assert len(captured_messages) == 1
        system_msg = captured_messages[0][0]
        assert system_msg["role"] == "system"
        assert "Alex" in system_msg["content"]
        assert "Long-Term Memory" in system_msg["content"]


class TestCodexMultipleSystemMessages:
    """Codex provider must concatenate multiple system messages."""

    def test_convert_messages_merges_system_prompts(self) -> None:
        """Multiple system messages should be joined, not overwritten."""
        from clambot.providers.openai_codex_provider import _convert_messages

        messages = [
            {
                "role": "system",
                "content": "You are a helpful assistant. Memory: User name is Alex.",
            },
            {"role": "system", "content": "You have access to tools: cron, web_fetch."},
            {"role": "user", "content": "what's my name?"},
        ]

        system_prompt, input_items = _convert_messages(messages)

        # Both system messages must be present
        assert "Alex" in system_prompt
        assert "cron" in system_prompt
        assert len(input_items) == 1  # only the user message
