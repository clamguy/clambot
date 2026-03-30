"""Integration tests for filesystem operations via the agent pipeline.

End-to-end tests verifying that user requests to create and read files
flow correctly through selector → generator → grounding → runtime →
analysis → response.

Covers:
- Write file: "save 'hello' to /tmp/..." → fs({operation: "write"}) → success
- Read file: "show /tmp/..." → fs({operation: "read"}) → file content
- Grounding rejects Node.js APIs (require, fs.readFileSync) and self-fix
  recovers using the built-in fs tool
- Sequential write-then-read across two agent turns
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from clambot.agent.clams import ClamRegistry
from clambot.agent.context import ContextBuilder
from clambot.agent.generation_adapter import GenerationResult
from clambot.agent.generation_grounding import apply_grounding_rules
from clambot.agent.loop import AgentLoop
from clambot.agent.post_runtime_analysis import PostRuntimeAnalysisDecision
from clambot.agent.post_runtime_analysis_adapter import AnalysisResult
from clambot.agent.runtime_backend_amla_sandbox import RuntimeResult
from clambot.agent.selector import SelectionResult
from clambot.config.schema import ClamBotConfig

# ---------------------------------------------------------------------------
# Helpers (match existing integration-test conventions)
# ---------------------------------------------------------------------------


def _make_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace directory structure."""
    ws = tmp_path / "workspace"
    for subdir in ("clams", "build", "sessions", "logs", "docs", "memory"):
        (ws / subdir).mkdir(parents=True, exist_ok=True)
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


def _make_agent_loop(
    workspace: Path,
    config: ClamBotConfig,
    *,
    generator: AsyncMock,
    runtime: AsyncMock,
    analyzer: AsyncMock | None = None,
) -> AgentLoop:
    """Build an AgentLoop with generate_new selector and supplied mocks."""
    mock_selector = AsyncMock()
    mock_selector.select = AsyncMock(
        return_value=SelectionResult(
            decision="generate_new",
            reason="File operation — generating new clam",
        )
    )

    if analyzer is None:
        analyzer = AsyncMock()
        analyzer.analyze = AsyncMock(
            return_value=AnalysisResult(
                decision=PostRuntimeAnalysisDecision.ACCEPT,
                output="",
                reason="OK",
            )
        )

    clam_registry = ClamRegistry(workspace=workspace)
    context_builder = ContextBuilder(
        workspace=workspace,
        memory_budget_config=config.agents.memory_prompt_budget,
    )

    return AgentLoop(
        selector=mock_selector,
        generator=generator,
        runtime=runtime,
        analyzer=analyzer,
        tool_registry=None,
        context_builder=context_builder,
        clam_registry=clam_registry,
        memory_workspace=workspace,
        config=config,
    )


# ---------------------------------------------------------------------------
# Test: fs write via agent pipeline
# ---------------------------------------------------------------------------


FS_WRITE_SCRIPT = """\
async function run(args) {
  const result = await fs({operation: "write", path: args.path, content: args.content});
  return result;
}"""

FS_READ_SCRIPT = """\
async function run(args) {
  const content = await fs({operation: "read", path: args.path});
  return content;
}"""

NODEJS_REQUIRE_SCRIPT = """\
const fs = require("fs");
async function run(args) {
  const expandedPath = args.path.replace(/^~\\//, `${process.env.HOME}/`);
  return fs.readFileSync(expandedPath, "utf8");
}"""

NODEJS_PROMISES_SCRIPT = """\
async function run(args) {
  const fileContents = await fs.promises.readFile(args.path, "utf8");
  return fileContents;
}"""

NODEJS_IMPORT_SCRIPT = """\
import { readFile } from "fs/promises";
async function run(args) {
  return await readFile(args.path, "utf8");
}"""

NODEJS_FETCH_SCRIPT = """\
async function run(args) {
  const resp = await fetch(args.url);
  return await resp.text();
}"""


