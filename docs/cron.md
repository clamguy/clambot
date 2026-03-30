# ClamBot — Cron Scheduling System

## Overview

ClamBot's cron subsystem (`clambot.cron`) provides persistent, timezone-aware job scheduling that:

- Persists jobs to disk (survive restarts)
- Executes via the same agent pipeline as user messages
- Delivers results to Telegram (optional)
- Syncs immediately when jobs are added/removed at runtime
- Logs all execution events to a JSONL audit file

---

## Schedule Types

| Kind | Config Example | Description |
|------|---------------|-------------|
| `every` | `{"every_seconds": 3600}` | Repeat every N seconds |
| `cron` | `{"cron": "0 9 * * *", "timezone": "UTC"}` | 5-field cron expression |
| `at` | `{"at_ms": 1772618400000}` | One-time execution at epoch milliseconds |

### `every` Schedule

```json
{
  "every_seconds": 3600
}
```

- Interval in seconds
- Supports string suffixes in CLI: `60s`, `5m`, `2h`, `1d`

### `cron` Schedule

```json
{
  "cron": "0 9 * * 1-5",
  "timezone": "America/New_York"
}
```

5-field cron syntax: `minute hour day-of-month month day-of-week`

Supported syntax:
- `*` — any value
- `5` — exact value
- `1-5` — range
- `*/2` — step (every 2)
- `1,3,5` — list

Timezone: IANA timezone names via `zoneinfo.ZoneInfo` (e.g., `"America/New_York"`, `"Europe/London"`, `"UTC"`).

### `at` Schedule

```json
{
  "at_ms": 1772618400000
}
```

One-time execution. After execution: if `delete_after_run=true`, job is removed from store.

Also accepts ISO8601 strings in CLI: `2025-12-31T09:00:00Z`

---

## Job Data Model

```python
@dataclass
class CronJob:
    id: str                    # UUID-prefixed identifier
    name: str                  # Human-readable name
    enabled: bool              # Whether job fires
    schedule: CronSchedule     # Schedule definition
    payload: CronPayload       # What to do when fired
    state: CronJobState        # Runtime state (next_run_at_ms, etc.)
    created_at_ms: int
    updated_at_ms: int
    delete_after_run: bool     # Remove after first execution (for `at` schedules)
```

### CronPayload

```python
@dataclass
class CronPayload:
    kind: str          # "agent_turn" or "system_event"
    message: str       # Message to send to agent
    deliver: bool      # Whether to deliver result to a channel
    channel: str       # Channel to deliver to (e.g., "telegram")
    target: str        # Target within channel (e.g., chat_id)
    metadata: dict     # Extra metadata
```

### CronJobState

```python
@dataclass
class CronJobState:
    next_run_at_ms: int | None
    last_run_at_ms: int | None
    last_status: str | None     # "ok", "error", "skipped"
    last_error: str | None
```

---

## Persistence

Jobs stored at `~/.clambot/cron/jobs.json`:

```json
{
  "version": 1,
  "schema": "clambot.cron.jobs",
  "jobs": [
    {
      "id": "job-abc123",
      "name": "openrouter_credits_balance_hourly",
      "enabled": true,
      "schedule": {
        "cron": "0 * * * *",
        "timezone": "UTC"
      },
      "payload": {
        "kind": "agent_turn",
        "message": "Send OpenRouter credits balance.",
        "deliver": true,
        "channel": "telegram",
        "target": "6373557528",
        "metadata": {}
      },
      "state": {
        "next_run_at_ms": 1704067200000,
        "last_run_at_ms": 1704063600000,
        "last_status": "ok",
        "last_error": null
      },
      "created_at_ms": 1700000000000,
      "updated_at_ms": 1704063600000,
      "delete_after_run": false
    }
  ]
}
```

Writes use atomic replacement (write to temp file → rename) to prevent corruption.

---

## Scheduler Implementation

### InMemoryCronService

```python
class InMemoryCronService:
    async def _run(self):
        while True:
            now_ms = current_time_ms()
            due_jobs = [j for j in self._jobs if j.state.next_run_at_ms <= now_ms]
            
            for job in due_jobs:
                await self._executor(job)
                job.state.last_run_at_ms = now_ms
                job.state.next_run_at_ms = calculate_next(job.schedule, now_ms)
                await self._save()
            
            # Sleep until next job is due
            next_ms = min(j.state.next_run_at_ms for j in self._jobs if j.enabled)
            sleep_seconds = max(0, (next_ms - current_time_ms()) / 1000)
            
            # asyncio.Event allows instant wake on job change
            with timeout(sleep_seconds):
                await self._change_event.wait()
```

- Uses `asyncio.Event` for instant wakeup when jobs are added/removed (no need to wait for next poll)
- Calculates `next_run_at_ms` from current time after each execution
- Saves state after each job run

### Runtime Sync Hook

When a generated clam uses the `cron` tool to add/remove jobs:

```python
def configure_cron_tool_runtime_sync_hook(cron_service: InMemoryCronService):
    """Wire cron tool to update live scheduler without restart"""
    cron_tool.on_add = lambda job: cron_service.add_job(job)
    cron_tool.on_remove = lambda job_id: cron_service.remove_job(job_id)
```

Changes are:
1. Written to `jobs.json` (persistence)
2. Immediately reflected in `InMemoryCronService` (live)
3. `_change_event.set()` wakes the scheduler to recalculate sleep time

---

## Execution Flow

When a job fires:

