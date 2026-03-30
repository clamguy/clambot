# ClamBot — Configuration Reference

## Config File Location

Default: `~/.clambot/config.json`

Override with `--config` flag or `CLAMBOT_CONFIG` env var.

---

## Full Config Schema

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.clambot/workspace",
      "model": "openrouter/anthropic/claude-opus-4-5",
      "max_tokens": 8192,
      "temperature": 0.7,
      "max_tool_iterations": 20,
      "max_self_fix_attempts": 3,
      "available_tools": [
        "fs", "web_fetch", "http_request", "cron",
        "secrets_add", "memory_recall", "memory_search_history"
      ]
    },
    "compaction": {
      "enabled": true,
      "target_ratio": 0.55,
      "reserve_tokens": 2000,
      "summary_max_tokens": 600,
      "keep_recent_turns": 12,
      "max_auto_compactions_per_turn": 1
    },
    "memory_prompt_budget": {
      "max_tokens": 2000,
      "min_tokens": 256,
      "reserve_tokens": 6000,
      "max_context_ratio": 0.2,
      "durable_facts_ratio": 0.6
    },
    "models": {
      "openrouter/anthropic/claude-opus-4-5": {
        "maxContextSize": 200000
      },
      "openai/gpt-4o": {
        "maxContextSize": 128000
      }
    },
    "selector": {
      "provider": null,
      "model": "openrouter/openai/gpt-4o-mini",
      "retries": 2,
      "max_tokens": 500,
      "temperature": 0.0
    },
    "link_context": {
      "enabled": true,
      "provider": null,
      "model": null,
      "retries": 1,
      "max_links": 5,
      "max_chars_per_link": 50000,
      "explicit_links_only": true,
      "heuristic_prefetch_enabled": true,
      "intent_url_inference_enabled": true
    },
    "approvals": {
      "enabled": true,
      "interactive": true,
      "allow_always": true,
      "always_grants": [
        {
          "tool": "web_fetch",
          "scope": "host:api.openrouter.ai"
        }
      ]
    },
    "clam_env": {
      "workspace/my_clam": {
        "API_TOKEN": "my_secret_name"
      }
    }
  },
  "channels": {
    "telegram": {
      "enabled": false,
      "token": "",
      "allow_from": [],
      "proxy": null,
      "reply_to_message": false
    }
  },
  "providers": {
    "openrouter": {
      "api_key": "",
      "api_base": null,
      "extra_headers": null
    },
    "anthropic": {
      "api_key": ""
    },
    "openai": {
      "api_key": ""
    },
    "openai_codex": {
      "api_key": ""
    },
    "deepseek": {
      "api_key": ""
    },
    "gemini": {
      "api_key": ""
    },
    "ollama": {
      "api_base": "http://localhost:11434"
    },
    "custom": {
      "api_key": "",
      "api_base": ""
    }
  },
  "gateway": {
    "host": "127.0.0.1",
    "port": 18790
  },
  "security": {
    "sslFallbackInsecure": false
  },
  "heartbeat": {
    "enabled": false,
    "interval": 1800
  },
  "tools": {
    "mcp_servers": {},
    "filesystem": {
      "restrict_to_workspace": true,
      "max_read_bytes": null,
      "max_write_bytes": null
    }
  }
}
```

---

## Config Section Reference

### `agents.defaults`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `workspace` | string | `~/.clambot/workspace` | Path to workspace directory |
| `model` | string | required | Primary LLM model identifier (e.g. `openrouter/anthropic/claude-opus-4-5`) |
| `max_tokens` | int | `8192` | Max tokens for generation |
| `temperature` | float | `0.7` | Generation temperature |
| `max_tool_iterations` | int | `20` | Max tool calls per clam execution |
| `max_self_fix_attempts` | int | `3` | Max self-fix loop iterations |
| `available_tools` | list | `[]` | Tools available to generated clams |

### `agents.compaction`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable automatic session compaction |
| `target_ratio` | float | `0.55` | Compact when tokens exceed this ratio of context size |
| `reserve_tokens` | int | `2000` | Token budget reserved for response generation |
| `summary_max_tokens` | int | `600` | Max tokens for compaction summary |
| `keep_recent_turns` | int | `12` | Recent turns to keep after compaction |
| `max_auto_compactions_per_turn` | int | `1` | Max compactions triggered per turn |

### `agents.memory_prompt_budget`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_tokens` | int | `2000` | Total budget for memory injection in system prompt |
| `min_tokens` | int | `256` | Minimum required for memory to be injected |
| `reserve_tokens` | int | `6000` | Reserved for other prompt sections |
| `max_context_ratio` | float | `0.2` | Max fraction of context window for memory |
| `durable_facts_ratio` | float | `0.6` | Fraction of memory budget for durable facts vs history |

