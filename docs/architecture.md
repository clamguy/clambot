# ClamBot — Architecture

## System Overview

ClamBot is a multi-layered Python application with three distinct runtime modes sharing a common core:

1. **CLI mode** — synchronous single-turn or REPL interactions
2. **Gateway mode** — long-running async daemon: Telegram + cron + heartbeat
3. **Cron/heartbeat** — scheduled agent turn execution

All modes converge on the same `process_turn_with_persistence_and_execution()` pipeline.

---

## High-Level Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│  Inbound Sources                                                      │
│  ┌─────────────┐  ┌───────────────┐  ┌───────────────┐  ┌─────────┐ │
│  │  Telegram   │  │  Cron Service │  │   Heartbeat   │  │   CLI   │ │
│  │ long-polling│  │ (async loop)  │  │   Service     │  │  REPL   │ │
│  └──────┬──────┘  └───────┬───────┘  └───────┬───────┘  └────┬────┘ │
└─────────┼─────────────────┼──────────────────┼───────────────┼──────┘
          │InboundMessage   │direct call        │direct call    │
          ▼                 ▼                   ▼               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  MessageBus (Telegram only uses bus.inbound)                         │
│  bus.inbound  (asyncio.Queue)                                        │
│  bus.outbound (asyncio.Queue)                                        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  GatewayOrchestrator                                                 │
│                                                                      │
│  async _process_inbound()   [runs on event loop, no thread offload]  │
│                                                                      │
│  Special command routing:                                            │
│  • /approve <decision>  → resolve pending approval                   │
│  • /secret <name> <val> → store secret + auto-resume                 │
│  • /new                 → memory consolidate + session reset         │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  process_turn_with_persistence_and_execution()  [async]              │
│                                                                      │
│  1. await SessionManager.load_history(session_key)                   │
│  2. await maybe_auto_compact_session() → LLM summary if near limit   │
│  3. await AgentLoop.process_turn()                                   │
│     a. await ProviderLinkContextBuilder.build_context()              │
│     b. MemoryRecall (MEMORY.md content)                              │
│     c. ContextBuilder.build_system_prompt()                          │
│     d. await ClamSelector.select()     ─► LLM call                  │
│        • pre-selection: exact normalized match → skip LLM            │
│        • decision: select_existing / generate_new / chat             │
│     e. await ClamGenerator.generate() ─► LLM call (if generate_new) │
│     f. WorkspaceClamWriter.write() → persist to build/               │
│     g. await ClamRuntime.execute()    ─► WASM sandbox (thread)       │
│     h. await PostRuntimeAnalyzer.analyze() ─► LLM call              │
│        • ACCEPT → promote clam to clams/                             │
│        • SELF_FIX → re-enter loop with fix instructions              │
│        • REJECT → return error                                       │
│  4. SessionManager.append_turn()                                     │
│  5. asyncio.create_task(_background_extract_durable_facts())         │
│     [fire-and-forget: LLM memory update, never blocks response]      │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ OutboundMessage
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  bus.outbound → ChannelManager → TelegramChannel.send()              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## WASM Runtime Detail

```
await ClamRuntime.execute()     [async def, runs on event loop]
│
├─ Pre-flight checks (sync, fast)
│   ├─ Secret requirements resolution
│   ├─ Compatibility check (JS-only)
│   └─ Capability policy validation
│
├─ asyncio.Event done_event created
│
├─ Thread(target=run_backend).start()   [daemon thread]
│   │
│   ├─ Sandbox.execute(script)          [QuickJS inside WASM, blocking]
│   │   │
│   │   │  on tool call yield:
│   │   ├─ emit("backend.tool_call")
│   │   │   [if on_event set: loop.call_soon_threadsafe(on_event, event)]
│   │   ├─ ApprovalGate.evaluate_request()
│   │   │   ├─ check always_grants
│   │   │   ├─ check capability policy
│   │   │   └─ interactive prompt (terminal or Telegram inline buttons)
│   │   ├─ BuiltinToolRegistry.dispatch()
│   │   │   └─ fs / cron / web_fetch / http_request / secrets_add /
│   │   │      memory_recall / memory_search_history / echo
│   │   └─ Sandbox.resume(result)
│   │
│   └─ finally: loop.call_soon_threadsafe(done_event.set)
│       [thread-safe signal back to event loop]
│
└─ await asyncio.wait_for(done_event.wait(), timeout=timeout_seconds)
    │
    ├─ normal completion → process output/error
    │
    └─ asyncio.TimeoutError:
        ├─ token.cancel("timeout")
        ├─ await asyncio.wait_for(done_event.wait(), cancellation_grace_seconds)
        └─ if still not done → return runtime_timeout_unresponsive error
```

