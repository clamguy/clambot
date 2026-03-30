# ClamBot — Features

## 1. Clam System (Core Primitive)

"Clams" are the fundamental execution unit — named, versioned JavaScript packages stored on disk.

### Structure

```
~/.clambot/workspace/clams/<name>/
├── CLAM.md       # Metadata (YAML frontmatter) + description
└── run.js        # Executable JavaScript
```

### CLAM.md Frontmatter Fields

```yaml
---
manifest_version: "1"
description: "Fetches the price of a cryptocurrency from CoinGecko"
language: javascript
tools:
  - http_request
inputs:
  - name: symbol
    type: string
    description: "Coin symbol (e.g. BTC)"
metadata:
  reusable: true
  source_request: "what is the price of BTC"
---
```

| Field | Required | Description |
|-------|----------|-------------|
| `manifest_version` | yes | Schema version, always `"1"` |
| `description` | yes | Human-readable clam purpose |
| `language` | yes | Must be `javascript` (only supported language) |
| `tools` | yes | Declared tools the script will use |
| `inputs` | no | JSON Schema inputs for `run(args)` function |
| `metadata.reusable` | no | If `true`, eligible for exact-match reuse |
| `metadata.source_request` | no | Normalized request string used for reuse matching |

### Clam Lifecycle

1. **Generation** — LLM creates script + CLAM.md → saved to `build/<name>/`
2. **Execution** — WASM sandbox runs the script
3. **Promotion** — On successful post-runtime analysis, `build/<name>/` → `clams/<name>/`
4. **Reuse** — Future identical requests load the existing clam without LLM call

---

## 2. Clam Selector Router

Two-stage routing determines what to do with each user request.

### Stage 1: Pre-Selection (No LLM)

Before any LLM call, the system checks if an existing reusable clam exactly matches the current request:
- Unicode NFKC normalization + punctuation stripping applied to both sides
- If match found: clam selected immediately, selector LLM call skipped
- Benefit: zero latency, zero cost for repeated requests

### Stage 2: LLM Selection

The selector sends a lightweight prompt to a cheap/fast model (e.g., `gpt-5-mini`) with:
- System prompt with routing rules
- Clam catalog (all available clams with descriptions)
- Recent conversation history + extracted URLs from history
- Link context (pre-fetched URL content if any)
- Current user message

Returns JSON: `{decision, clam_id, reason, chat_response}`

| Decision | Meaning |
|----------|---------|
| `select_existing` | Reuse an existing clam by `clam_id` |
| `generate_new` | Generate a new clam script |
| `chat` | Answer directly without code (for questions, clarifications) |

### Self-Repair

If the selector returns invalid JSON, one automatic retry embeds the prior bad response in a repair prompt.

### Override Rules

- If selector says `chat` but message looks like a file operation (contains grep/read + path hint + `fs` tool available) → overrides to `generate_new`
- Cron routing guidance injected when `cron` tool is available

---

## 3. Clam Generator

When the selector decides `generate_new`, the generator produces the JavaScript script.

### Input to LLM

- Core identity instructions (JS-only, WASM constraints)
- Workspace documentation files (from `workspace/docs/`)
- Memory content (MEMORY.md durable facts)
- Tool metadata (all available tools with schemas)
- Clam catalog (to avoid duplicating existing functionality)
- Compatibility rules (no `require()`, no native modules, object-only tool calls)
- Conversation history
- Link context (pre-fetched URL content if any)

### Output Schema

```json
{
  "language": "javascript",
  "script": "async function run(args) { ... }",
  "declared_tools": ["http_request"],
  "inputs": [{"name": "symbol", "type": "string"}],
  "metadata": {
    "reusable": true,
    "source_request": "what is the price of BTC"
  }
}
```

### Generation Rules Enforced

- Only JavaScript — shell scripts rejected at compatibility check
- Only tools explicitly declared in `declared_tools`
- No network access from JS directly; must use `http_request` tool
- For host `fs` tool calls, use workspace-relative host paths (for example `notes/todo.md`) or absolute host paths inside workspace
- `/workspace/...` is a sandbox VFS namespace and is rejected by the host `fs` tool path resolver
- Use `await tool({param: value})` object syntax (not positional args)
- Tool names use underscores (`http_request` not `http.request`)