### `agents.models`

Per-model context size overrides. Key is model identifier string.

```json
"models": {
  "openrouter/anthropic/claude-opus-4-5": { "maxContextSize": 200000 },
  "openai/gpt-4o": { "maxContextSize": 128000 }
}
```

Default if not specified: `100000` tokens.

### `agents.selector`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider` | string\|null | `null` | Override provider for selector (null = use primary) |
| `model` | string | primary model | Cheaper/faster model for routing decisions |
| `retries` | int | `2` | Max retries on invalid JSON |
| `max_tokens` | int | `500` | Max tokens for selector response |
| `temperature` | float | `0.0` | Deterministic for routing |

### `agents.link_context`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable pre-generation URL fetching |
| `max_links` | int | `5` | Max URLs to fetch per turn |
| `max_chars_per_link` | int | `50000` | Max content chars per fetched URL |
| `explicit_links_only` | bool | `true` | Only fetch URLs explicitly in message |
| `heuristic_prefetch_enabled` | bool | `true` | Enable heuristic-based prefetch |
| `intent_url_inference_enabled` | bool | `true` | Enable LLM-inferred URL fetching |

### `agents.approvals`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Enable approval gate |
| `interactive` | bool | `true` | Enable interactive approval prompts |
| `allow_always` | bool | `true` | Allow "Allow Always" option |
| `always_grants` | list | `[]` | Pre-approved tool+scope combinations |

**`always_grants` entry format:**

```json
{ "tool": "web_fetch", "scope": "host:api.coinbase.com" }
{ "tool": "http_request", "scope": "host:api.openrouter.ai" }
{ "tool": "fs", "scope": "workspace" }
{ "tool": "cron", "scope": "any" }
```

### `agents.clam_env`

Maps clam names to secret-name bindings. The key is the clam path relative to workspace.

```json
"clam_env": {
  "workspace/my_crypto_clam": {
    "COINBASE_API_KEY": "coinbase_key_secret"
  }
}
```

When `my_crypto_clam` executes, the env variable `COINBASE_API_KEY` is set from the secret store entry `coinbase_key_secret`.

---

### `channels.telegram`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable Telegram channel |
| `token` | string | `""` | Bot token from @BotFather |
| `allow_from` | list | `[]` | Allowed user IDs/usernames. Empty = allow all |
| `proxy` | string\|null | `null` | SOCKS5 proxy URL (e.g. `socks5://user:pass@host:port`) |
| `reply_to_message` | bool | `false` | Reply to user's message instead of new message |

**`allow_from` format:**

Each entry is a pipe-separated string with segments matched against the source:
- `"123456789"` — exact user ID
- `"123456789|johndoe"` — user ID or username match
- `"johndoe"` — username only

---

### `providers`

Each provider has `api_key` and optionally `api_base`. Values can be set via environment variables (see below) — config file values take precedence.

