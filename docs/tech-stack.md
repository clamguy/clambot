# ClamBot — Tech Stack

## Python Runtime Requirements

| Requirement | Value |
|-------------|-------|
| Python | `>=3.13` |
| Package manager | `uv` (recommended) |
| Build backend | `hatchling` |

---

## Core Dependencies (`clambot/pyproject.toml`)

| Package | Version | Purpose |
|---------|---------|---------|
| `amla-sandbox` | local path `../../amla-sandbox` | WASM sandbox for executing generated clams |
| `pydantic` | `>=2.12.0` | Config schema, data validation throughout |
| `typer` | `>=0.19.2` | CLI framework |
| `litellm` | `>=1.81.14` | Multi-provider LLM backend (OpenAI, Anthropic, etc.) |
| `python-telegram-bot` | `>=22.5` | Telegram Bot API integration |
| `questionary` | `>=2.1.1` | Interactive terminal prompts for approvals |
| `pyyaml` | `>=6.0.2` | CLAM.md frontmatter parsing |
| `python-dotenv` | `>=1.2.1` | `.env` file loading |
| `oauth-cli-kit` | `>=0.1.3` | OAuth browser flow for OpenAI Codex login |

---

## Testing Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `pytest` | `>=9.0.2` | Test runner |
| `pytest-async` | `>=0.1.1` | Async test support |
| `pytest-dotenv` | `>=0.5.2` | `.env.test` loading for tests |

---

## amla-sandbox Library (`amla-sandbox/pyproject.toml`)

| Package | Version | Purpose |
|---------|---------|---------|
| `wasmtime` | `>=29.0.0` | WASM runtime (executes QuickJS WASM binary) |
| `cryptography` | `>=43.0.0` | PCA (ephemeral authority) token generation |

### Optional amla-sandbox Extras

| Extra | Packages | Purpose |
|-------|---------|---------|
| `[codeact]` | `langgraph-codeact>=0.0.1`, `langchain>=0.3.0` | CodeAct integration pattern |
| `[langgraph]` | `langgraph>=0.2.0`, `langchain-anthropic>=0.3.0`, `langchain-openai>=0.3.0` | LangGraph integration |

---

## JavaScript Runtime Inside Sandbox

The WASM binary bundled in `amla-sandbox/src/amla_sandbox/_wasm/` contains:

| Component | Details |
|-----------|---------|
| JS engine | QuickJS |
| JS spec | ES2020 (full async/await support) |
| Shell builtins available | `grep`, `jq`, `tr`, `head`, `tail`, `sort`, `uniq`, `wc`, `cut`, `cat` |
| Network access | None by default |
| Filesystem | Virtual (WASI-based): `/workspace` + `/tmp` writable, `/` read-only |

---

## LLM Providers

ClamBot supports these LLM backends through the provider registry:

| Provider | Backend Library | Key Models |
|----------|----------------|------------|
| OpenRouter | LiteLLM | Any model (pass-through to 200+ models) |
| OpenAI | LiteLLM | GPT-4o, GPT-5, etc. |
| Anthropic | LiteLLM | Claude Opus, Sonnet, Haiku |
| DeepSeek | LiteLLM | deepseek-chat, deepseek-coder |
| Google Gemini | LiteLLM | gemini-1.5-pro, gemini-2.0-flash |
| Ollama | LiteLLM | Any locally-served model |
| OpenAI Codex | Custom OAuth streaming | GPT-5 Codex (Responses API) |
| Custom | Direct OpenAI-compatible | Any OpenAI-compatible endpoint |

---

## External Services

### Required

| Service | Purpose | Config Location |
|---------|---------|-----------------|
| At least one LLM provider | All agent operations | `providers.<name>.api_key` |

### Optional

| Service | Purpose | Config Location |
|---------|---------|-----------------|
| Telegram Bot API | Chat channel | `channels.telegram.token` |
| Any additional LLM provider | Selector model, custom | `providers.<name>.api_key` |

---

## Python Standard Library Usage

Key standard library modules used:

| Module | Usage |
|--------|-------|
| `asyncio` | Event loop, queues, tasks |
| `concurrent.futures` | Thread pool for off-loop agent execution |
| `threading` | Daemon thread for background event loop |
| `hashlib` | SHA-256 for approval scope fingerprinting |
| `base64` | URL-safe encoding for session key filenames |
| `pathlib` | File path operations throughout |
| `json` | Config, session, cron, secrets persistence |
| `zoneinfo` | IANA timezone support for cron expressions |
| `getpass` | Hidden terminal input for secrets |
| `uuid` | Correlation IDs, job IDs, run IDs |
| `re` | Request normalization, cron expression validation |
| `unicodedata` | NFKC normalization for request matching |
| `dataclasses` | Frozen dataclasses for message types |

---

## nanobot Reference Implementation Tech Stack

The `/nanobot` directory contains an unsecured reference implementation with a broader channel set:

| Package | Version | Purpose |
|---------|---------|---------|
| `typer` | `>=0.20.0` | CLI |
| `litellm` | `>=1.81.5` | LLM backend |
| `python-telegram-bot[socks]` | `>=22.0` | Telegram |
| `httpx` | `>=0.28.0` | HTTP client |
| `readability-lxml` | `>=0.8.4` | HTML readability for web_fetch |
| `croniter` | `>=6.0.0` | Cron expression parsing |
| `rich` | `>=14.0.0` | Rich terminal output |
| `mcp` | `>=1.26.0` | Model Context Protocol |
| `dingtalk-stream` | `>=0.24.0` | DingTalk channel |
| `lark-oapi` | `>=1.5.0` | Feishu/Lark channel |
| `slack-sdk` | `>=3.39.0` | Slack channel |
| `qq-botpy` | `>=1.2.0` | QQ channel |
| `websockets` | `>=16.0` | WebSocket channels |
| `python-socketio` | `>=5.16.0` | Socket.IO for WhatsApp bridge |
| `loguru` | `>=0.7.3` | Logging |
| `json-repair` | `>=0.57.0` | Malformed JSON fixing |

### WhatsApp Bridge (nanobot)

| Component | Details |
|-----------|---------|
| Runtime | Node.js >=18 |
| Language | TypeScript |
| Library | `whatsapp-web.js` |
| Protocol | Socket.IO WebSocket bridge to Python |

---

## Development Tooling

| Tool | Purpose |
|------|---------|
| `uv` | Fast Python package manager and virtual environment |
| `opencode` (opencode.ai) | Multi-agent development orchestration |
| `opencode.json` | Multi-agent config: codex orchestrator + core-developer + core-tester |
| `feedback_loop.sh` | 100-iteration automated dev loop (opencode → test → fix) |

### opencode.json Multi-Agent Config

```json
{
  "model": "claude-4-5",
  "agents": {
    "core-developer": {
      "model": "claude-4-5",
      "permissions": { "edit": ["clambot/**"] }
    },
    "core-tester": {
      "model": "claude-4-5",
      "permissions": { "bash": ["uv pytest ..."] }
    }
  }
}
```

---

## File Formats

| Format | Usage |
|--------|-------|
| JSON | Config, secrets store, cron jobs store |
| JSONL (append-only) | Session history, cron event logs |
| Markdown + YAML frontmatter | CLAM.md clam metadata |
| JavaScript (ES2020) | Generated clam scripts (`run.js`) |
| Markdown | Memory files, workspace docs, HEARTBEAT.md |
| `.env` | Local environment variable overrides |