---

## 4. Clam Runtime (WASM Execution)

`ClamRuntime` wraps `AmlaSandboxRuntimeBackend` which wraps the amla-sandbox library.

### Pre-Flight Checks

Before execution:
1. **Secret requirements** — `_resolve_pre_runtime_secret_error()` verifies all secrets declared in `secret_requirements` are available; blocks with `pre_runtime_secret_requirements_unresolved` if missing
2. **Compatibility** — `CompatibilityChecker.check()` rejects non-JavaScript languages
3. **Capability policy** — validates clam metadata capability constraints

### Execution Flow

1. `Sandbox.start()` — initializes QuickJS WASM instance
2. `Sandbox.execute(script)` — script runs asynchronously inside WASM
3. On tool yield: `_handle_tool_call()` dispatches to Python host
4. Tool result returned, `Sandbox.resume(result)` called
5. Script completes or errors

### Timeout and Cancellation

- Background watchdog thread monitors elapsed time
- After `timeout_seconds`: `ClamRuntimeCancellationToken` set
- After `cancellation_grace_seconds` (default 2s): force `Sandbox.abort()`
- Error code: `runtime_timeout_unresponsive`

### Runtime Events

```
runtime.started
backend.execute_started
backend.tool_call     (per tool invocation)
backend.tool_result   (per tool result)
backend.execute_finished
runtime.completed / runtime.failed
runtime.awaiting_approval  (when approval needed)
```

### Large Script Handling

Scripts > 7KB are piped via stdin instead of inline command to avoid shell argument length limits.

---

## 5. Post-Runtime Analysis and Self-Fix Loop

After clam execution, an LLM analyzes whether the output is acceptable.

### Analyzer Input

- User message (original intent)
- Generated script
- Runtime output + error
- Run log summary
- Tool result context
- Tool contracts (expected behavior)

### Decision Types

| Decision | Action |
|----------|--------|
| `ACCEPT` | Output accepted; optionally transform it; promote clam to `clams/` |
| `SELF_FIX` | Re-generate with fix instructions embedded in prompt |
| `REJECT` | Return error to user |

### Self-Fix Loop

- On `SELF_FIX`: rebuilds user message with `SELF_FIX_RUNTIME:` prefix + previous script, output, error, and fix instructions
- Re-enters agent loop (selector will `generate_new`)
- Limit: `max_self_fix_attempts=3` (configurable; CLI interactive capped at 1)
- After 2 consecutive empty-output completed runs: injects `SELF_FIX_FORCE_GENERATE_NEW` to prevent reuse of cached clam

### Skip Conditions

Post-runtime analysis is skipped for:
- Pre-selected reusable clams with successful structured JSON output (trusted auto-accept)

---

## 6. Interactive Tool Approvals

Every tool call from a generated clam goes through the approval gate.

### Approval Configuration

```json
"approvals": {
  "enabled": true,
  "interactive": true,
  "allow_always": true,
  "always_grants": [
    {"tool": "web_fetch", "scope": "host:api.coinbase.com"}
  ]
}
```

### Decision Flow

```
Tool call arrives
│
├─ Check always_grants (exact scope match) → ALLOW immediately
│
├─ No match → check interactive mode
│   ├─ interactive=false → DENY (unless allow_always grant exists)
│   └─ interactive=true → prompt user
│       ├─ Allow Once  → allow this invocation, scope={run_id, tool, args_hash}
│       ├─ Allow Always (with scoped options) → persist to config.always_grants
│       └─ Reject → deny, clam receives error
```

### Scope Fingerprint

A SHA-256 hash of `tool_name + canonicalized_arguments` uniquely identifies each approval request. Ensures "Allow Always" grants match only equivalent future calls, not arbitrary calls to the same tool.

### Approval Scope Options (Per Tool)

Each tool provides granularity options for "Allow Always":

