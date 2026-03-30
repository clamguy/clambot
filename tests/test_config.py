"""Tests for clambot.config — Phase 2 Core Primitives."""

import json
from pathlib import Path

import pytest

from clambot.config.schema import AgentDefaults, ClamBotConfig, GatewayConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, data: dict) -> Path:
    """Write a JSON config file and return its path."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(data))
    return cfg_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_config_roundtrip(tmp_path: Path) -> None:
    """Values written to a JSON config file survive a load roundtrip."""
    from clambot.config.loader import load_config

    data = {
        "gateway": {"host": "127.0.0.1", "port": 9999},
        "agents": {"defaults": {"model": "anthropic/claude-3-haiku", "maxTokens": 1024}},
    }
    cfg_file = _write_config(tmp_path, data)

    cfg = load_config(path=cfg_file)

    assert isinstance(cfg, ClamBotConfig)
    assert cfg.gateway.host == "127.0.0.1"
    assert cfg.gateway.port == 9999
    assert cfg.agents.defaults.model == "anthropic/claude-3-haiku"
    assert cfg.agents.defaults.max_tokens == 1024


def test_missing_config_returns_defaults(tmp_path: Path) -> None:
    """A path that does not exist returns a default ClamBotConfig."""
    from clambot.config.loader import load_config

    nonexistent = tmp_path / "does_not_exist.json"
    cfg = load_config(path=nonexistent)

    assert isinstance(cfg, ClamBotConfig)
    # Spot-check a few defaults from the schema
    assert cfg.gateway.host == GatewayConfig().host
    assert cfg.gateway.port == GatewayConfig().port
    assert cfg.agents.defaults.max_tokens == AgentDefaults().max_tokens


def test_resolve_config_path_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAMBOT_CONFIG env var is used when no explicit path is given."""
    from clambot.config.loader import resolve_config_path

    env_path = tmp_path / "env_config.json"
    monkeypatch.setenv("CLAMBOT_CONFIG", str(env_path))

    resolved = resolve_config_path()

    assert resolved == env_path


def test_resolve_config_path_explicit_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit path argument takes priority over the env var."""
    from clambot.config.loader import resolve_config_path

    explicit = tmp_path / "explicit.json"
    monkeypatch.setenv("CLAMBOT_CONFIG", str(tmp_path / "env.json"))

    resolved = resolve_config_path(path=explicit)

    assert resolved == explicit


def test_resolve_config_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an explicit path or env var, the default path is returned."""
    from clambot.config.loader import resolve_config_path

    monkeypatch.delenv("CLAMBOT_CONFIG", raising=False)

    resolved = resolve_config_path()

    assert resolved == Path("~/.clambot/config.json").expanduser()
