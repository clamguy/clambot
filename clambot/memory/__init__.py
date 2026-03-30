"""Memory — Long-term memory (MEMORY.md + HISTORY.md).

Exports memory store operations and consolidation utilities.
"""

from clambot.memory.store import (
    memory_append_history,
    memory_recall,
    memory_save,
    memory_search_history,
)

__all__ = [
    "memory_append_history",
    "memory_recall",
    "memory_save",
    "memory_search_history",
]