---

## Concurrency Model

### Fully Async Pipeline (current implementation)

All components run as cooperative asyncio tasks on a single event loop. The agent pipeline itself is `async def` end-to-end, yielding the event loop at every `await` (LLM calls, WASM wait, memory I/O).

```
asyncio.run(gateway_main(config))
│
├─ TelegramChannel._on_message / _on_callback_query  [event loop, async]
│   └─ typing indicator loop   [asyncio.Task, every 4s]
│
├─ InMemoryCronService._run()       [asyncio.Task]
│   └─ executor(job) → await orchestrator.process_inbound_async(inbound)
│
├─ InMemoryHeartbeatService._run()  [asyncio.Task]
│
├─ ChannelManager._dispatch_outbound()  [asyncio.Task, reads bus.outbound]
│
└─ GatewayOrchestrator._run()       [asyncio.Task, reads bus.inbound]
    └─ await process_inbound_async(inbound)
        └─ await _process_inbound(inbound)
            ├─ await litellm.acompletion()   [selector, generator, analyzer]
            ├─ await asyncio.wait_for(       [WASM — in daemon thread]
            │      done_event.wait(), ...)
            ├─ phase callbacks: put_nowait() [sync, safe on event loop]
            └─ asyncio.create_task(          [background, non-blocking]
                   _background_extract_durable_facts())
```

### Why WASM still uses a Thread

`amla-sandbox`'s `Sandbox.execute()` is a blocking C extension (Wasmtime via CFFI). It has no async API. It runs in a daemon thread; the thread signals completion back to the event loop via `loop.call_soon_threadsafe(done_event.set)` — making the synchronisation point fully async from the event loop's perspective.

### Cron executor vs. `_run()` loop

Cron jobs call `orchestrator.process_inbound_async(inbound)` **directly** (bypassing `bus.inbound`). This means cron jobs and user-message processing run **concurrently** as separate asyncio coroutines. They share no locks; the GIL makes per-dict-operation access safe and the `try/except` guards in each path handle any logical races.

### `async_runner.py` — CLI Bridge Only

A persistent daemon thread with a dedicated event loop remains for the **CLI** only (`clambot agent` interactive/single-turn commands). The gateway no longer uses it; the CLI uses `run_sync(coro)` to call async functions from Typer's synchronous command handlers.

---

## Message Flow: Normal User Message

```
Telegram user sends "what is the price of BTC?"
│
├─ TelegramChannel._on_message() receives update
├─ correlation_id = "corr_<uuid>" generated
├─ _start_typing_indicator(corr_<uuid>, chat_id)  ← typing task started
├─ InboundMessage(channel="telegram", session_key="telegram:<chat_id>",
│                 correlation_id="corr_<uuid>") → bus.inbound.put()
│
├─ GatewayOrchestrator._run() dequeues message
├─ await process_inbound_async(msg)   [on event loop — no thread offload]
│
│  Meanwhile (other tasks on same event loop):
│  ├─ typing task sends "typing" action every 4s
│  └─ _dispatch_outbound task sends phase status messages as they arrive
│
├─ process_turn:
│   ├─ Phase: selecting   → put_nowait "🧠 Analyzing input..."
│   ├─ await ClamSelector → "generate_new"
│   ├─ Phase: building    → put_nowait "🛠️ Building clam..."
│   ├─ await ClamGenerator → JavaScript clam
│   ├─ Phase: running     → put_nowait "🐌 Running clam..."
│   ├─ await ClamRuntime (WASM thread + asyncio.Event) → calls http_request
│   ├─ Phase: analyzing_output → put_nowait "🧠 Analyzing output..."
│   └─ await PostRuntimeAnalyzer → ACCEPT
│
├─ SessionManager.append_turn()
├─ asyncio.create_task(_background_extract_durable_facts())  ← fire-and-forget
│   [memory consolidation LLM call runs independently, never blocks response]
│
├─ OutboundMessage("BTC price is $67,432", correlation_id="corr_<uuid>")
│   → bus.outbound.put()
│
└─ TelegramChannel.send():
    ├─ _stop_typing_indicator("corr_<uuid>")  ← cancels typing task
    ├─ _delete_status_message("corr_<uuid>")  ← removes phase messages
    └─ send final text (chunked at 4096 chars, MarkdownV2 formatted)
```

---

## Message Flow: Approval Required

