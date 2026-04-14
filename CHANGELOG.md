# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-04-13

### Added

- **Local Whisper ASR Support** — transcribe tool now supports local `whisper-asr-webservice` style endpoints via `whisperApiStyle: "whisper_asr"` (alongside OpenAI-compatible mode)
- **Transcribe Timeout Controls** — `whisperRequestTimeoutSeconds` config added for per-chunk transcription HTTP timeout tuning
- **Prompt-Pluggable Tool Guidance** — built-in tools can now expose `usage_instructions` metadata that is injected into the system prompt when those tools are enabled
- **Default Runtime Timeout Config** — `agents.defaults.runtimeTimeoutSeconds` now controls clam execution timeout policy globally

### Changed

- **Planner Routing for Media** — YouTube/video/audio transcript tasks are explicitly rewritten to use `transcribe` in execute steps
- **Direct Tool Execution Scope** — agent-loop direct execution is now limited to `cron` (no direct `web_fetch` execution path)
- **Self-Fix Regeneration Prompt** — retries now explicitly require valid JavaScript + JSON shape and include rejected output context for correction

### Fixed

- **Large Transcript Bridge Failures** — sandbox runtime backend now emits large returned values in safe chunks, preventing `JSONDecodeError: Unterminated string` failures on long transcripts
- **Subtask Context Leakage** — planned execute subtasks no longer inherit top-level pre-fetched link context that could cause prose/refusal outputs instead of runnable code
- **Transcription Resilience** — transcribe client now retries on timeout/transport errors and transient HTTP statuses (408/429/5xx), improving long local ASR runs
- **Execute→Transform Output Composition** — planned flows that transform fetched content now return only the transformed result (not raw transcript + summary), preventing oversized first replies in Telegram

## [0.1.1] - 2026-03-31

### Added

- **Interactive Provider Connect** — `uv run clambot provider connect <name>` for all API-key providers (openrouter, anthropic, openai, deepseek, gemini, groq) with masked key input and model selection
- **Agent Loop Direct Tool Execution** — cron and web_fetch operations execute directly in the agent loop without WASM sandbox, parsed from generated scripts
- **Persistent Command History** — agent REPL supports arrow-key navigation and cross-session history via readline
- **Think-Disabled Model Cache** — thinking models (Qwen, DeepSeek-R1) that return empty content are detected, retried with `think=False`, and cached in config for future calls

### Changed

- **Fresh CLI Sessions** — each `clambot agent` run starts a new session; `--resume` continues the last one
- **Chat Mode Simplified** — removed tool schemas from chat mode to prevent thinking models from making unwanted tool calls
- **Selector Routing** — tool operations (cron, web_fetch, fs) now route to `generate_new` instead of `chat`; cron intercepted by agent loop direct execution
- **Model IDs Updated** — all provider model lists use current stable names (no dated suffixes)
- **Default Model Removed** — `AgentDefaults.model` is now empty; must be set via `provider connect` or `onboard`
- **Selector Model Default Removed** — falls back to primary model when not set; onboard auto-selects per-provider

### Fixed

- **Thinking Model Compatibility** — auto-retry with `think=False` when LLM returns empty content with high token usage; `reasoning_content` fallback for providers that populate it
- **Command Too Long** — removed manual stdin piping, let amla-sandbox auto-pipe large scripts via `_should_pipe_js_via_stdin()`
- **Cron Tool in Sandbox** — `_AGENT_ONLY_TOOLS` excludes cron from WASM sandbox; agent loop executes cron directly
- **Quiet REPL Logging** — `clambot agent` suppresses INFO/DEBUG by default; `--logs` for INFO, `-v` for DEBUG

## [0.1.0] - 2026-03-29

### Added

- **WASM Sandbox Execution** — all LLM-generated code runs inside a QuickJS/Wasmtime sandbox with memory isolation and capability-gated tool access
- **Clam System** — named, versioned, reusable JavaScript execution units with metadata, disk persistence, and exact-match reuse for zero-latency repeat requests
- **Agent Loop** — full pipeline: request normalization, two-stage clam selection (pre-selection + LLM routing), code generation, WASM execution, post-runtime analysis, and self-fix loop (up to 3 retries)
- **Provider Layer** — multi-provider LLM support via LiteLLM (OpenRouter, Anthropic, OpenAI, Gemini, Ollama, DeepSeek) plus OpenAI Codex OAuth and custom OpenAI-compatible endpoints
- **Session Management** — append-only JSONL conversation persistence with in-memory cache, legacy format migration, and automatic compaction with LLM-generated summaries
- **Built-in Tools** — 8 capability-gated tools callable from WASM: `fs` (filesystem), `http_request` (authenticated HTTP), `web_fetch` (URL content), `cron` (scheduling), `secrets_add` (secret storage), `memory_recall`, `memory_search_history`, and `echo`
- **Transcribe Tool** — audio transcription via yt-dlp download + Groq Whisper API with automatic ffmpeg chunking for large files
- **Interactive Approval Gate** — SHA-256 fingerprinted tool approvals with always-grants, one-time grants, turn-scoped grants, and per-tool scope options (exact, host, path, directory)
- **Telegram Channel** — full integration with long-polling, typing indicators, phase status messages, MarkdownV2 rendering, inline approval keyboards, message chunking, file upload support, and SOCKS5 proxy
- **Cron System** — persistent timezone-aware job scheduling with `every`, `cron` (5-field), and `at` (one-time) schedule types, immediate sync on changes, and Telegram delivery
- **Heartbeat Service** — proactive scheduled agent wakeup with HEARTBEAT.md task instructions and skip logic for empty task lists
- **Long-term Memory** — MEMORY.md (durable facts, auto-injected into prompts) + HISTORY.md (searchable interaction summaries) with fire-and-forget LLM-based extraction
- **Gateway Orchestrator** — central coordinator with message bus routing, special command handling (`/approve`, `/secret`, `/new`), phase callbacks, and ordered startup/shutdown
- **CLI** — Typer-based commands: `agent` (single-turn/REPL), `gateway` (daemon), `onboard` (workspace setup), `status` (provider readiness), `cron` (job management), `channels connect telegram`
- **Workspace Onboarding** — automatic provider/model discovery from environment, config generation, and workspace directory bootstrapping
- **Link Context Builder** — pre-fetches URL content from user messages to improve LLM generation quality
- **Security Hardening** — SSRF protection (private IP blocking), configurable SSL fallback, default localhost binding, secret redaction in all logs/traces
- **Error Handling** — tracked async tasks with exception logging, structured error payloads with stable error codes, silent exception block instrumentation
- **Code Quality** — Ruff linting/formatting, mypy type checking, pre-commit hooks, protocol-based type safety, dependency version bounds
- **Host-Managed Secrets** — atomic-write secret store with 0600 permissions, multi-source resolution (explicit, env, dotenv, getpass, gateway prompt), and bearer token injection

[0.1.2]: https://github.com/clamguy/clambot/releases/tag/v0.1.2
[0.1.1]: https://github.com/clamguy/clambot/releases/tag/v0.1.1
[0.1.0]: https://github.com/clamguy/clambot/releases/tag/v0.1.0
[Unreleased]: https://github.com/clamguy/clambot/compare/v0.1.2...HEAD