| Tool | Options |
|------|---------|
| `web_fetch` | Exact URL, same host (`host:*`), same path prefix |
| `http_request` | Exact URL+method, same host, same path |
| `fs` | Exact file path, parent directory, entire workspace |
| `cron` | Exact operation, any cron operation |
| `secrets_add` | Exact secret name, any secrets operation |

### Terminal Approval UI

Uses `questionary` library with styled option highlighting:

```
Tool call: web_fetch
URL: https://api.coinbase.com/v2/prices/BTC-USD/spot

  > Allow Once
    Allow Always: exact URL
    Allow Always: host api.coinbase.com
    Allow Always: path api.coinbase.com/v2/prices/*
    Reject
```

### Telegram Approval UX

Inline keyboard buttons sent to Telegram chat. See [telegram-ux.md](./telegram-ux.md) for full details.

### Resume After Approval

After user grants approval, the original inbound message is re-queued with:
- `approval_resume=True` (skips session append to prevent duplicate user turns)
- `one_time_approval_grants` attached (pre-authorized for the specific tool call)
- Approval keyboard message deleted from Telegram

---

## 7. Session Management

Conversations are persisted per-context as append-only JSONL files.

### Storage Format

```
sessions/
  <base64url(telegram:123456789)>.jsonl
  <base64url(cron:job-abc)>.jsonl
  ...
```

Line 1 (metadata):
```json
{"_type": "metadata", "key": "telegram:123456789", "created_at": "2025-01-01T00:00:00Z", "metadata": {}}
```

Subsequent lines (turns):
```json
{"role": "user", "content": "what is BTC price?", "timestamp": "...", "metadata": {}}
{"role": "assistant", "content": "BTC is $67,432", "timestamp": "...", "metadata": {
  "correlation_id": "uuid",
  "selection_reason": "generate_new",
  "runtime": {"status": "completed"},
  "post_runtime_analysis": {"decision": "ACCEPT"}
}}
```

### Session Commands

- `/new` — consolidate memory + reset session (history preserved in HISTORY.md)
- Session keys are base64url-encoded; legacy `:` → `_` format auto-migrated on load

---

## 8. Session Compaction

Prevents context window overflow in long conversations.

### Trigger

When estimated token count > `max_context_size - reserve_tokens`.

### Process

1. LLM generates a summary of older turns (up to `summary_max_tokens`)
2. Summary injected as a `system` turn with `AUTO-COMPACTION SUMMARY` marker
3. Older turns removed from in-memory history
4. Memory consolidation: durable facts extracted → appended to HISTORY.md, MEMORY.md updated

### Configuration

```json
"compaction": {
  "enabled": true,
  "target_ratio": 0.55,
  "reserve_tokens": 2000,
  "summary_max_tokens": 600,
  "keep_recent_turns": 12,
  "max_auto_compactions_per_turn": 1
}
```

---

## 9. Long-Term Memory

Two-file system for persistent agent memory.

### MEMORY.md — Durable Facts

- Auto-injected into every system prompt
- Contains user preferences, frequently-used settings, important context
- Updated via memory consolidation (LLM extraction from session summaries)
- Budget-constrained: `memory_prompt_budget.max_tokens` caps injection size

### HISTORY.md — Interaction Summaries

- Append-only record of past interactions
- Searchable via `memory_search_history(query)` tool (substring match)
- Never injected directly into prompts (retrieval-only)

### Memory Tools (available to generated clams)

| Tool | Description |
|------|-------------|
| `memory_recall()` | Returns full MEMORY.md content |
| `memory_search_history(query, limit=10)` | Substring search over HISTORY.md entries |

### Consolidation

During session compaction or `/new` command:
1. LLM called with strict JSON contract: `{history_entry, memory_update}`
2. `history_entry` appended to HISTORY.md
3. `memory_update` replaces MEMORY.md if content changed

---

## 10. Provider Layer

Multi-provider LLM support with registry-driven selection.

### Available Providers