```
CronService._executor(job)
│
├─ Log: cron_executor_started
│
├─ orchestrator.process_inbound_async(
│     InboundMessage(
│       channel="cron",
│       source="cron:executor",
│       chat_id="<job_id>",
│       content=job.payload.message,
│       metadata={job_id, job_name, schedule, ...}
│     )
│   )
│
├─ Agent pipeline runs (same as Telegram)
│   └─ result: OutboundMessage or None
│
├─ Log: cron_orchestrator_completed
│
├─ if payload.deliver == true and result:
│   └─ outbound message with channel=job.payload.channel,
│                             target=job.payload.target
│   └─ bus.outbound.put(outbound)
│   └─ Log: cron_delivery_queued
│
└─ Update job state: last_run_at_ms, last_status, next_run_at_ms
```

---

## Delivery to Telegram

When a cron job delivers to Telegram:

```json
"payload": {
  "deliver": true,
  "channel": "telegram",
  "target": "6373557528"
}
```

The `target` is the Telegram `chat_id`. The response is sent as a new message (not a reply).

### Default Delivery for Bot-Created Jobs

When a user in a Telegram chat asks the agent to create a cron job:

```
User: "Remind me every morning at 9am to check my emails"
```

The agent uses the `cron` tool:

```javascript
await cron({
  op: "add",
  schedule: { cron: "0 9 * * *", timezone: "UTC" },
  payload: {
    message: "Remind the user to check emails",
    deliver: true
    // channel and target are auto-filled by the runtime
    // with the originating Telegram channel + chat_id
  }
});
```

The runtime automatically fills `channel` and `target` from the current turn's context when not specified.

---

## CLI Management

```bash
# List all jobs
clambot cron list

# Add a job
clambot cron add \
  --name "daily_weather" \
  --message "What's the weather in New York?" \
  --cron "0 8 * * *" \
  --timezone "America/New_York" \
  --deliver \
  --channel telegram \
  --target 123456789

# Add an interval job
clambot cron add \
  --name "health_check" \
  --message "Check system status" \
  --every 3600

# Add a one-time job
clambot cron add \
  --name "reminder" \
  --message "Meeting in 5 minutes" \
  --at "2025-12-31T09:00:00Z" \
  --delete-after-run

# Remove a job
clambot cron remove job-abc123

# Enable/disable
clambot cron enable job-abc123
clambot cron disable job-abc123

# Manually trigger a job
clambot cron run job-abc123
```

---

## Agent Cron Tool (From JavaScript)

Generated clams can manage cron jobs:

```javascript
// Add recurring job
const result = await cron({
  op: "add",
  schedule: {
    cron: "0 * * * *",       // every hour
    timezone: "UTC"
  },
  payload: {
    message: "Check OpenRouter API credits balance",
    deliver: true             // channel/target auto-filled
  }
});
// result.id = "job-uuid-..."

// Add interval job
await cron({
  op: "add",
  schedule: { every_seconds: 1800 },  // every 30 minutes
  payload: { message: "..." }
});

// List all jobs
const { jobs } = await cron({ op: "list" });

// Remove a job
await cron({ op: "remove", id: "job-abc123" });
```

**Approval required** for `add` and `remove` operations. `list` is always allowed.

---

## Audit Logging

All cron execution events are logged to:

```
~/.clambot/workspace/logs/gateway_cron_events.jsonl
```

Event types:

```jsonl
{"event": "cron_executor_started", "job_id": "job-abc", "job_name": "daily_check", "timestamp": "2025-01-01T09:00:00Z"}
{"event": "cron_orchestrator_invoked", "job_id": "job-abc", "correlation_id": "uuid", "processing_ms": 0}
{"event": "cron_orchestrator_completed", "job_id": "job-abc", "correlation_id": "uuid", "processing_ms": 1234, "status": "ok"}
{"event": "cron_delivery_queued", "job_id": "job-abc", "correlation_id": "uuid", "channel": "telegram", "target": "123456789"}
{"event": "cron_outbound_result", "job_id": "job-abc", "outbound_status": "ok"}
{"event": "cron_executor_error", "job_id": "job-abc", "error": "...", "timestamp": "..."}
```

---

## Reliability Patterns

### Gateway Restart Recovery

When the gateway restarts:
1. `jobs.json` is loaded → all jobs restored with their last known `next_run_at_ms`
2. Jobs that were due while offline: `next_run_at_ms` is in the past → fire immediately on startup
3. No missed executions are retried retroactively (fire once, then reschedule)

### Error Handling

Job execution errors:
- `last_status = "error"`, `last_error = <message>` saved to `jobs.json`
- Job remains enabled — will retry on next schedule
- Error logged to audit log
- Does not crash the scheduler loop

### Disabled Jobs

`enabled=false` jobs are skipped during execution. Their `next_run_at_ms` is not recalculated until re-enabled. On re-enable, `next_run_at_ms` is recalculated from current time.

### One-Time Jobs (`at` schedule)

After a one-time job fires:
- If `delete_after_run=true`: job removed from `jobs.json`
- If `delete_after_run=false`: job remains but `next_run_at_ms` is `None` — never fires again

---

## Live Example: OpenRouter Credits Balance

The production `jobs.json` contains this job:

```json
{
  "id": "openrouter_credits_balance_hourly",
  "name": "openrouter_credits_balance_hourly",
  "enabled": true,
  "schedule": {
    "cron": "0 * * * *",
    "timezone": "UTC"
  },
  "payload": {
    "kind": "agent_turn",
    "message": "Send OpenRouter credits balance.",
    "deliver": true,
    "channel": "telegram",
    "target": "6373557528",
    "metadata": {}
  }
}
```

This fires at the top of every hour (`:00`), runs the agent with "Send OpenRouter credits balance." as the message, and delivers the result to Telegram chat `6373557528`.

The agent will:
1. Select or generate a clam that calls the OpenRouter API (using stored API key via `http_request` with `bearer_secret`)
2. Format the credits balance as a readable message
3. Return it as the response
4. The cron executor delivers it to Telegram
