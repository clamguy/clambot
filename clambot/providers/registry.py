"""Provider registry — maps model string prefixes to provider config names.

The single source of truth for prefix → provider routing.  Adding a new
provider only requires a new entry in ``PROVIDER_PREFIXES`` and a matching
field on ``ProvidersConfig`` in ``config/schema.py``.
"""

from __future__ import annotations

# Map of model-string prefix (the part before the first ``/``) to the
# corresponding field name on ``ProvidersConfig``.
PROVIDER_PREFIXES: dict[str, str] = {
    "openrouter": "openrouter",
    "anthropic": "anthropic",
    "openai": "openai",
    "ollama": "ollama",
    "ollama_chat": "ollama",
    "deepseek": "deepseek",
    "groq": "groq",
    "gemini": "gemini",
    "openai-codex": "openai_codex",
    "custom": "custom",
}


def find_provider_for_model(model: str) -> str | None:
    """Return the provider config field name for a model string.

    Parses the prefix before the first ``/`` and looks it up in
    ``PROVIDER_PREFIXES``.  Returns ``None`` if no prefix match is found.

    Examples::

        >>> find_provider_for_model("anthropic/claude-sonnet-4-20250514")
        'anthropic'
        >>> find_provider_for_model("bare-model-name")
        None
    """
    if "/" not in model:
        return None
    prefix = model.split("/", 1)[0]
    return PROVIDER_PREFIXES.get(prefix)
