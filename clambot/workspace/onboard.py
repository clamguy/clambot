"""Workspace onboarding — auto-detect providers and generate config.

``onboard_workspace()`` scans environment variables for known provider
API keys, optionally probes Ollama, and generates a ``config.json``
with discovered providers filled in.  All other fields use defaults.

The function is idempotent: it only fills missing fields and never
overwrites existing values.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from clambot.config.schema import ClamBotConfig
from clambot.utils.constants import OLLAMA_DEFAULT_HOST

__all__ = ["onboard_workspace"]

logger = logging.getLogger(__name__)

# Map of env var name → (config provider section, config field)
_PROVIDER_ENV_MAP: dict[str, tuple[str, str]] = {
    "OPENROUTER_API_KEY": ("openrouter", "apiKey"),
    "ANTHROPIC_API_KEY": ("anthropic", "apiKey"),
    "OPENAI_API_KEY": ("openai", "apiKey"),
    "DEEPSEEK_API_KEY": ("deepseek", "apiKey"),
    "GROQ_API_KEY": ("groq", "apiKey"),
    "GEMINI_API_KEY": ("gemini", "apiKey"),
}

# Provider preference for default model selection (highest first).
_PROVIDER_PRIORITY: list[str] = [
    "openai",
    "anthropic",
    "openrouter",
    "deepseek",
    "gemini",
    "groq",
    "ollama",
]

# Curated model choices per provider (displayed name → model string).
# Ollama models are probed at runtime and not listed here.
_PROVIDER_MODELS: dict[str, list[tuple[str, str]]] = {
    "openai": [
        ("gpt-4.1 (recommended)", "openai/gpt-4.1"),
        ("gpt-4.1-mini (fast, cheap)", "openai/gpt-4.1-mini"),
        ("gpt-4.1-nano (fastest, cheapest)", "openai/gpt-4.1-nano"),
        ("o3 (reasoning)", "openai/o3"),
        ("o4-mini (reasoning, fast)", "openai/o4-mini"),
    ],
    "anthropic": [
        ("claude-sonnet-4 (recommended)", "anthropic/claude-sonnet-4-20250514"),
        ("claude-opus-4 (strongest)", "anthropic/claude-opus-4-20250514"),
        ("claude-haiku-3.5 (fast, cheap)", "anthropic/claude-3-5-haiku-20241022"),
    ],
    "openrouter": [
        ("anthropic/claude-sonnet-4 (recommended)", "openrouter/anthropic/claude-sonnet-4-20250514"),
        ("anthropic/claude-opus-4", "openrouter/anthropic/claude-opus-4-20250514"),
        ("openai/gpt-4.1", "openrouter/openai/gpt-4.1"),
        ("openai/gpt-4.1-mini (fast, cheap)", "openrouter/openai/gpt-4.1-mini"),
        ("google/gemini-2.5-pro", "openrouter/google/gemini-2.5-pro-preview-06-05"),
        ("google/gemini-2.5-flash (fast, cheap)", "openrouter/google/gemini-2.5-flash-preview-05-20"),
        ("deepseek/deepseek-chat-v3", "openrouter/deepseek/deepseek-chat-v3-0324"),
    ],
    "deepseek": [
        ("deepseek-chat (recommended)", "deepseek/deepseek-chat"),
        ("deepseek-reasoner (reasoning)", "deepseek/deepseek-reasoner"),
    ],
    "gemini": [
        ("gemini-2.5-pro (recommended)", "gemini/gemini-2.5-pro-preview-06-05"),
        ("gemini-2.5-flash (fast, cheap)", "gemini/gemini-2.5-flash-preview-05-20"),
        ("gemini-2.0-flash", "gemini/gemini-2.0-flash"),
    ],
    "groq": [
        ("llama-3.3-70b-versatile (recommended)", "groq/llama-3.3-70b-versatile"),
        ("llama-3.1-8b-instant (fast)", "groq/llama-3.1-8b-instant"),
        ("gemma2-9b-it", "groq/gemma2-9b-it"),
    ],
}

# Recommended cheap/fast selector model per provider.  Used for the
# routing-only selector call — should be the cheapest viable model.
# ``None`` means "use the same model as the primary" (no separate selector).
_SELECTOR_MODELS: dict[str, str | None] = {
    "openai": "openai/gpt-4.1-nano",
    "anthropic": "anthropic/claude-3-5-haiku-20241022",
    "openrouter": "openrouter/google/gemini-2.0-flash-001",
    "deepseek": None,  # only one cheap model
    "gemini": "gemini/gemini-2.0-flash",
    "groq": None,  # already fast/free
    "ollama": None,  # use whatever the user picked
}


def _probe_ollama(api_base: str = OLLAMA_DEFAULT_HOST) -> list[str]:
    """Probe Ollama at the given base URL.

    Returns a list of available model names (empty if unreachable).
    """
    try:
        import urllib.error
        import urllib.request

        url = f"{api_base}/api/tags"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status != 200:
                return []
            data = json.loads(resp.read().decode())
            models = data.get("models", [])
            return [m["name"] for m in models if "name" in m]
    except Exception:
        return []


def _prompt_model_selection(
    provider: str,
    choices: list[tuple[str, str]],
) -> str | None:
    """Prompt the user to select a default model.

    Args:
        provider: Display name of the provider.
        choices: List of ``(display_label, model_string)`` tuples.

    Returns the full model string or ``None`` if cancelled / non-interactive.
    """
    if not choices:
        return None
    try:
        import questionary

        print(f"\n{provider} models:")
        q_choices = [questionary.Choice(title=label, value=value) for label, value in choices]
        choice = questionary.select(
            "Select a default model:",
            choices=q_choices,
        ).ask()
        return choice  # None if user cancelled (Ctrl+C)
    except Exception:
        # Non-interactive or questionary unavailable — skip selection
        return None


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay into base. Overlay values win for scalars;
    dicts are merged recursively. Empty strings in overlay do NOT overwrite."""
    result = dict(base)
    for key, val in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        elif val != "" and val is not None:
            result[key] = val
    return result