| Provider ID | Backend | Auto-Detection |
|-------------|---------|---------------|
| `openrouter` | LiteLLM | `OPENROUTER_API_KEY` env or `sk-or-` key prefix |
| `anthropic` | LiteLLM | `ANTHROPIC_API_KEY` env or `claude` in model name |
| `openai` | LiteLLM | `OPENAI_API_KEY` env |
| `deepseek` | LiteLLM | `deepseek` in model name |
| `gemini` | LiteLLM | `GEMINI_API_KEY` env or `gemini` in model name |
| `ollama` | LiteLLM | Probed via `/api/tags` at configured `api_base` |
| `openai_codex` | OAuth streaming Responses API | `openai_codex` explicit |
| `custom` | Direct OpenAI-compatible | Explicit `api_base` + `api_key` |

### Model Configuration

```json
"agents": {
  "defaults": {
    "model": "openrouter/anthropic/claude-opus-4-5",
    "max_tokens": 8192
  },
  "selector": {
    "model": "openrouter/openai/gpt-4o-mini",
    "max_tokens": 500,
    "temperature": 0.0
  }
}
```

Primary model: generation, post-runtime analysis, chat mode, memory consolidation  
Selector model: routing only (cheaper/faster)

### OAuth Login (Codex)

```bash
clambot provider login openai-codex
# Opens browser → OAuth flow → stores access token in config
```

---

## 11. Host-Managed Secrets

Secrets stored outside the workspace with strict permissions.

### Storage

`~/.clambot/secrets/store.json` (directory: `0700`, file: `0600`)

```json
{
  "my_api_key": {
    "name": "my_api_key",
    "value": "sk-...",
    "description": "API key for external service",
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:00Z"
  }
}
```

### Resolution Order

When a clam calls `secrets_add({name: "key", ...})`:

1. Explicit `value` argument
2. `from_env` environment variable
3. Provider API key env vars (e.g., `OPENROUTER_API_KEY`)
4. `load_dotenv(override=False)` lookup
5. Hidden terminal input via `getpass.getpass()` (CLI only)
6. `input_unavailable` error (triggers Telegram `/secret name value` flow)

### Secret in HTTP Tool

```javascript
const result = await http_request({
  method: "GET",
  url: "https://api.example.com/data",
  auth: { type: "bearer_secret", name: "my_api_key" }
});
```

Secret value auto-injected as `Authorization: Bearer <token>` header.  
Value never appears in tool args, events, approval records, run logs, or traces — redacted as `[REDACTED_SECRET]`.

### Gateway Resume on Missing Secret

If execution fails with `input_unavailable`:
1. Gateway sends user: `Please provide secret: /secret my_api_key <value>`
2. User sends `/secret my_api_key sk-abc123`
3. Gateway stores secret, re-queues original inbound message
4. Agent resumes automatically

### Operator-Managed Binding

```json
"agents": {
  "clam_env": {
    "workspace/my_clam": {
      "API_TOKEN": "my_api_token"
    }
  }
}
```

Maps env variable names (as seen by the clam) to secret store entries.

---

## 12. Built-In Tools

All tools callable from generated JavaScript clams via `await tool_name({...})`.

### `fs` — Filesystem

```javascript
await fs({ op: "list",  path: "." });
await fs({ op: "read",  path: "data.json" });
await fs({ op: "write", path: "output.txt", content: "hello" });
await fs({ op: "edit",  path: "file.txt", old_text: "foo", new_text: "bar" });
```

- Relative paths resolved against workspace
- Restricted to workspace by default (`restrict_to_workspace=true`)
- Paths prefixed `/workspace/` are rejected (sandbox VFS namespace collision)

### `http_request` — Authenticated HTTP

```javascript
await http_request({
  method: "GET",
  url: "https://api.example.com/endpoint",
  headers: { "Accept": "application/json" },
  auth: { type: "bearer_secret", name: "api_key" }
});
// Returns: {ok, status_code, content_type, content, headers, truncated?, error?}
```

### `web_fetch` — URL Content Fetching

```javascript
await web_fetch({ url: "https://example.com" });
// Returns HTML/text content
```

### `cron` — Schedule Management

```javascript
await cron({ op: "add", schedule: { cron: "0 9 * * *", timezone: "UTC" },
             payload: { message: "Daily briefing" } });
await cron({ op: "list" });
await cron({ op: "remove", id: "job-abc123" });
```

Approval required for `add` and `remove`. Changes immediately synced to live scheduler.

