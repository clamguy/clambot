# ClamBot — Module Reference

All modules are in the `clambot/clambot/` Python package.

---

## Agent Core (`clambot.agent`)

| Module | Class/Function | Description |
|--------|----------------|-------------|
| `agent.loop` | `AgentLoop` | Orchestrates one full agent turn: link context → memory → selector → generator → runtime → analysis |
| `agent.bootstrap` | `build_provider_backed_agent_loop_from_config()` | Factory that constructs a fully wired `AgentLoop` from config |
| `agent.turn_execution` | `process_turn_with_persistence_and_execution()` | Complete turn pipeline including session persistence and compaction |
| `agent.context` | `ContextBuilder` | Assembles system prompt from docs, memory, tools, clam catalog |
| `agent.clams` | `ClamRegistry` | Discovers, validates, and summarizes all clam packages on disk |

### Selector

| Module | Class | Description |
|--------|-------|-------------|
| `agent.selector` | `ProviderBackedClamSelector` | LLM-backed routing: decides `select_existing`, `generate_new`, or `chat` |
| `agent.request_normalization` | `normalize_request()` | NFKC + punctuation stripping for exact match pre-selection |

### Generator

| Module | Class | Description |
|--------|-------|-------------|
| `agent.provider_generation` | `ProviderBackedClamGenerator` | Calls LLM to generate clam script from user request |
| `agent.generation_adapter` | `normalize_generation_response()` | Provider-agnostic normalization of raw LLM generation output |
| `agent.generation_grounding` | `apply_grounding_rules()` | Post-processes generated script against grounding rules |
| `agent.workspace_clam_writer` | `WorkspaceClamPersistenceWriter` | Writes clam files to `build/`; promotes to `clams/` on success |

### Runtime

| Module | Class | Description |
|--------|-------|-------------|
| `agent.runtime` | `ClamRuntime` | Manages full execution lifecycle: pre-flight, sandbox, timeout, events |
| `agent.runtime_backend_amla_sandbox` | `AmlaSandboxRuntimeBackend` | Bridges `ClamRuntime` to the `amla-sandbox` library |
| `agent.runtime_policy` | `resolve_runtime_policy()` | Extracts network/filesystem policy from clam metadata |
| `agent.compatibility` | `CompatibilityChecker` | Validates language and platform requirements before execution |
| `agent.secret_preflight` | `resolve_pre_runtime_secret_requirements()` | Checks all declared secrets are available before execution |
| `agent.errors` | `ClamErrorPayload`, `ClamErrorStage` | Structured, machine-parseable error types |
| `agent.progress` | `ProgressState` | Enum: `DISCOVERING → GENERATING → VALIDATING → EXECUTING → WAITING_APPROVAL → COMPLETED / FAILED` |
| `agent.run_log` | `RunLogBuilder` | Collects structured execution events into a run log |

### Approvals

| Module | Class | Description |
|--------|-------|-------------|
| `agent.approval_gate` | `ApprovalGate` | Evaluates per-tool access requests: allow, deny, or await |
| `agent.approvals` | `CapabilityApprovalStore`, `ApprovalRecord`, `ApprovalOption` | Approval records, scope options, persistent store |
| `agent.approval_ux` | `resolve_approval_decision()` | Orchestrates interactive approval decision flow |
| `agent.approval_terminal_ui` | `TerminalApprovalUI` | `questionary`-based terminal prompt for approval decisions |
| `agent.capabilities` | `CapabilityEvaluator`, `CapabilityPolicy` | Capability policy DSL parsing and per-call enforcement |
| `agent.policy_violations` | `PolicyViolationCode` | Structured violation codes and payloads |

### Analysis

| Module | Class | Description |
|--------|-------|-------------|
| `agent.post_runtime_analysis` | `PostRuntimeAnalysisDecision` | Decision enum: `ACCEPT`, `SELF_FIX`, `REJECT` |
| `agent.provider_post_runtime_analysis` | `ProviderBackedPostRuntimeAnalyzer` | LLM-backed post-runtime analysis provider |
| `agent.post_runtime_analysis_adapter` | `normalize_analysis_response()` | Normalizes raw LLM analysis output |
| `agent.analysis_trace` | `AnalysisTraceBuilder` | Collects analysis trace for session metadata |
| `agent.final_response` | `select_final_response()` | Selects the final user-visible response from analysis output |

### Context and Memory

| Module | Class/Function | Description |
|--------|----------------|-------------|
| `agent.provider_link_context` | `ProviderLinkContextBuilder` | Pre-fetches URL content before generation |
| `agent.chat_mode` | `ChatModeFallbackResponder` | Direct LLM responder for `chat` decisions (no code execution) |
| `agent.diagnostics` | `DiagnosticsStore` | Interface for structured diagnostic collection |
| `agent.error_detail_context` | `build_error_detail_context()` | Enriches error payloads with contextual detail |

### Builtin Tools (Agent-Facing)