```
Agent clam calls web_fetch({url: "https://example.com"})
│
├─ ApprovalGate: no always_grant matches → AWAITING
│   [raised as ClamRuntimeApprovalGateError in WASM thread]
├─ run_backend() catches error → done_event.set() via call_soon_threadsafe
├─ ClamRuntime.execute() resumes, emits "runtime.awaiting_approval"
│
├─ GatewayOrchestrator._process_inbound():
│   ├─ stores pending InboundMessage by approval_id
│   └─ builds OutboundMessage(
│         type="approval_pending",
│         correlation_id=inbound.correlation_id)   ← same corr_id as original msg
│
├─ TelegramChannel.send(approval_pending_outbound):
│   ├─ _stop_typing_indicator("corr_<uuid>")  ← typing stopped correctly
│   ├─ _delete_status_message("corr_<uuid>")  ← phase messages cleared
│   └─ send inline keyboard:
│       [Allow Once] [Allow Always (exact)] [Allow Always (host)] [Reject]
│
├─ User taps "Allow Once"
├─ TelegramChannel._on_callback_query():
│   ├─ correlation_id = "corr_<new_uuid>" generated
│   ├─ _start_typing_indicator("corr_<new_uuid>", chat_id)  ← feedback during re-run
│   └─ InboundMessage(content="/approve allow_once",
│                     correlation_id="corr_<new_uuid>",
│                     metadata={approval_id: "...", grant_scope: "..."})
│       → bus.inbound.put()
│   (+ query.answer() to clear button loading state)
│
├─ GatewayOrchestrator detects /approve command:
│   ├─ ApprovalGate.resolve(approval_id, ALLOW_ONCE, scope)
│   ├─ re-queues original InboundMessage with approval_resume=True
│   └─ returns acknowledgment OutboundMessage(correlation_id="corr_<new_uuid>")
│
├─ TelegramChannel.send(acknowledgment):
│   └─ _stop_typing_indicator("corr_<new_uuid>")  ← stops callback typing task
│
├─ _run() picks up re-queued original InboundMessage
├─ Agent re-runs clam from start; one_time_approval_grants registered → tool allowed
├─ Final response sent (typing indicator was already stopped)
└─ asyncio.create_task(_background_extract_durable_facts())
```

---

## Message Flow: Cron Job Execution

```
InMemoryCronService: next_run_at_ms reached for job "openrouter_credits_balance_hourly"
│
├─ executor(job) called  [direct call, NOT via bus.inbound]
├─ await orchestrator.process_inbound_async(
│     InboundMessage(
│       channel="cron",
│       source="cron:executor",
│       chat_id="openrouter_credits_balance_hourly",
│       content="Send OpenRouter credits balance."
│     )
│   )
│
├─ Agent pipeline runs (same async path as Telegram)
├─ Orchestrator derives session key as "cron:<job_id>" from channel+chat_id
├─ Result: OutboundMessage with credits balance text
│
├─ payload.deliver=true → bus.outbound.put(outbound)
├─ ChannelManager → TelegramChannel.send(target="telegram:<chat_id>")
│
├─ asyncio.create_task(_background_extract_durable_facts())  [non-blocking]
│
├─ JSONL event logged: workspace/logs/gateway_cron_events.jsonl
└─ CronService: update job state, calculate next_run_at_ms
```

---

## Durable Facts Extraction (Background)

After every successful turn (`status ∈ {"ok", "chat_fallback", "skipped"}`), the gateway extracts durable facts from the conversation and updates `MEMORY.md` and `HISTORY.md`. This involves an LLM API call and must **never block response delivery**.

```
_persist_assistant_turn()
│
├─ session_manager.append(turn)               [sync, fast]
├─ asyncio.create_task(                       [non-blocking, fire-and-forget]
│      _background_extract_durable_facts())
└─ return outbound                            [response goes out immediately]

_background_extract_durable_facts()           [runs concurrently]
│
├─ except BaseException: pass                 [never propagates to _run() loop]
└─ _maybe_extract_durable_facts()
    ├─ check status ∈ {"ok", "chat_fallback", "skipped"}
    ├─ find last user message in session history
    ├─ await extract_durable_facts_for_turn()
    │   └─ await memory_consolidate()  ─► LLM call
    └─ append to HISTORY.md, update MEMORY.md if changed
```

**Key invariant:** The LLM call for durable facts never appears in the critical path of `_run()`. Even if it hangs or fails, the bot continues serving new messages immediately.

---

## Clam Lifecycle

```
User request
│
├─ [Pre-selection check]
│   └─ Exact NFKC-normalized match against catalog metadata.source_request?
│       └─ YES → skip LLM, use existing clam_id
│
├─ [await ClamSelector.select()] → decision
│
├─ select_existing:  load clam from clams/<name>/
│
├─ generate_new:
│   ├─ await ClamGenerator.generate() → {script, declared_tools, inputs, metadata}
│   ├─ WorkspaceClamWriter.write() → build/<name>/run.js + CLAM.md
│   └─ [after successful analysis] promote build/<name>/ → clams/<name>/
│
└─ chat:
    └─ await ChatResponder.respond() → direct text response (no clam)
```

