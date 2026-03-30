"""Auto-compaction logic for session history.

When the estimated token count of the active history exceeds a configurable
threshold, older turns are summarised by an LLM call and replaced with a
single ``AUTO-COMPACTION SUMMARY`` system turn. The most-recent turns are
always kept verbatim.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from clambot.session.contract import build_compaction_prompt
from clambot.session.types import SessionTurn

if TYPE_CHECKING:
    from clambot.config.schema import CompactionConfig
    from clambot.providers.base import LLMProvider
    from clambot.session.manager import SessionManager

log = logging.getLogger(__name__)

_DEFAULT_MAX_CONTEXT_TOKENS = 100_000


async def maybe_auto_compact_session(
    session_manager: SessionManager,
    key: str,
    config: CompactionConfig,
    provider: LLMProvider,
    max_context_tokens: int = _DEFAULT_MAX_CONTEXT_TOKENS,
) -> bool:
    """Compact the session history if it exceeds the token budget.

    Token count is estimated cheaply as ``sum(len(t.content) for t in turns) // 4``.
    If the estimate exceeds ``max_context_tokens * config.target_ratio``, the
    turns older than the most-recent ``config.keep_recent_turns`` are
    summarised via *provider* and replaced with a single compaction-summary
    system turn.

    The compaction summary turn is:
    - Injected at the front of the in-memory cache (followed by the kept
      recent turns).
    - Appended to the JSONL file on disk so the summary survives restarts.

    Args:
        session_manager: The :class:`~clambot.session.manager.SessionManager`
                         instance managing this session.
        key: Raw session key.
        config: :class:`~clambot.config.schema.CompactionConfig` controlling
                thresholds and behaviour.
        provider: LLM provider used to generate the summary.
        max_context_tokens: Hard upper bound on context size (tokens).

    Returns:
        ``True`` if compaction was performed, ``False`` otherwise.
    """
    if not config.enabled:
        return False

    turns = session_manager.load_history(key)
    if not turns:
        return False

    estimated_tokens = sum(len(t.content) for t in turns) // 4
    threshold = max_context_tokens * config.target_ratio

    if estimated_tokens <= threshold:
        log.debug(
            "Session %r: estimated %d tokens ≤ threshold %.0f — skipping compaction",
            key,
            estimated_tokens,
            threshold,
        )
        return False

    keep = config.keep_recent_turns
    if keep >= len(turns):
        # Nothing old enough to summarise
        log.debug(
            "Session %r: all %d turns are within keep_recent_turns=%d — skipping compaction",
            key,
            len(turns),
            keep,
        )
        return False

    older_turns = turns[:-keep] if keep > 0 else turns
    recent_turns = turns[-keep:] if keep > 0 else []

    log.info(
        "Session %r: compacting %d older turns (keeping %d recent, estimated %d tokens)",
        key,
        len(older_turns),
        len(recent_turns),
        estimated_tokens,
    )

    prompt = build_compaction_prompt(older_turns)
    response = await provider.acomplete(
        prompt,
        max_tokens=config.summary_max_tokens,
    )

    summary_content = f"AUTO-COMPACTION SUMMARY\n\n{response.content}"
    summary_turn = SessionTurn(
        role="system",
        content=summary_content,
        timestamp=time.time(),
        metadata={"_type": "compaction_summary"},
    )

    # Replace both in-memory cache and on-disk JSONL with the compacted view.
    # This prevents the JSONL from ballooning after repeated compactions —
    # on reload the session will contain only [summary + recent_turns].
    compacted_turns = [summary_turn] + list(recent_turns)
    session_manager.rewrite_session(key, compacted_turns)

    log.info("Session %r: compaction complete", key)
    return True