| Module | Description |
|--------|-------------|
| `agent.builtin_tools` | Built-in tool name constants, registry facade, dispatch helper |
| `agent.tools.base` | `AgentTool` base type |
| `agent.tools.registry` | Agent-side tool registry |
| `agent.tools.builtin.cron` | Cron tool definition (agent-facing) |
| `agent.tools.builtin.echo` | Echo tool definition |
| `agent.tools.builtin.fs` | Filesystem tool definition |
| `agent.tools.builtin.http_request` | HTTP request tool definition |
| `agent.tools.builtin.secrets_add` | Secrets add tool definition |
| `agent.tools.builtin.secrets_ask` | Secrets ask tool definition |
| `agent.tools.builtin.web_fetch` | Web fetch tool definition |

---

## Message Bus (`clambot.bus`)

| Module | Class | Description |
|--------|-------|-------------|
| `bus.events` | `InboundMessage`, `OutboundMessage` | Frozen dataclasses for inbound and outbound messages |
| `bus.queue` | `MessageBus` | Two `asyncio.Queue` instances: `inbound` and `outbound` |

### InboundMessage Fields

```python
@dataclass(frozen=True)
class InboundMessage:
    channel: str          # "telegram", "cron", "heartbeat", "cli"
    source: str           # "user_id|username" for Telegram
    chat_id: str          # conversation target id (Telegram chat or cron job id)
    content: str          # Raw message text
    correlation_id: str | None
    media: tuple[str, ...]
    metadata: dict        # approval_resume, one_time_approval_grants, secret_resume, etc.
```

### OutboundMessage Fields

```python
@dataclass(frozen=True)
class OutboundMessage:
    channel: str          # Target channel
    target: str           # e.g. "<chat_id>" for Telegram
    content: str          # Response text or special payload
    correlation_id: str | None
    media: tuple[str, ...]
    reply_to: str | None
    metadata: dict        # reply_to_message_id, approval_id, options, etc.
```

---

## Channels (`clambot.channels`)

| Module | Class | Description |
|--------|-------|-------------|
| `channels.base` | `BaseChannel` | ABC: `start()`, `stop()`, `send()`, `is_allowed_source()` |
| `channels.manager` | `ChannelManager` | Lifecycle management + outbound dispatch loop |
| `channels.telegram` | `TelegramChannel` | Full Telegram integration (see [telegram-ux.md](./telegram-ux.md)) |

---

## CLI (`clambot.cli`)

| Module | Function | Description |
|--------|----------|-------------|
| `cli.commands` | `app` (Typer) | All CLI commands defined here |

### CLI Commands

| Command | Description |
|---------|-------------|
| `clambot agent [-m MSG]` | Single-turn or interactive REPL |
| `clambot gateway` | Start gateway daemon |
| `clambot onboard` | Initialize workspace + config |
| `clambot status` | Show provider readiness + model alignment |
| `clambot cron list` | List scheduled jobs |
| `clambot cron add` | Add new scheduled job |
| `clambot cron remove` | Remove scheduled job |
| `clambot cron enable/disable` | Toggle job enabled state |
| `clambot cron run` | Manually trigger a job |
| `clambot provider login openai-codex` | OAuth login for Codex |
| `clambot channels connect telegram` | Interactive Telegram setup |

---

## Configuration (`clambot.config`)

| Module | Class/Function | Description |
|--------|----------------|-------------|
| `config.schema` | `ClamBotConfig` | Pydantic v2 model for full config schema |
| `config.loader` | `load_config()`, `resolve_config_path()` | Load config from JSON file + dotenv resolution |

---

## Cron (`clambot.cron`)

| Module | Class | Description |
|--------|-------|-------------|
| `cron.types` | `CronJob`, `CronSchedule`, `CronPayload`, `CronStore` | All cron data types |
| `cron.schedule` | `parse_schedule()`, `validate_cron_expression()` | Schedule parsing, cron syntax validation, IANA timezone support |
| `cron.service` | `InMemoryCronService`, `NotConfiguredCronService` | Async scheduler loop with `asyncio.Event`-based sleep |
| `cron.store` | `load_cron_store()`, `save_cron_store()` | JSON file persistence helpers |

---

## Gateway (`clambot.gateway`)

| Module | Class | Description |
|--------|-------|-------------|
| `gateway.orchestrator` | `GatewayOrchestrator` | Central coordinator: inbound message processing, approval/secret routing, phase callbacks |

---

## Heartbeat (`clambot.heartbeat`)

| Module | Class | Description |
|--------|-------|-------------|
| `heartbeat.service` | `InMemoryHeartbeatService`, `NotConfiguredHeartbeatService` | Periodic wakeup service; checks HEARTBEAT.md for actionable content |

---

## Memory (`clambot.memory`)

