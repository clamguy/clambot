"""ClamBot configuration loader."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .schema import ClamBotConfig

_DEFAULT_CONFIG_PATH = Path("~/.clambot/config.json")


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Return the resolved config file path.

    Priority:
    1. Explicit ``path`` argument
    2. ``CLAMBOT_CONFIG`` environment variable
    3. Default: ``~/.clambot/config.json``
    """
    if path is not None:
        return Path(path).expanduser()

    env_path = os.environ.get("CLAMBOT_CONFIG")
    if env_path:
        return Path(env_path).expanduser()

    return _DEFAULT_CONFIG_PATH.expanduser()


def load_config(path: str | Path | None = None) -> ClamBotConfig:
    """Load and return the ClamBot configuration.

    Loads ``.env`` first (without overriding existing env vars), then reads
    the JSON config file if it exists.  The JSON file uses camelCase keys.
    """
    load_dotenv(override=False)

    config_path = resolve_config_path(path)

    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        cfg = ClamBotConfig.model_validate(raw)
    else:
        cfg = ClamBotConfig()

    # Pre-populate thinking model cache from config.
    from clambot.providers.litellm_provider import load_think_disabled_models

    load_think_disabled_models(config_path)

    return cfg
