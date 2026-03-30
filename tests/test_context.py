"""Tests for Phase 12 — System Prompt + Clam Generation Rules.

Tests:
- test_system_prompt_contains_memory: Memory text appears in prompt
- test_memory_budget_truncation_at_max_tokens: Long memory truncated by max_tokens
- test_memory_skipped_below_min_tokens: Short memory skipped when below min_tokens threshold
- test_generation_rules_contain_key_constraints: Rules contain JS-only, /workspace/, object args,
  etc.
- test_tool_schemas_rendered_for_all_tools: Multiple tool schemas all appear
- test_clam_catalog_entries_in_prompt: Multiple catalog entries appear
- test_memory_budget_max_context_ratio: Memory respects max_context_ratio * model_context_size
- test_memory_budget_reserve_tokens: Reserve tokens deducted from budget
- test_workspace_docs_loaded: load_workspace_docs() reads from docs dir
- test_link_context_injected: Link context string appears in prompt
- test_generation_mode_false_excludes_rules: No generation rules when generation_mode=False
- test_empty_memory_not_injected: Whitespace-only memory produces no section
"""

from __future__ import annotations

from pathlib import Path

from clambot.agent.clams import ClamSummary
from clambot.agent.context import ContextBuilder
from clambot.config.schema import MemoryPromptBudgetConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_budget(
    max_tokens: int = 4000,
    min_tokens: int = 0,
    reserve_tokens: int = 0,
    max_context_ratio: float = 1.0,
    durable_facts_ratio: float = 0.5,
) -> MemoryPromptBudgetConfig:
    """Create a MemoryPromptBudgetConfig with explicit values."""
    return MemoryPromptBudgetConfig(
        max_tokens=max_tokens,
        min_tokens=min_tokens,
        reserve_tokens=reserve_tokens,
        max_context_ratio=max_context_ratio,
        durable_facts_ratio=durable_facts_ratio,
    )


def _make_builder(
    budget: MemoryPromptBudgetConfig | None = None,
    model_context_size: int = 100_000,
    workspace: Path | None = None,
) -> ContextBuilder:
    return ContextBuilder(
        workspace=workspace,
        memory_budget_config=budget,
        model_context_size=model_context_size,
    )


# ---------------------------------------------------------------------------
# Memory injection tests
# ---------------------------------------------------------------------------


def test_system_prompt_contains_memory() -> None:
    """Memory text appears verbatim in the assembled system prompt."""
    builder = _make_builder()
    memory = "User prefers dark mode. Favourite language is Python."
    prompt = builder.build_system_prompt(memory=memory)
    assert memory in prompt
    assert "Long-Term Memory" in prompt


def test_empty_memory_not_injected() -> None:
    """Whitespace-only memory produces no Long-Term Memory section."""
    builder = _make_builder()
    for blank in ("", "   ", "\n\n\t\n"):
        prompt = builder.build_system_prompt(memory=blank)
        assert "Long-Term Memory" not in prompt


def test_memory_budget_truncation_at_max_tokens() -> None:
    """Memory longer than max_tokens * 4 chars is truncated with a notice."""
    max_tokens = 100  # 400 chars budget
    budget = _make_budget(max_tokens=max_tokens, min_tokens=0, reserve_tokens=0)
    builder = _make_builder(budget=budget)

    # Build memory that is clearly over budget (2000 chars >> 400)
    long_memory = "x" * 2000
    prompt = builder.build_system_prompt(memory=long_memory)

    assert "Long-Term Memory" in prompt
    assert "[Memory truncated due to budget]" in prompt
    # The injected memory section should not contain the full 2000-char string
    assert "x" * 2000 not in prompt


def test_memory_skipped_below_min_tokens() -> None:
    """Short memory is skipped entirely when estimated tokens < min_tokens."""
    # min_tokens=50 means we need at least 50 * 4 = 200 chars to inject
    budget = _make_budget(min_tokens=50, max_tokens=4000, reserve_tokens=0)
    builder = _make_builder(budget=budget)

    short_memory = "Hi."  # ~0.75 estimated tokens — well below 50
    prompt = builder.build_system_prompt(memory=short_memory)
    assert "Long-Term Memory" not in prompt


def test_memory_budget_max_context_ratio() -> None:
    """Memory budget is capped by max_context_ratio * model_context_size."""
    # model_context_size=1000, ratio=0.1 → ratio budget = 100 tokens = 400 chars
    # max_tokens=4000 is larger, so ratio wins
    budget = _make_budget(max_tokens=4000, max_context_ratio=0.1, reserve_tokens=0)
    builder = _make_builder(budget=budget, model_context_size=1000)

    long_memory = "y" * 2000  # 2000 chars >> 400 char ratio budget
    prompt = builder.build_system_prompt(memory=long_memory)

    assert "Long-Term Memory" in prompt
    assert "[Memory truncated due to budget]" in prompt
    assert "y" * 2000 not in prompt


