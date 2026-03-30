# ClamBot — Telegram Integration & UX

## Overview

ClamBot's Telegram integration (`clambot.channels.telegram.TelegramChannel`) provides:

- Long-polling updates via `python-telegram-bot>=22.5`
- Real-time typing indicators during agent processing
- Phase-based ephemeral status messages
- MarkdownV2-rendered responses
- Inline keyboard buttons for tool approval
- Automatic session resume after approvals and secret provision

---

## Setup

### 1. Create Bot via @BotFather

```
/newbot → set name → get token (e.g. 7123456789:AAFxx...)
```

### 2. Connect Bot to ClamBot

```bash
clambot channels connect telegram
```

This launches a temporary bot that sends a "Connect" button to any user who messages it. When pressed, the user's Telegram user ID is auto-populated in `config.json` under `channels.telegram.allow_from`.

### 3. Start Gateway

```bash
clambot gateway
```

Gateway starts Telegram long-polling.

---

## Configuration

```json
"channels": {
  "telegram": {
    "enabled": true,
    "token": "7123456789:AAFxx...",
    "allow_from": ["123456789|johndoe"],
    "proxy": null,
    "reply_to_message": false
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | bool | Must be `true` to start channel |
| `token` | string | Bot token from @BotFather |
| `allow_from` | list | Allowed sources. Empty = allow all |
| `proxy` | string\|null | SOCKS5 proxy for restricted networks |
| `reply_to_message` | bool | Use reply threading for responses |

### `allow_from` Format

Each entry is matched against the source string `"<user_id>|<username>"`:

```json
"allow_from": [
  "123456789",                  // user ID only
  "123456789|johndoe",          // user ID with username
  "johndoe"                     // username only
]
```

A source is allowed if any segment of the pipe-delimited source matches any segment of the pipe-delimited allow_from entry.

---

## Message Handling

### Inbound

Accepted update types:
- Text messages (`message`)
- Callback queries from inline keyboards (`callback_query`)

`drop_pending_updates=True` on polling start — ignores messages received while bot was offline.

### Source String

For each message, source is constructed as:
- With username: `"<user_id>|<username>"`
- Without username: `"<user_id>"`

### Session Key

```
telegram:<chat_id>
```

All messages from the same chat share a session. The session key is base64url-encoded for filename storage.

---

## UX Patterns

### 1. Typing Indicator

A continuous typing indicator loop runs for the duration of agent processing:

```python
# Sends "typing" action every 4 seconds until stopped
async def _typing_loop():
    while not stop_event.is_set():
        await bot.send_chat_action(chat_id, "typing")
        await asyncio.sleep(4)
```

The typing indicator:
- Starts when the inbound message is received
- Stops before the final response is sent
- Ensures the user always sees activity feedback

### 2. Ephemeral Status Messages

During agent processing, phase-based status messages are sent:

| Agent Phase | Status Message |
|------------|----------------|
| `selecting` | "🧠 Analyzing input..." |
| `building` | "🛠️ Building clam..." |
| `running` | "🐌 Running clam..." |
| `analyzing_output` | "🧠 Analyzing output..." |

Status messages are:
- Sent as new messages when a phase begins
- **Edited in-place** when the phase changes (not new messages)
- **Deleted** when the final response arrives
- Never stacked (only one status message at a time)

This prevents chat clutter while keeping the user informed.

### 3. Final Response

Final responses:
- **Delete** the ephemeral status message (if any)
- **Stop** the typing indicator
- **Send** the final text, optionally with `reply_to_message_id` if `reply_to_message=true`

### 4. Long Message Chunking

Telegram's 4096-character message limit is handled by:
1. Splitting on newline boundaries first
2. Then on space boundaries
3. First chunk uses `reply_to_message_id` if configured
4. Subsequent chunks are plain sends

### 5. Markdown Rendering

Responses are rendered as Telegram MarkdownV2:
- Standard Markdown → MarkdownV2 conversion applied
- Falls back to plain text on parse error (never fails silently)
- Handles: **bold**, _italic_, `code`, ```code blocks```, [links](url), > blockquotes

---

## Tool Approval UX

When a generated clam calls a tool that needs approval, ClamBot sends an inline keyboard message.

### Approval Message

```
Tool approval required:

web_fetch
URL: https://api.coinbase.com/v2/prices/BTC-USD/spot

Choose how to allow this:

[Allow Once]
[Always: exact URL]
[Always: host coinbase.com]
[Always: path /v2/prices/*]
[Reject]
```

(`disable_web_page_preview=True` to avoid link previews cluttering the approval message.)

### Button Types

| Button Label | Action |
|-------------|--------|
| `Allow Once` | Allow this single invocation |
| `Always: <scope description>` | Persist approval to config + memory |
| `Reject` | Deny this tool call |

The "Allow Always" options are generated dynamically per tool — each tool provides its own scope options with different granularities.

### Approval Callback Flow

```
User taps button
→ Telegram callback_query received
→ TelegramChannel._approval_command_from_callback_data()
  parses callback_data:
    "approval:allow_once"
    "approval:allow_always"
    "approval:allow_always:option_id"
    "approval:reject"
→ InboundMessage(content="/approve allow_once", metadata={approval_id, grant_scope})
→ GatewayOrchestrator routes to approval handler
→ ApprovalGate.resolve(approval_id, decision, scope)
→ Approval keyboard message DELETED from chat
→ Original inbound re-queued with approval_resume=True
→ Agent resumes execution
→ Final response sent
```

### Approval Message Cleanup

When an approval is resolved (either Allow or Reject), the approval keyboard message is deleted from the Telegram chat. This keeps the chat clean — resolved approvals don't leave dead buttons.

---

## Secret Provision UX

When a clam needs a secret that hasn't been stored yet:

### Bot Message to User

```
Please provide secret: `/secret openrouter_api_key <value>`
```

### User Response

```
/secret openrouter_api_key sk-or-v1-abc123...
```

### Flow

```
User sends: /secret openrouter_api_key sk-or-v1-...
→ GatewayOrchestrator detects /secret command
→ SecretStore.save("openrouter_api_key", "sk-or-v1-...")
→ Re-queues original inbound with secret_resume=True
→ Agent resumes from beginning (with secret now available)
→ Final response sent
```

### Security Note

The `/secret` message containing the token remains in Telegram chat history. Users should be aware of this and use private/direct chats with the bot.

---

## Session Reset

```
/new
```

Triggers:
1. Memory consolidation (extracts facts from current session → HISTORY.md)
2. MEMORY.md updated if new facts found
3. Session history cleared (in-memory only; JSONL file untouched)
4. Response: "New session started."

---

## UX Reliability Patterns

### Drop Pending Updates

`drop_pending_updates=True` in `start_polling()` — ensures messages sent while the bot was offline are ignored. Without this, a bot restart would process a backlog of stale messages.

### Typing Loop Never Blocks

The typing indicator runs as an asyncio task on the event loop while agent processing is also async on the same event loop. Concurrency comes from cooperative scheduling (`await` points), while the WASM backend itself runs in a daemon thread and signals back via thread-safe callbacks.

### Status Message Lifecycle

Status messages are tracked by message ID:
- On phase change: `edit_message_text()` updates existing message
- On final response: `delete_message()` removes status message before `send_message()`
- If edit/delete fails (e.g., message already gone): error is suppressed, continue normally

This means if a status message was manually deleted by the user, the bot handles it gracefully.

### Approval State Persistence (In-Memory)

Pending approvals are stored in `GatewayOrchestrator._pending_approvals: Dict[str, InboundMessage]`. If the gateway restarts mid-approval, the pending state is lost and the user needs to resend their message. This is acceptable for a personal assistant use case.

### Callback Query Acknowledgment

All callback queries are `answer()`-ed immediately to remove the loading spinner from Telegram's UI, even if processing takes longer.

### Markdown Fallback

```python
try:
    await bot.send_message(chat_id, text, parse_mode="MarkdownV2")
except telegram.error.BadRequest:
    await bot.send_message(chat_id, text)  # plain text fallback
```

Never fails silently — always delivers content even if formatting fails.

---

## Channel-Specific Behaviors

### Allowed Updates

```python
start_polling(
    allowed_updates=("message", "callback_query"),
    drop_pending_updates=True
)
```

Only text messages and button callbacks are processed. Other update types (photos, stickers, voice, etc.) are ignored.

### Voice/Media Messages

Not handled. Only text content from `message.text` is processed.

### Group Chat Support

Technically works if the bot is added to a group and the user's ID is in `allow_from`. However, session key is still based on `chat_id` — all group members share the same session, which may cause confusion.

### Bot Commands

| Command | Source | Handler |
|---------|--------|---------|
| `/approve <decision> [option]` | Internal (via callback) | Approval resolution |
| `/secret <name> <value>` | User | Secret storage + resume |
| `/new` | User | Session reset |
| Any other text | User | Normal agent turn |

There are no registered Telegram slash commands (`/start`, `/help`, etc.) — all routing is done by message content parsing.

---

## Error Handling

### LLM/Agent Errors

Structured `ClamErrorPayload` with `user_message` field is sent to Telegram as a text message. Never crashes silently.

### Telegram API Errors

Wrapped in try/except; logged. Gateway continues running.

### Connection Errors

`python-telegram-bot`'s long-polling handles reconnection automatically.

---

## Proxy Configuration

For environments where Telegram's API is blocked:

```json
"proxy": "socks5://user:password@proxy.example.com:1080"
```

Passed to `python-telegram-bot`'s `HTTPXRequest` proxy parameter.