| Module | Function | Description |
|--------|----------|-------------|
| `memory.store` | `memory_recall()`, `memory_save()`, `memory_append_history()` | File I/O for MEMORY.md and HISTORY.md |
| `memory.facts` | `extract_durable_facts_for_turn()` | Extracts key facts from a session turn |
| `memory.consolidation` | `consolidate_session_memory()` | LLM-based memory consolidation post-compaction |

---

## Providers (`clambot.providers`)

| Module | Class | Description |
|--------|-------|-------------|
| `providers.base` | `LLMProvider` (Protocol), `LLMResponse` | Provider interface and response type |
| `providers.factory` | `create_provider()` | Instantiates correct provider from config |
| `providers.registry` | `PROVIDERS`, `find_by_name()`, `find_by_model()` | Registry of all available providers |
| `providers.litellm_provider` | `LiteLLMProvider` | Default multi-backend provider (wraps `litellm`) |
| `providers.openai_codex_provider` | `OpenAICodexProvider` | OAuth streaming Responses API adapter |
| `providers.custom_provider` | `CustomProvider` | Direct OpenAI-compatible endpoint |

---

## Session (`clambot.session`)

| Module | Class | Description |
|--------|-------|-------------|
| `session.manager` | `SessionManager` | JSONL-persisted sessions; in-memory cache; auto-migrate legacy formats |
| `session.types` | `SessionRecord`, `SessionTurn` | Data types for session and turn records |
| `session.key` | `encode_session_key()`, `decode_session_key()` | Base64url encoding/decoding for filenames |
| `session.history` | `turns_to_llm_history()` | Converts session turns to LLM-compatible message history |
| `session.compaction` | `maybe_auto_compact_session()` | Trigger and execute session compaction |
| `session.contract` | `CompactionContract` | Data contract for compaction LLM call |
| `session.errors` | `SessionStorageError`, `SessionValidationError` | Typed session errors |

---

## Tools — Runtime Implementations (`clambot.tools`)

| Module | Tool | Description |
|--------|------|-------------|
| `tools.__init__` | `BUILTIN_TOOLS` | Tuple of all registered built-in tools |
| `tools.base` | `BuiltinTool`, `ToolApprovalOption` | Base types for all built-in tools |
| `tools.registry` | `BuiltinToolRegistry` | Lookup by name, dispatch calls, render metadata to LLM |
| `tools.cron.operations` | `CronTool` | `add`, `remove`, `list` cron operations |
| `tools.cron.approval` | `CronApprovalOptions` | Approval scope options for cron tool |
| `tools.cron.contract` | `CronToolContract` | Expected behavior specification |
| `tools.echo.echo` | `EchoTool` | Debug echo tool |
| `tools.filesystem.core` | `FilesystemTool` | `list`, `read`, `write`, `edit` file operations |
| `tools.filesystem.operations` | `fs_list()`, `fs_read()`, `fs_write()`, `fs_edit()` | Individual filesystem op implementations |
| `tools.filesystem.approval` | `FilesystemApprovalOptions` | Approval scope options (exact, parent, workspace) |
| `tools.filesystem.contract` | `FilesystemToolContract` | Expected behavior specification |
| `tools.http.operations` | `HttpRequestTool` | HTTP request with bearer secret auth |
| `tools.http.approval` | `HttpApprovalOptions` | Approval scope options for HTTP tool |
| `tools.http.contract` | `HttpRequestToolContract` | Expected behavior specification |
| `tools.memory.recall` | `MemoryRecallTool` | Returns MEMORY.md content |
| `tools.memory.search` | `MemorySearchHistoryTool` | Substring search over HISTORY.md |
| `tools.secrets.operations` | `SecretsAddTool` | Store or update a named secret |
| `tools.secrets.store` | `SecretStore` | Read/write secrets store with atomic writes |
| `tools.secrets.env` | `resolve_secret_value()` | Resolution logic (value→env→dotenv→prompt→error) |
| `tools.secrets.approval` | `SecretsApprovalOptions` | Approval scope options |
| `tools.secrets.contract` | `SecretsAddToolContract` | Expected behavior specification |
| `tools.web.fetch` | `WebFetchTool` | Fetches URL content; used both at generation and runtime |

---

## Workspace (`clambot.workspace`)

| Module | Function | Description |
|--------|----------|-------------|
| `workspace.bootstrap` | `bootstrap_workspace()` | Creates workspace directory layout on first run |
| `workspace.onboard` | `onboard_workspace()` | Generates config, doc templates, memory files |
| `workspace.retention` | `prune_session_logs()` | Limits total session JSONL file count |

---

## Async Runner (`clambot.async_runner`)

| Module | Function | Description |
|--------|----------|-------------|
| `async_runner` | `run_sync(coro)` | Submits coroutine to persistent background event loop; returns result synchronously |
| `async_runner` | `get_event_loop()` | Returns the persistent background loop (creates on first call) |

This module exists specifically to solve the sync/async boundary for CLI command handlers. The gateway and agent pipeline are async and run on a single event loop; `clambot.async_runner` provides a persistent loop bridge for synchronous CLI entry points.
