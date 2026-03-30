# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/clamguy/clambot/releases/tag/v0.1.0
