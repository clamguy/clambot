"""Agent bootstrap — factory for building a fully-configured AgentLoop.

Creates all the components needed for the agent pipeline from a
ClamBotConfig, wiring providers, registries, and services together.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from clambot.config.schema import ClamBotConfig
from clambot.providers.factory import create_provider

from .chat_mode import ChatModeFallbackResponder
from .clams import ClamRegistry
from .context import ContextBuilder
from .loop import AgentLoop
from .provider_generation import ProviderBackedClamGenerator
from .provider_link_context import ProviderLinkContextBuilder
from .provider_post_runtime_analysis import ProviderBackedPostRuntimeAnalyzer
from .runtime import ClamRuntime
from .runtime_backend_amla_sandbox import AmlaSandboxRuntimeBackend
from .selector import ProviderBackedClamSelector

logger = logging.getLogger(__name__)


def build_provider_backed_agent_loop_from_config(
    config: ClamBotConfig,
    tool_registry: Any | None = None,
    runtime: Any | None = None,
    workspace: Path | None = None,
) -> AgentLoop:
    """Build a fully-configured AgentLoop from a ClamBotConfig.

    Creates and wires all agent pipeline components:
      - Primary provider (for generation + analysis + chat)
      - Selector provider (cheap/fast model for routing)
      - ProviderBackedClamSelector
      - ProviderBackedClamGenerator
      - ProviderBackedPostRuntimeAnalyzer
      - ChatModeFallbackResponder
      - ContextBuilder
      - ClamRegistry
      - ProviderLinkContextBuilder

    Args:
        config: Full ClamBot configuration.
        tool_registry: Pre-built tool registry.
        runtime: Pre-built ClamRuntime.
        workspace: Workspace path (overrides config default).

    Returns:
        A fully-configured AgentLoop instance.
    """
    # Resolve workspace
    ws = workspace or Path(config.agents.defaults.workspace).expanduser()

    # Create providers
    primary_provider = create_provider(config)

    selector_config = config.agents.selector
    if selector_config.model:
        selector_provider = create_provider(config, model=selector_config.model)
    else:
        selector_provider = primary_provider

    # Build selector
    selector = ProviderBackedClamSelector(
        provider=selector_provider,
        max_tokens=selector_config.max_tokens,
        temperature=selector_config.temperature,
        retries=selector_config.retries,
    )

    # Build generator
    defaults = config.agents.defaults
    generator = ProviderBackedClamGenerator(
        provider=primary_provider,
        max_tokens=defaults.max_tokens,
        temperature=defaults.temperature,
    )

    # Build analyzer
    analyzer = ProviderBackedPostRuntimeAnalyzer(
        provider=primary_provider,
    )

    # Build chat responder — with access to agent-level tools (cron,
    # web_fetch) so the LLM can call them via function calling without
    # generating a clam.
    chat_responder = ChatModeFallbackResponder(
        provider=primary_provider,
        max_tokens=defaults.max_tokens,
        temperature=defaults.temperature,
        tool_registry=tool_registry,
        agent_tools={"cron", "web_fetch"},
    )

    # Resolve model context size from config
    model_name = config.agents.defaults.model
    model_config = config.agents.models.get(model_name)
    model_context_size = model_config.max_context_size if model_config else 100_000

    # Build context builder
    context_builder = ContextBuilder(
        workspace=ws,
        memory_budget_config=config.agents.memory_prompt_budget,
        model_context_size=model_context_size,
    )

    # Build clam registry
    clam_registry = ClamRegistry(workspace=ws)

    # Build link context builder
    link_config = config.agents.link_context
    link_context_builder = ProviderLinkContextBuilder(
        max_links=link_config.max_links,
        max_chars_per_link=link_config.max_chars_per_link,
        enabled=link_config.enabled,
        explicit_links_only=link_config.explicit_links_only,
    )

    # Build runtime if not provided
    if runtime is None:
        runtime_backend = AmlaSandboxRuntimeBackend()
        runtime = ClamRuntime(
            backend=runtime_backend,
            tool_registry=tool_registry,
            config=config,
        )

    # Build agent loop
    return AgentLoop(
        selector=selector,
        generator=generator,
        runtime=runtime,
        analyzer=analyzer,
        tool_registry=tool_registry,
        context_builder=context_builder,
        clam_registry=clam_registry,
        memory_workspace=ws,
        link_context_builder=link_context_builder,
        chat_responder=chat_responder,
        config=config,
    )