### `secrets_add` — Secret Storage

```javascript
await secrets_add({ name: "api_key", value: "sk-abc123", description: "My API key" });
await secrets_add({ name: "api_key", from_env: "MY_API_KEY" });
```

### `memory_recall` — Read Memory

```javascript
const memory = await memory_recall();
// Returns MEMORY.md content as string
```

### `memory_search_history` — Search History

```javascript
const results = await memory_search_history({ query: "OpenRouter", limit: 5 });
// Returns matching HISTORY.md entries
```

### `echo` — Debug Tool

```javascript
await echo({ message: "debug output" });
// Returns message as-is; excluded from default agent tool surface
```

---

## 13. Link Context Builder

Pre-fetches documentation before LLM generation to improve quality.

### Trigger Conditions

- User message contains explicit URLs
- `link_context.enabled=true`
- NOT already matched as reusable clam (would skip generation anyway)

### Process

1. Extracts URLs from message (explicit + optionally inferred from intent)
2. For each URL (up to `max_links`): fetches content, truncates to `max_chars_per_link`
3. Content injected into selector + generator prompts as "Retrieved link context (JSON)"
4. Approval gate: `ApprovalPhase.GENERATION_CONTEXT` — denied/pending → fail-open (skip fetch, don't block generation)

### Configuration

```json
"link_context": {
  "enabled": true,
  "max_links": 5,
  "explicit_links_only": true,
  "heuristic_prefetch_enabled": true,
  "intent_url_inference_enabled": true
}
```

---

## 14. Heartbeat Service

Proactive scheduled agent wakeup for autonomous task execution.

### Configuration

```json
"heartbeat": {
  "enabled": true,
  "interval": 1800
}
```

### HEARTBEAT.md

Place task instructions in `~/.clambot/workspace/memory/HEARTBEAT.md`:

```markdown
## Tasks

- [ ] Check OpenRouter credits balance every morning
- [ ] Send daily weather summary
```

### Skip Logic

If HEARTBEAT.md only contains headings, comments, or empty checkbox lines → heartbeat skipped silently. Only triggers when actionable content is present.

---

## 15. Gateway Orchestrator

The central coordinator for gateway mode.

### Startup Sequence

```
runtime.startup()
→ orchestrator.start()
→ channel_manager.start()     (starts Telegram polling)
→ cron_service.start()        (starts scheduler loop)
→ heartbeat_service.start()   (starts heartbeat loop)
```

### Shutdown Sequence

Reverse order, each step wrapped in `suppress(Exception)`.

### Special Command Routing

| Command | Action |
|---------|--------|
| `/approve <decision> [option_id]` | Resolve pending tool approval |
| `/secret <name> <value>` | Store secret + auto-resume blocked request |
| `/new` | Memory consolidation + session reset |

### Approval State Management

The orchestrator stores pending inbound messages by `approval_id` in memory. On resolution:
1. Retrieves original inbound message
2. Resolves approval in ApprovalGate
3. Deletes Telegram approval keyboard message
4. Re-queues original inbound with `approval_resume=True`

### Phase Callbacks

Each processing phase emits a callback to the channel:

| Phase | Telegram Status |
|-------|----------------|
| `selecting` | "🧠 Analyzing input..." |
| `building` | "🛠️ Building clam..." |
| `running` | "🐌 Running clam..." |
| `analyzing_output` | "🧠 Analyzing output..." |

Status messages are ephemeral — deleted when the final response arrives.

---

## 16. Workspace Onboarding

```bash
clambot onboard
```

Creates workspace directory structure:
```
~/.clambot/
├── config.json           # Auto-generated with discovered providers/models
├── secrets/
│   └── store.json
├── cron/
│   └── jobs.json
└── workspace/
    ├── clams/            # Promoted clam packages
    ├── build/            # Staging area for new clams
    ├── sessions/         # Conversation history
    ├── logs/             # Event logs
    ├── docs/             # Custom LLM instruction docs
    └── memory/
        ├── MEMORY.md
        ├── HISTORY.md
        └── HEARTBEAT.md
```

Provider/model discovery: scans env vars, probes Ollama, fills config with what's available.
