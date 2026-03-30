"""Secret value resolver — multi-source resolution chain.

``resolve_secret_value`` walks a priority-ordered chain of sources to find a
secret value, falling back gracefully from explicit args → named env var →
secret store → provider env vars → bare env var → error.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

from clambot.tools.secrets.store import SecretStore

if TYPE_CHECKING:
    from clambot.config.schema import ClamBotConfig

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "resolve_secret_value",
]

# ---------------------------------------------------------------------------
# Well-known provider env-var patterns
#
# Maps fragments that commonly appear in secret names to the environment
# variable names used by each provider.  The lookup is case-insensitive on
# the secret name.
# ---------------------------------------------------------------------------

_PROVIDER_ENV_PATTERNS: list[tuple[str, str]] = [
    # (fragment in secret name, candidate env var name)
    ("anthropic", "ANTHROPIC_API_KEY"),
    ("openai", "OPENAI_API_KEY"),
    ("openrouter", "OPENROUTER_API_KEY"),
    ("groq", "GROQ_API_KEY"),
    ("gemini", "GEMINI_API_KEY"),
    ("google", "GOOGLE_API_KEY"),
    ("deepseek", "DEEPSEEK_API_KEY"),
    ("ollama", "OLLAMA_API_KEY"),
    ("telegram", "TELEGRAM_BOT_TOKEN"),
    ("github", "GITHUB_TOKEN"),
    ("stripe", "STRIPE_SECRET_KEY"),
    ("sendgrid", "SENDGRID_API_KEY"),
    ("twilio", "TWILIO_AUTH_TOKEN"),
    ("aws", "AWS_SECRET_ACCESS_KEY"),
    ("azure", "AZURE_API_KEY"),
    ("huggingface", "HUGGINGFACE_API_KEY"),
    ("hf_", "HUGGINGFACE_API_KEY"),
    ("cohere", "COHERE_API_KEY"),
    ("mistral", "MISTRAL_API_KEY"),
    ("together", "TOGETHER_API_KEY"),
    ("replicate", "REPLICATE_API_TOKEN"),
    ("perplexity", "PERPLEXITYAI_API_KEY"),
]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_secret_value(
    name: str,
    args: dict[str, Any],
    secret_store: SecretStore,
    config: ClamBotConfig | None = None,
) -> str:
    """Resolve a secret value through a priority chain.

    Resolution order:

    1. Explicit ``value`` key in *args* — returned as-is if truthy.
    2. ``from_env`` key in *args* — the named environment variable is looked
       up via :func:`os.environ.get`.
    3. :class:`~clambot.tools.secrets.store.SecretStore` lookup by *name*.
    4. Well-known provider env-var names derived from *name* patterns
       (e.g. a name containing ``"anthropic"`` maps to
       ``ANTHROPIC_API_KEY``).  Optionally enriched by *config* provider
       ``api_key`` fields.
    5. Bare ``os.environ.get(name)`` as a final environment fallback.
    6. :exc:`RuntimeError` with message ``"input_unavailable: <name>"`` if
       nothing was found.

    Args:
        name: Canonical secret name (e.g. ``"OPENAI_API_KEY"``).
        args: Tool argument dict that may contain ``"value"`` or
              ``"from_env"`` keys.
        secret_store: Initialised :class:`~clambot.tools.secrets.store.SecretStore`
                      instance to query.
        config: Optional :class:`~clambot.config.schema.ClamBotConfig` used
                to check provider ``api_key`` fields as an additional source.

    Returns:
        The resolved secret value string.

    Raises:
        RuntimeError: If no value could be resolved from any source.
    """
    # ------------------------------------------------------------------
    # 1. Explicit value in args
    # ------------------------------------------------------------------
    explicit = args.get("value")
    if explicit:
        return str(explicit)

    # ------------------------------------------------------------------
    # 2. Named env var via from_env
    # ------------------------------------------------------------------
    from_env_key = args.get("from_env")
    if from_env_key:
        env_val = os.environ.get(str(from_env_key))
        if env_val:
            return env_val

    # ------------------------------------------------------------------
    # 3. SecretStore lookup
    # ------------------------------------------------------------------
    stored = secret_store.get(name)
    if stored:
        return stored

    # ------------------------------------------------------------------
    # 4. Provider env vars — pattern matching + optional config api_key
    # ------------------------------------------------------------------
    name_lower = name.lower()
    for fragment, candidate_env in _PROVIDER_ENV_PATTERNS:
        if fragment in name_lower:
            env_val = os.environ.get(candidate_env)
            if env_val:
                return env_val
            # Also check config provider api_key if config is available
            if config is not None:
                provider_val = _config_api_key_for_fragment(config, fragment)
                if provider_val:
                    return provider_val

    # ------------------------------------------------------------------
    # 5. load_dotenv + bare env var fallback
    # ------------------------------------------------------------------
    load_dotenv(override=False)
    env_val = os.environ.get(name)
    if env_val:
        return env_val

    # ------------------------------------------------------------------
    # 6. Nothing found — raise
    # ------------------------------------------------------------------
    raise RuntimeError(f"input_unavailable: {name}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _config_api_key_for_fragment(config: ClamBotConfig, fragment: str) -> str:
    """Return the ``api_key`` from the matching provider config, if set.

    Maps well-known name fragments to the corresponding field on
    :class:`~clambot.config.schema.ProvidersConfig`.

    Args:
        config: Root ClamBot configuration.
        fragment: Lowercase fragment string (e.g. ``"anthropic"``).

    Returns:
        The ``api_key`` string if non-empty, otherwise ``""``.
    """
    providers = config.providers
    _fragment_to_provider: dict[str, str] = {
        "anthropic": "anthropic",
        "openai": "openai",
        "openrouter": "openrouter",
        "groq": "groq",
        "gemini": "gemini",
        "google": "gemini",
        "deepseek": "deepseek",
        "ollama": "ollama",
        "custom": "custom",
    }
    provider_name = _fragment_to_provider.get(fragment)
    if provider_name is None:
        return ""
    provider_cfg = getattr(providers, provider_name, None)
    if provider_cfg is None:
        return ""
    return provider_cfg.api_key or ""