---

## Session Key Mapping

| Context | Session Key | File |
|---------|-------------|------|
| Telegram chat | `telegram:<chat_id>` | `sessions/<base64url>.jsonl` |
| CLI single-turn | `cli:single-turn` | `sessions/<base64url>.jsonl` |
| CLI interactive | `cli:interactive` | `sessions/<base64url>.jsonl` |
| Cron job | `cron:<job_id>` | `sessions/<base64url>.jsonl` |
| Heartbeat | `heartbeat:loop` | `sessions/<base64url>.jsonl` |

Session keys are base64url-encoded (no padding) for filesystem-safe filenames.

---

## Data Stores

| Store | Location | Format | Purpose |
|-------|----------|--------|---------|
| Sessions | `~/.clambot/workspace/sessions/*.jsonl` | JSONL append-only | Conversation history per context |
| Clams | `~/.clambot/workspace/clams/<name>/` | CLAM.md + run.js | Generated + reusable scripts |
| Clam builds | `~/.clambot/workspace/build/<name>/` | CLAM.md + run.js | Staging before promotion |
| Memory | `~/.clambot/workspace/memory/MEMORY.md` | Markdown | Durable facts injected in system prompt |
| History | `~/.clambot/workspace/memory/HISTORY.md` | Markdown | Long-term interaction summaries |
| Heartbeat | `~/.clambot/workspace/memory/HEARTBEAT.md` | Markdown | Scheduled task instructions |
| Workspace docs | `~/.clambot/workspace/docs/*.md` | Markdown | Custom LLM instruction docs |
| Secrets | `~/.clambot/secrets/store.json` | JSON | Named secrets (0600 permissions) |
| Cron jobs | `~/.clambot/cron/jobs.json` | JSON | Scheduled job definitions |
| Config | `~/.clambot/config.json` | JSON | Application configuration |
| Cron event log | `~/.clambot/workspace/logs/gateway_cron_events.jsonl` | JSONL | Cron execution audit trail |

---

## Component Dependency Graph

```
GatewayOrchestrator
├── MessageBus (bus.inbound + bus.outbound — asyncio.Queue)
├── ChannelManager
│   └── TelegramChannel (python-telegram-bot)
│       ├── typing indicator tasks   [per correlation_id]
│       └── status message tracking [per correlation_id]
├── InMemoryCronService
│   └── CronStore (JSON persistence)
├── InMemoryHeartbeatService
└── AgentLoop (via process_turn_with_persistence_and_execution)
    ├── SessionManager (JSONL persistence)
    ├── ContextBuilder
    │   ├── ClamRegistry
    │   └── MemoryStore
    ├── ProviderLinkContextBuilder
    ├── ProviderBackedClamSelector ──► LLMProvider (await acompletion)
    ├── ProviderBackedClamGenerator ──► LLMProvider (await acompletion)
    ├── WorkspaceClamWriter
    ├── ClamRuntime
    │   ├── AmlaSandboxRuntimeBackend ──► amla-sandbox (WASM, daemon thread)
    │   │   └── asyncio.Event done_event  [thread → event loop bridge]
    │   ├── ApprovalGate
    │   │   └── CapabilityApprovalStore
    │   └── BuiltinToolRegistry
    │       ├── filesystem tool
    │       ├── cron tool ──► InMemoryCronService
    │       ├── web_fetch tool
    │       ├── http_request tool
    │       ├── secrets_add tool ──► SecretStore
    │       ├── memory_recall tool ──► MemoryStore
    │       ├── memory_search_history tool ──► MemoryStore
    │       └── echo tool
    └── ProviderBackedPostRuntimeAnalyzer ──► LLMProvider (await acompletion)
```

---

## Key Design Invariants

| Invariant | Mechanism |
|-----------|-----------|
| Response always delivered before memory update | `create_task(_background_extract_durable_facts)` — outbound returned first |
| Typing indicator always stops when response arrives | `approval_pending` outbound uses `inbound.correlation_id` (not runtime's) |
| User sees typing feedback during approval re-run | `_on_callback_query` starts typing indicator before `handle_inbound` |
| WASM thread calls to event loop are thread-safe | `loop.call_soon_threadsafe(done_event.set)` in `run_backend()` finally block |
| `on_event` callbacks from WASM thread are thread-safe | `loop.call_soon_threadsafe(on_event, event)` instead of direct call |
| Cron job changes reflected immediately | `asyncio.Event._change_event.set()` wakes the scheduler loop on add/remove |
| WASM timeout never hangs `_run()` loop | `asyncio.wait_for(done_event.wait(), timeout)` + grace period before abort |