def test_memory_budget_reserve_tokens() -> None:
    """Reserve tokens are deducted from the effective budget before truncation."""
    # max_tokens=200, reserve=100 → effective=100 tokens = 400 chars
    budget = _make_budget(max_tokens=200, reserve_tokens=100, max_context_ratio=1.0)
    builder = _make_builder(budget=budget, model_context_size=100_000)

    # 1600 chars >> 400 char effective budget
    long_memory = "z" * 1600
    prompt = builder.build_system_prompt(memory=long_memory)

    assert "Long-Term Memory" in prompt
    assert "[Memory truncated due to budget]" in prompt
    assert "z" * 1600 not in prompt


# ---------------------------------------------------------------------------
# Generation rules tests
# ---------------------------------------------------------------------------


def test_generation_rules_contain_key_constraints() -> None:
    """Generation rules section contains all critical constraints."""
    builder = _make_builder()
    prompt = builder.build_system_prompt(generation_mode=True)

    assert "Clam Generation Rules" in prompt
    # JavaScript-only constraint
    assert "JavaScript" in prompt
    # Object argument syntax
    assert "await tool(" in prompt or "Object argument" in prompt or "object" in prompt.lower()
    # /workspace/ path constraint
    assert "/workspace/" in prompt
    # run(args) function
    assert "run(args)" in prompt
    # Return values
    assert "Return" in prompt or "return" in prompt


def test_generation_mode_false_excludes_rules() -> None:
    """No generation rules section when generation_mode=False."""
    builder = _make_builder()
    prompt = builder.build_system_prompt(generation_mode=False)
    assert "Clam Generation Rules" not in prompt


# ---------------------------------------------------------------------------
# Tool schema tests
# ---------------------------------------------------------------------------


def test_tool_schemas_rendered_for_all_tools() -> None:
    """All provided tool schemas appear in the system prompt."""
    tools = [
        {
            "function": {
                "name": "http_request",
                "description": "Make an HTTP request",
                "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
            }
        },
        {
            "function": {
                "name": "web_fetch",
                "description": "Fetch a web page",
                "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
            }
        },
        {
            "function": {
                "name": "fs_read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        },
    ]
    builder = _make_builder()
    prompt = builder.build_system_prompt(tools=tools)

    assert "Available Tools" in prompt
    assert "http_request" in prompt
    assert "web_fetch" in prompt
    assert "fs_read" in prompt
    assert "Make an HTTP request" in prompt
    assert "Fetch a web page" in prompt
    assert "Read a file" in prompt


# ---------------------------------------------------------------------------
# Clam catalog tests
# ---------------------------------------------------------------------------


def test_clam_catalog_entries_in_prompt() -> None:
    """All catalog entries appear in the system prompt."""
    catalog = [
        ClamSummary(
            name="send_email",
            description="Sends an email via SMTP",
            declared_tools=["http_request"],
        ),
        ClamSummary(
            name="fetch_weather",
            description="Fetches current weather data",
            declared_tools=["web_fetch", "http_request"],
        ),
        ClamSummary(
            name="list_files",
            description="Lists files in a directory",
            declared_tools=[],
        ),
    ]
    builder = _make_builder()
    prompt = builder.build_system_prompt(clam_catalog=catalog)

    assert "Available Clams" in prompt
    assert "send_email" in prompt
    assert "Sends an email via SMTP" in prompt
    assert "fetch_weather" in prompt
    assert "Fetches current weather data" in prompt
    assert "list_files" in prompt
    assert "Lists files in a directory" in prompt


# ---------------------------------------------------------------------------
# Workspace docs tests
# ---------------------------------------------------------------------------


def test_workspace_docs_loaded(tmp_path: Path) -> None:
    """load_workspace_docs() reads all .md files from the docs directory."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "overview.md").write_text("# Overview\n\nThis is the overview.", encoding="utf-8")
    (docs_dir / "api.md").write_text("# API\n\nEndpoints are documented here.", encoding="utf-8")
    # Non-md file should be ignored
    (docs_dir / "notes.txt").write_text("ignore me", encoding="utf-8")

    builder = ContextBuilder(workspace=tmp_path)
    docs = builder.load_workspace_docs()

    assert "overview" in docs
    assert "This is the overview." in docs
    assert "api" in docs
    assert "Endpoints are documented here." in docs
    assert "ignore me" not in docs


def test_workspace_docs_loaded_into_prompt(tmp_path: Path) -> None:
    """Docs loaded via load_workspace_docs() appear in the system prompt."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("# Guide\n\nFollow these steps.", encoding="utf-8")

    builder = ContextBuilder(workspace=tmp_path)
    docs = builder.load_workspace_docs()
    prompt = builder.build_system_prompt(docs=docs)

    assert "Workspace Documentation" in prompt
    assert "Follow these steps." in prompt


# ---------------------------------------------------------------------------
# Link context tests
# ---------------------------------------------------------------------------


def test_link_context_injected() -> None:
    """Link context string appears in the assembled system prompt."""
    link_ctx = "Title: Example Page\nURL: https://example.com\nContent: Hello world."
    builder = _make_builder()
    prompt = builder.build_system_prompt(link_context=link_ctx)

    assert "Pre-fetched Link Context" in prompt
    assert link_ctx in prompt


def test_link_context_absent_when_empty() -> None:
    """No link context section when link_context is empty."""
    builder = _make_builder()
    prompt = builder.build_system_prompt(link_context="")
    assert "Pre-fetched Link Context" not in prompt