class TestFsWriteFlow:
    """User request to create a file flows through the pipeline correctly."""

    @pytest.mark.asyncio
    async def test_write_file_to_tmp(self, tmp_path: Path) -> None:
        """'save hello to /tmp/test.txt' generates an fs write clam,
        executes successfully, and returns confirmation."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        mock_generator = AsyncMock()
        mock_generator.generate = AsyncMock(
            return_value=GenerationResult(
                language="javascript",
                script=FS_WRITE_SCRIPT,
                declared_tools=["fs"],
                inputs={"path": "/tmp/test_clambot.txt", "content": "hello world"},
                metadata={
                    "description": "Writes content to a file",
                    "reusable": True,
                    "source_request": "save 'hello world' to /tmp/test_clambot.txt",
                },
            )
        )

        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="Written 11 bytes to /tmp/test_clambot.txt",
                error="",
            )
        )

        agent_loop = _make_agent_loop(
            workspace,
            config,
            generator=mock_generator,
            runtime=mock_runtime,
        )

        result = await agent_loop.process_turn(
            message="save 'hello world' to /tmp/test_clambot.txt",
            session_key="test:user1",
        )

        assert result.status == "completed"
        assert "Written" in result.content or "test_clambot" in result.content
        mock_generator.generate.assert_called_once()
        mock_runtime.execute.assert_called_once()

        # Verify the clam was promoted (build/ → clams/)
        promoted = list((workspace / "clams").iterdir())
        assert len(promoted) >= 1, "Clam should be promoted to clams/"
        # Verify run.js contains the fs tool call, not require()
        run_js = (promoted[0] / "run.js").read_text(encoding="utf-8")
        assert "await fs(" in run_js
        assert "require" not in run_js


class TestFsReadFlow:
    """User request to read a file flows through the pipeline correctly."""

    @pytest.mark.asyncio
    async def test_read_file_from_tmp(self, tmp_path: Path) -> None:
        """'show /tmp/test.txt' generates an fs read clam,
        executes successfully, and returns file content."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        mock_generator = AsyncMock()
        mock_generator.generate = AsyncMock(
            return_value=GenerationResult(
                language="javascript",
                script=FS_READ_SCRIPT,
                declared_tools=["fs"],
                inputs={"path": "/tmp/test_clambot.txt"},
                metadata={
                    "description": "Reads and returns file contents",
                    "reusable": True,
                    "source_request": "show /tmp/test_clambot.txt",
                },
            )
        )

        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="hello world",
                error="",
            )
        )

        agent_loop = _make_agent_loop(
            workspace,
            config,
            generator=mock_generator,
            runtime=mock_runtime,
        )

        result = await agent_loop.process_turn(
            message="show /tmp/test_clambot.txt",
            session_key="test:user1",
        )

        assert result.status == "completed"
        assert "hello world" in result.content
        mock_runtime.execute.assert_called_once()

        # Verify promoted clam contains correct fs tool usage
        promoted = list((workspace / "clams").iterdir())
        assert len(promoted) >= 1, "Clam should be promoted to clams/"
        run_js = (promoted[0] / "run.js").read_text(encoding="utf-8")
        assert "await fs(" in run_js
        assert 'operation: "read"' in run_js


# ---------------------------------------------------------------------------
# Test: Grounding rejects Node.js APIs and self-fix recovers
# ---------------------------------------------------------------------------


class TestNodejsGroundingRejection:
    """Scripts using Node.js APIs are caught by grounding before WASM."""

    def test_grounding_rejects_require(self) -> None:
        """require('fs') is rejected with an error pointing at the fs tool."""
        gen = GenerationResult(language="javascript", script=NODEJS_REQUIRE_SCRIPT)
        result = apply_grounding_rules(gen)
        assert result.error is not None
        assert "require()" in result.error
        assert "await fs(" in result.error

    def test_grounding_rejects_fs_promises(self) -> None:
        """fs.promises.readFile() is rejected."""
        gen = GenerationResult(language="javascript", script=NODEJS_PROMISES_SCRIPT)
        result = apply_grounding_rules(gen)
        assert result.error is not None
        assert "fs.promises" in result.error

    def test_grounding_rejects_import_from(self) -> None:
        """import { readFile } from 'fs/promises' is rejected."""
        gen = GenerationResult(language="javascript", script=NODEJS_IMPORT_SCRIPT)
        result = apply_grounding_rules(gen)
        assert result.error is not None
        assert "import" in result.error

    def test_grounding_rejects_bare_fetch(self) -> None:
        """fetch() is rejected — must use web_fetch or http_request tool."""
        gen = GenerationResult(language="javascript", script=NODEJS_FETCH_SCRIPT)
        result = apply_grounding_rules(gen)
        assert result.error is not None
        assert "fetch()" in result.error

    def test_grounding_accepts_correct_fs_tool_usage(self) -> None:
        """Proper await fs({operation: 'read'}) passes grounding cleanly."""
        gen = GenerationResult(language="javascript", script=FS_READ_SCRIPT)
        result = apply_grounding_rules(gen)
        assert result.error is None

    def test_grounding_accepts_correct_fs_write_tool_usage(self) -> None:
        """Proper await fs({operation: 'write'}) passes grounding cleanly."""
        gen = GenerationResult(language="javascript", script=FS_WRITE_SCRIPT)
        result = apply_grounding_rules(gen)
        assert result.error is None