def onboard_workspace(config_path: Path | str) -> dict[str, Any]:
    """Auto-detect providers and generate or update config.json.

    Args:
        config_path: Path to config.json (created if missing).

    Returns:
        Summary dict with discovered providers, models, and config path.
    """
    config_path = Path(config_path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing config if present
    existing: dict[str, Any] = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse existing config: %s", exc)

    # Build providers overlay from environment
    providers_overlay: dict[str, Any] = {}
    configured: list[str] = []

    for env_var, (provider_name, field_name) in _PROVIDER_ENV_MAP.items():
        value = os.environ.get(env_var, "")
        if value:
            existing_provider = existing.get("providers", {}).get(provider_name, {})
            if not existing_provider.get(field_name):
                providers_overlay.setdefault(provider_name, {})[field_name] = value
                configured.append(provider_name)
                logger.info("Detected %s from %s", provider_name, env_var)

    # Probe Ollama
    ollama_base = os.environ.get("OLLAMA_HOST", OLLAMA_DEFAULT_HOST)
    ollama_models = _probe_ollama(ollama_base)
    ollama_detected = len(ollama_models) > 0
    if ollama_detected:
        existing_ollama = existing.get("providers", {}).get("ollama", {})
        if not existing_ollama.get("apiBase"):
            providers_overlay.setdefault("ollama", {})["apiBase"] = ollama_base
            configured.append("ollama")
            logger.info("Detected Ollama at %s", ollama_base)

    # --- Model selection ---
    # Prompt for default model if none is already configured.
    selected_model: str | None = None
    selected_provider: str | None = None
    existing_model = existing.get("agents", {}).get("defaults", {}).get("model", "")
    if not existing_model:
        selected_provider, selected_model = _select_default_model(configured, ollama_models)

    # Generate defaults from schema
    defaults = ClamBotConfig().model_dump(by_alias=True)

    # Merge: defaults < existing < new discoveries
    merged = _deep_merge(defaults, existing)
    if providers_overlay:
        merged_providers = _deep_merge(
            merged.get("providers", {}),
            providers_overlay,
        )
        merged["providers"] = merged_providers

    # Apply selected model + matching selector model
    if selected_model:
        merged.setdefault("agents", {}).setdefault("defaults", {})["model"] = selected_model
        logger.info("Default model set to %s", selected_model)

        # Set a cheap/fast selector model for routing if the provider has one
        existing_selector = existing.get("agents", {}).get("selector", {}).get("model", "")
        if not existing_selector and selected_provider:
            selector_model = _SELECTOR_MODELS.get(selected_provider)
            if selector_model:
                merged.setdefault("agents", {}).setdefault("selector", {})["model"] = selector_model
                logger.info("Selector model set to %s", selector_model)

    # Write config
    config_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    summary: dict[str, Any] = {
        "configured_providers": configured,
        "ollama_detected": ollama_detected,
        "ollama_models": ollama_models,
        "selected_model": selected_model,
        "config_path": str(config_path),
    }

    # Print summary
    if configured:
        print(f"Configured providers: {', '.join(configured)}")
    else:
        print("No new providers detected from environment.")
    if ollama_detected:
        print(f"Ollama detected at {ollama_base} ({len(ollama_models)} model(s))")
    if selected_model:
        print(f"Default model: {selected_model}")
    print(f"Config written to {config_path}")

    return summary


def _select_default_model(
    configured_providers: list[str],
    ollama_models: list[str],
) -> tuple[str | None, str | None]:
    """Pick the best available provider and prompt the user for a model.

    Uses ``_PROVIDER_PRIORITY`` to select the highest-priority provider
    among those that were configured, then presents model choices.

    Returns ``(provider_name, model_string)`` or ``(None, None)`` if no
    provider was found or the user cancelled.
    """
    # Find the highest-priority configured provider
    for provider in _PROVIDER_PRIORITY:
        if provider not in configured_providers:
            continue

        if provider == "ollama":
            if not ollama_models:
                continue
            choices = [(m, f"ollama/{m}") for m in ollama_models]
            model = _prompt_model_selection("Ollama", choices)
            return (provider, model) if model else (None, None)

        models = _PROVIDER_MODELS.get(provider)
        if not models:
            continue
        display = provider.replace("_", " ").title()
        model = _prompt_model_selection(display, models)
        return (provider, model) if model else (None, None)

    return (None, None)