| Provider | Key Env Var | Notes |
|---------|------------|-------|
| `openrouter` | `OPENROUTER_API_KEY` | `sk-or-` prefix auto-detects |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude` in model name auto-detects |
| `openai` | `OPENAI_API_KEY` | |
| `openai_codex` | set via `clambot provider login` | OAuth-managed |
| `deepseek` | `DEEPSEEK_API_KEY` | |
| `gemini` | `GEMINI_API_KEY` | |
| `ollama` | n/a | `api_base` points to Ollama instance |
| `custom` | `CUSTOM_API_KEY` | Requires `api_base` |

---

### `gateway`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `"127.0.0.1"` | Gateway HTTP host (reserved, not currently used). Defaults to localhost for security. Set to `"0.0.0.0"` to bind all interfaces if external access is needed. |
| `port` | int | `18790` | Gateway HTTP port (reserved, not currently used) |

---

### `security`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `sslFallbackInsecure` | bool | `false` | When `true`, HTTP tools and the Codex provider retry with `verify=False` on SSL certificate errors. Only enable in sandboxed/proxy environments where CA certificates are unavailable. |

> **Note:** Built-in HTTP tools (`web_fetch`, `http_request`) also enforce SSRF
> protection — requests to private/internal IP ranges (`127.0.0.0/8`,
> `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`, `::1`,
> `fc00::/7`) are blocked regardless of this setting.

---

### `heartbeat`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable heartbeat service |
| `interval` | int | `1800` | Seconds between heartbeat wakeups |

---

### `tools.filesystem`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `restrict_to_workspace` | bool | `true` | Block paths outside workspace |
| `max_read_bytes` | int\|null | `null` | Max bytes per read (null = unlimited) |
| `max_write_bytes` | int\|null | `null` | Max bytes per write (null = unlimited) |

### `tools.mcp_servers`

MCP server definitions (Claude Desktop/Cursor-compatible format):

```json
"mcp_servers": {
  "my_mcp": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
    "env": {}
  }
}
```

---

## Environment Variables

These env vars override or supplement config file values. All use `load_dotenv(override=False)` — explicit config values take precedence.

| Variable | Purpose |
|----------|---------|
| `CLAMBOT_CONFIG` | Override config file path |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `GEMINI_API_KEY` | Google Gemini API key |
| `CLAMBOT_<PROVIDER>_MODEL` | Override model for a provider (e.g. `CLAMBOT_OPENROUTER_MODEL`) |

---

## Workspace Directory Layout

```
~/.clambot/
├── config.json                    # Main configuration
├── secrets/
│   └── store.json                 # Named secrets (0700 dir, 0600 file)
├── cron/
│   └── jobs.json                  # Scheduled job definitions
└── workspace/
    ├── clams/                     # Promoted clam packages
    │   └── <name>/
    │       ├── CLAM.md
    │       └── run.js
    ├── build/                     # Staging area for new clams
    │   └── <name>/
    │       ├── CLAM.md
    │       └── run.js
    ├── sessions/                  # Conversation history (JSONL)
    │   └── <base64url_key>.jsonl
    ├── logs/
    │   └── gateway_cron_events.jsonl   # Cron execution audit log
    ├── docs/                      # Custom LLM instruction documents
    │   └── *.md
    └── memory/
        ├── MEMORY.md              # Durable facts (injected in system prompt)
        ├── HISTORY.md             # Interaction summaries (retrieval-only)
        └── HEARTBEAT.md           # Scheduled task instructions
```

---

## `.env` File

Place at project root or workspace root. Variables set here are loaded with `load_dotenv(override=False)` — explicit config values take priority.

```bash
# .env example
OPENROUTER_API_KEY=sk-or-v1-...
ANTHROPIC_API_KEY=sk-ant-...
```

---

## Test Configuration

`clambot/.env.test` is loaded by `pytest-dotenv` for tests:

```bash
# .env.test
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2
```

---

## Config File Initialization

`clambot onboard` generates a `config.json` with:
- Auto-discovered providers (scans env vars)
- Auto-detected Ollama models (probes `/api/tags`)
- Default values for all other fields

The command is idempotent — safe to re-run; only fills missing values.