class TestSelfFixFromNodejsToFsTool:
    """Grounding rejection of Node.js code triggers self-fix that succeeds
    with the correct fs tool pattern."""

    @pytest.mark.asyncio
    async def test_self_fix_recovers_from_require_fs(self, tmp_path: Path) -> None:
        """First attempt uses require('fs') → grounding rejects → self-fix
        re-generates with correct await fs({...}) → succeeds."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        gen_count = 0

        async def mock_generate(**kwargs):
            nonlocal gen_count
            gen_count += 1
            if gen_count == 1:
                # First attempt: LLM returns Node.js code (the bug)
                return GenerationResult(
                    language="javascript",
                    script=NODEJS_REQUIRE_SCRIPT,
                    declared_tools=["fs"],
                    inputs={"path": "/tmp/test_clambot.txt"},
                    metadata={"description": "read file (Node.js style)"},
                )
            # Self-fix attempt: LLM corrects to built-in tool
            return GenerationResult(
                language="javascript",
                script=FS_READ_SCRIPT,
                declared_tools=["fs"],
                inputs={"path": "/tmp/test_clambot.txt"},
                metadata={
                    "description": "read file via fs tool",
                    "reusable": True,
                    "source_request": "show /tmp/test_clambot.txt",
                },
            )

        mock_generator = AsyncMock()
        mock_generator.generate = AsyncMock(side_effect=mock_generate)

        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="hello world",
                error="",
            )
        )

        agent_loop = _make_agent_loop(
            workspace,
            config,
            generator=mock_generator,
            runtime=mock_runtime,
        )

        result = await agent_loop.process_turn(
            message="show /tmp/test_clambot.txt",
            session_key="test:user1",
        )

        assert result.status == "completed"
        assert "hello world" in result.content
        # Generator called twice: bad attempt + self-fix
        assert mock_generator.generate.call_count == 2
        # Runtime only called once (grounding caught the first attempt)
        assert mock_runtime.execute.call_count == 1
        # Self-fix context was passed to the second generation call
        second_call_kwargs = mock_generator.generate.call_args_list[1].kwargs
        assert "self_fix_context" in second_call_kwargs
        assert "require()" in second_call_kwargs["self_fix_context"]

    @pytest.mark.asyncio
    async def test_self_fix_recovers_from_fs_promises(self, tmp_path: Path) -> None:
        """fs.promises.readFile() is caught by grounding; self-fix succeeds."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        gen_count = 0

        async def mock_generate(**kwargs):
            nonlocal gen_count
            gen_count += 1
            if gen_count == 1:
                return GenerationResult(
                    language="javascript",
                    script=NODEJS_PROMISES_SCRIPT,
                    declared_tools=["fs"],
                    inputs={"path": "/tmp/test.txt"},
                    metadata={"description": "read file (Node.js promises)"},
                )
            return GenerationResult(
                language="javascript",
                script=FS_READ_SCRIPT,
                declared_tools=["fs"],
                inputs={"path": "/tmp/test.txt"},
                metadata={
                    "description": "read file via fs tool",
                    "reusable": True,
                    "source_request": "show /tmp/test.txt",
                },
            )

        mock_generator = AsyncMock()
        mock_generator.generate = AsyncMock(side_effect=mock_generate)

        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="file contents here",
                error="",
            )
        )

        agent_loop = _make_agent_loop(
            workspace,
            config,
            generator=mock_generator,
            runtime=mock_runtime,
        )

        result = await agent_loop.process_turn(
            message="show /tmp/test.txt",
            session_key="test:user1",
        )

        assert result.status == "completed"
        assert mock_generator.generate.call_count == 2
        assert mock_runtime.execute.call_count == 1


# ---------------------------------------------------------------------------
# Test: Sequential write → read across two turns
# ---------------------------------------------------------------------------


