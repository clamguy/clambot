"""Agent — Core AI agent logic.

Exports the main agent pipeline components for Phase 8+.
"""

from clambot.agent.bootstrap import build_provider_backed_agent_loop_from_config
from clambot.agent.clams import Clam, ClamRegistry, ClamSummary
from clambot.agent.loop import AgentLoop, AgentResult
from clambot.agent.request_normalization import normalize_request
from clambot.agent.selector import ProviderBackedClamSelector, SelectionResult
from clambot.agent.workspace_clam_writer import WorkspaceClamPersistenceWriter

__all__ = [
    "AgentLoop",
    "AgentResult",
    "Clam",
    "ClamRegistry",
    "ClamSummary",
    "ProviderBackedClamSelector",
    "SelectionResult",
    "WorkspaceClamPersistenceWriter",
    "build_provider_backed_agent_loop_from_config",
    "normalize_request",
]
