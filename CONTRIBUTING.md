# Contributing to ClamBot

Thank you for your interest in contributing to ClamBot! This guide covers everything you need to get started.

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager
- **Git**
- **ffmpeg** (optional, only needed for the `transcribe` tool when audio > 25MB)

## Development Setup

```bash
# Clone the repository
git clone https://github.com/clamguy/clambot.git
cd clambot

# Install in development mode (includes test/lint dependencies)
uv pip install -e "clambot/[dev]"

# Verify installation
clambot status
```

## Running Tests

```bash
cd clambot && uv run pytest tests/ -x -v
```

- `-x` stops on first failure for faster feedback
- `-v` enables verbose output
- Tests use `pytest-asyncio` (auto mode) and `pytest-dotenv` for environment loading
- Integration tests requiring Ollama are skipped automatically when unavailable

## Linting & Formatting

ClamBot uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
# Check for lint errors
cd clambot && uv run ruff check .

# Auto-fix lint errors
cd clambot && uv run ruff check --fix .

# Format code
cd clambot && uv run ruff format .

# Type checking (incremental, not enforced yet)
cd clambot && uv run mypy clambot/ --ignore-missing-imports
```

### Pre-commit Hooks

A `.pre-commit-config.yaml` is provided at the repo root. To enable:

```bash
pip install pre-commit
pre-commit install
```

This runs Ruff lint and format checks automatically before each commit.

## Code Conventions

### Architecture

- **Rely on LLM capabilities** — avoid hardcoded logic, rule-based heuristics, and static mappings
- **Integration tests over unit tests** — test system behavior, not implementation details
- **No over-engineering** — lean modules, clear interfaces, defer complexity until needed
- See [docs/architecture.md](docs/architecture.md) for the full system architecture

### Style

- **Line length:** 100 characters
- **Target Python:** 3.11+
- **Type hints:** encouraged but not strictly enforced (`disallow_untyped_defs = false`)
- **Async-first:** all I/O-bound operations should be async where possible
- **Dataclasses:** prefer frozen dataclasses for immutable data types
- **Pydantic v2:** used for config schemas and validation

### Module Structure

```
clambot/clambot/
  agent/       # Core AI agent logic (loop, selector, generator, runtime, approvals)
  bus/         # Async message routing
  channels/    # Chat channel integrations (Telegram)
  cli/         # Typer CLI commands
  config/      # Config schema (Pydantic) + loader
  cron/        # Cron scheduling subsystem
  gateway/     # Gateway orchestrator
  heartbeat/   # Proactive scheduled wakeup
  memory/      # Long-term memory (MEMORY.md + HISTORY.md)
  providers/   # LLM provider layer (LiteLLM, Codex, custom)
  session/     # Conversation session management (JSONL)
  tools/       # Built-in tool implementations
  utils/       # Shared utilities
  workspace/   # Workspace bootstrap + onboarding
```

### Testing

- Test files go in `clambot/tests/`; integration tests in `clambot/tests/integration/`
- Mock at the LLM boundary (patch `litellm.acompletion`), not internal methods
- Use `pytest-asyncio` for async tests (auto mode enabled)
- Use `tmp_path` fixture for temporary directories, never hardcoded `/tmp` paths

## Pull Request Process

1. **Fork** the repository and create a feature branch from `main`
2. **Write code** following the conventions above
3. **Add tests** for new functionality (integration-level preferred)
4. **Run the full test suite** and ensure it passes: `cd clambot && uv run pytest tests/ -x -v`
5. **Run linting**: `cd clambot && uv run ruff check . && uv run ruff format --check .`
6. **Submit a PR** with a clear description of the changes and motivation

### Commit Style

- Use concise, descriptive commit messages
- Prefix with the area of change when helpful (e.g., `agent: fix self-fix retry count`, `tools: add SSRF protection`)
- Reference issue numbers where applicable

## Versioning

ClamBot follows [Semantic Versioning (SemVer)](https://semver.org/):

- **MAJOR** (`X.0.0`) — incompatible API or config schema changes
- **MINOR** (`0.X.0`) — new features, backward-compatible
- **PATCH** (`0.0.X`) — bug fixes, backward-compatible

The current version is defined in `clambot/pyproject.toml` under `version`.

Release tags follow the format `vX.Y.Z` (e.g., `v0.1.0`).

## Getting Help

- Read the [project documentation](docs/README.md) for an overview
- See [docs/architecture.md](docs/architecture.md) for system design details
- See [docs/configuration.md](docs/configuration.md) for config reference
- Open an [issue](https://github.com/clamguy/clambot/issues) for questions or bug reports