class TestWriteThenRead:
    """Two sequential agent turns: first writes a file, then reads it back."""

    @pytest.mark.asyncio
    async def test_write_then_read_sequential_turns(self, tmp_path: Path) -> None:
        """Turn 1 writes a file via fs tool; turn 2 reads it back.
        Verifies both turns complete and the pipeline handles fs ops
        in sequence."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        # --- Turn 1: write ---
        write_generator = AsyncMock()
        write_generator.generate = AsyncMock(
            return_value=GenerationResult(
                language="javascript",
                script=FS_WRITE_SCRIPT,
                declared_tools=["fs"],
                inputs={"path": "/tmp/clambot_int_test.txt", "content": "integration test data"},
                metadata={
                    "description": "Writes content to a file",
                    "reusable": True,
                    "source_request": "save 'integration test data' to /tmp/clambot_int_test.txt",
                },
            )
        )

        write_runtime = AsyncMock()
        write_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="Written 21 bytes to /tmp/clambot_int_test.txt",
                error="",
            )
        )

        write_loop = _make_agent_loop(
            workspace,
            config,
            generator=write_generator,
            runtime=write_runtime,
        )

        write_result = await write_loop.process_turn(
            message="save 'integration test data' to /tmp/clambot_int_test.txt",
            session_key="test:user1",
        )

        assert write_result.status == "completed"
        assert "Written" in write_result.content

        # --- Turn 2: read ---
        read_generator = AsyncMock()
        read_generator.generate = AsyncMock(
            return_value=GenerationResult(
                language="javascript",
                script=FS_READ_SCRIPT,
                declared_tools=["fs"],
                inputs={"path": "/tmp/clambot_int_test.txt"},
                metadata={
                    "description": "Reads and returns file contents",
                    "reusable": True,
                    "source_request": "show /tmp/clambot_int_test.txt",
                },
            )
        )

        read_runtime = AsyncMock()
        read_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="integration test data",
                error="",
            )
        )

        read_loop = _make_agent_loop(
            workspace,
            config,
            generator=read_generator,
            runtime=read_runtime,
        )

        read_result = await read_loop.process_turn(
            message="show /tmp/clambot_int_test.txt",
            session_key="test:user1",
        )

        assert read_result.status == "completed"
        assert "integration test data" in read_result.content

    @pytest.mark.asyncio
    async def test_read_nonexistent_file_returns_error(self, tmp_path: Path) -> None:
        """Reading a file that doesn't exist surfaces the error from the
        fs tool through the pipeline."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)

        mock_generator = AsyncMock()
        mock_generator.generate = AsyncMock(
            return_value=GenerationResult(
                language="javascript",
                script=FS_READ_SCRIPT,
                declared_tools=["fs"],
                inputs={"path": "/tmp/nonexistent_clambot.txt"},
                metadata={"description": "read file"},
            )
        )

        mock_runtime = AsyncMock()
        mock_runtime.execute = AsyncMock(
            return_value=RuntimeResult(
                output="File not found: /tmp/nonexistent_clambot.txt",
                error="",
            )
        )

        mock_analyzer = AsyncMock()
        mock_analyzer.analyze = AsyncMock(
            return_value=AnalysisResult(
                decision=PostRuntimeAnalysisDecision.ACCEPT,
                output="File not found: /tmp/nonexistent_clambot.txt",
                reason="Tool returned file-not-found — surface to user",
            )
        )

        agent_loop = _make_agent_loop(
            workspace,
            config,
            generator=mock_generator,
            runtime=mock_runtime,
            analyzer=mock_analyzer,
        )

        result = await agent_loop.process_turn(
            message="show /tmp/nonexistent_clambot.txt",
            session_key="test:user1",
        )

        assert result.status == "completed"
        assert "not found" in result.content.lower() or "nonexistent" in result.content.lower()


# ---------------------------------------------------------------------------
# Test: Generation rules include fs examples
# ---------------------------------------------------------------------------


class TestGenerationRulesIncludeFsExamples:
    """System prompt generation rules contain fs tool usage examples."""

    def test_system_prompt_has_fs_read_example(self, tmp_path: Path) -> None:
        """The generation rules section includes an fs read example."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)
        builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )
        prompt = builder.build_system_prompt()
        assert 'fs({operation: "read"' in prompt

    def test_system_prompt_has_fs_write_example(self, tmp_path: Path) -> None:
        """The generation rules section includes an fs write example."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)
        builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )
        prompt = builder.build_system_prompt()
        assert 'fs({operation: "write"' in prompt

    def test_system_prompt_has_fs_list_example(self, tmp_path: Path) -> None:
        """The generation rules section includes an fs list example."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)
        builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )
        prompt = builder.build_system_prompt()
        assert 'fs({operation: "list"' in prompt

    def test_system_prompt_forbids_require(self, tmp_path: Path) -> None:
        """The generation rules explicitly forbid require()."""
        workspace = _make_workspace(tmp_path)
        config = _make_config(workspace)
        builder = ContextBuilder(
            workspace=workspace,
            memory_budget_config=config.agents.memory_prompt_budget,
        )
        prompt = builder.build_system_prompt()
        assert "require()" in prompt.lower() or "no `require()`" in prompt.lower()
