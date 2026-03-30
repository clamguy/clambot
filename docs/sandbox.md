# ClamBot — WASM Sandbox & Clam Authoring

## What Is the Sandbox

The `amla-sandbox` library (v0.1.7, local dependency) runs JavaScript code inside a WebAssembly (WASM) environment using Wasmtime + QuickJS. This means:

- Generated JavaScript is **never executed via `exec()`, `subprocess.run()`, or `eval()` on the host**
- The sandbox has a **separate memory space** (WASM linear memory, bounds-checked)
- Host resources (filesystem, network, secrets) are accessed only through **declared, capability-checked tool calls**
- The sandbox can be **cancelled and aborted** programmatically

---

## Sandbox Properties

| Property | Value |
|----------|-------|
| WASM runtime | Wasmtime ≥29.0.0 |
| JS engine | QuickJS (ES2020) |
| Process isolation | Full WASM memory isolation |
| Network access | None by default |
| Filesystem | Virtual (WASI-based) — see VFS section |
| Shell builtins | `grep`, `jq`, `tr`, `head`, `tail`, `sort`, `uniq`, `wc`, `cut`, `cat` |
| Native Node.js modules | NOT available |
| GPU access | NOT available |
| `require()` | NOT available |
| `import` (ESM) | NOT available |
| Async/await | Fully supported |

---

## Virtual Filesystem (VFS)

The sandbox has its own isolated VFS — separate from the host filesystem:

| VFS Path | Access | Host Mapping |
|----------|--------|-------------|
| `/workspace` | Read + Write | `~/.clambot/workspace/` |
| `/tmp` | Read + Write | Ephemeral temp dir |
| `/` (root) | Read-only | Limited host dirs |

**Critical distinction**: When a clam calls `await fs({op:"write", path:"output.json", ...})`, the `fs` Python host tool resolves `output.json` against the **host workspace path** (`~/.clambot/workspace/output.json`). This is **different** from the sandbox's internal `/workspace/output.json`. `/workspace/...` paths are valid in sandbox-native VFS APIs, but ClamBot's generated clams are expected to use host tools, and the host `fs` tool rejects `/workspace/...` prefixes. Use relative paths or absolute host paths in `fs` tool calls.

---

## Tool Call Execution Model

Tool calls in JavaScript yield execution back to the Python host:

```
JavaScript: await http_request({method:"GET", url:"https://api.example.com"})
                     │
                     │ sandbox yields (pauses execution)
                     ▼
Python: ApprovalGate.evaluate_request(tool_name, args)
                     │
                     │ dispatches to BuiltinToolRegistry
                     ▼
Python: execute tool (e.g., make HTTP request)
                     │
                     │ resume sandbox with result
                     ▼
JavaScript: result = {ok: true, status_code: 200, content: "..."}
```

This is a **cooperative multitasking** model — the sandbox pauses on every tool call. There is no parallelism within a single clam execution.

---

## Sandbox Limitations (Critical for Clam Authors)

### 1. JavaScript Only

Only `language: javascript` is accepted. Shell scripts (`run.sh`), Python, and other languages are rejected at compatibility check with a clear error. There is no workaround — the sandbox only runs QuickJS.

### 2. No Native Modules

```javascript
// WILL FAIL:
const fs = require('fs');
const https = require('https');
const crypto = require('crypto');

// CORRECT: Use declared tools instead
const result = await fs({ op: "read", path: "data.txt" });
const response = await http_request({ method: "GET", url: "https://..." });
```

### 3. Object Argument Syntax Required

All tool calls must use object syntax. Positional arguments are not supported:

```javascript
// WRONG:
await web_fetch("https://example.com");

// CORRECT:
await web_fetch({ url: "https://example.com" });
```

### 4. Tool Name Transformation

Characters `.`, `/`, `-`, `:` in tool names are transformed to `_` inside the sandbox. Use the transformed names in JavaScript:

| Configured Name | JavaScript Name |
|----------------|-----------------|
| `http.request` | `http_request` |
| `fs` | `fs` |
| `web_fetch` | `web_fetch` |
| `secrets_add` | `secrets_add` |
| `memory_recall` | `memory_recall` |
| `memory_search_history` | `memory_search_history` |
| `echo` | `echo` |
| `cron` | `cron` |

### 5. Only Declared Tools Available

Tools must be listed in `CLAM.md` under `tools:` AND declared in the generation response `declared_tools`. Calling an undeclared tool results in a runtime error.

### 6. No Network from JavaScript

JavaScript cannot make HTTP calls directly. Use the `http_request` or `web_fetch` tools, which go through the host:

```javascript
// WRONG (will fail silently or error):
const response = await fetch("https://api.example.com");

// CORRECT:
const response = await http_request({
  method: "GET",
  url: "https://api.example.com",
  headers: { "Accept": "application/json" }
});
```

### 7. No Infinite Loop Protection

WASM does not limit JavaScript instruction count. An infinite loop will run until the timeout fires. Configure `timeout_seconds` appropriately and avoid unbounded loops.

### 8. No Parallel Tool Calls

Each `await tool(...)` is sequential. There is no mechanism for parallel tool calls within a single clam execution.

### 9. Large Scripts via stdin

Scripts larger than ~7KB are piped via stdin instead of inline command line. This is handled automatically — no action required.

### 10. PCA Token (Dev vs Production)

The sandbox uses an ephemeral authority (PCA) token for tool call authentication:
- **Dev mode**: Auto-generated per execution (no config needed)
- **Production mode**: Requires explicit `pca_bytes` and `trusted_authorities` configuration

For ClamBot usage, the `AmlaSandboxRuntimeBackend` handles PCA generation automatically.

---

## Writing Correct Clam Scripts

### Basic Structure

```javascript
// Option 1: Simple script (no inputs)
const result = await web_fetch({ url: "https://example.com" });
console.log("Fetched:", result.length, "chars");
return result;

// Option 2: Function with inputs (inputs injected as clamInputs, run(clamInputs) called)
async function run(args) {
  const { symbol = "BTC", currency = "USD" } = args;
  
  const response = await http_request({
    method: "GET",
    url: `https://api.coinbase.com/v2/prices/${symbol}-${currency}/spot`
  });
  
  if (!response.ok) {
    return { error: response.error, status: response.status_code };
  }
  
  const data = JSON.parse(response.content);
  return { price: data.data.amount, currency: data.data.currency };
}
```

### Return Values

- String return → returned as-is to user
- Object/Array return → serialized to JSON
- No return → empty output (triggers post-runtime analysis)

### File Operations

```javascript
// Write to workspace (use relative paths)
await fs({ op: "write", path: "results.json", content: JSON.stringify(data) });

// Read from workspace
const content = await fs({ op: "read", path: "results.json" });
const data = JSON.parse(content.result);

// List directory
const listing = await fs({ op: "list", path: "." });

// Edit file (find-and-replace)
await fs({ op: "edit", path: "file.txt", old_text: "old value", new_text: "new value" });
```

### Authenticated HTTP

```javascript
// Using stored secret for auth
const response = await http_request({
  method: "GET",
  url: "https://api.example.com/protected",
  auth: { type: "bearer_secret", name: "my_api_key" }
});

// Manual headers
const response = await http_request({
  method: "POST",
  url: "https://api.example.com/data",
  headers: {
    "Content-Type": "application/json",
    "X-Custom-Header": "value"
  },
  body: JSON.stringify({ key: "value" })
});
```

### Error Handling

```javascript
async function run(args) {
  const response = await http_request({
    method: "GET",
    url: `https://api.example.com/${args.id}`
  });
  
  if (!response.ok) {
    return {
      error: `HTTP ${response.status_code}: ${response.error || "Request failed"}`,
      ok: false
    };
  }
  
  try {
    const data = JSON.parse(response.content);
    return { ok: true, data };
  } catch (e) {
    return { error: "Failed to parse response as JSON", content: response.content };
  }
}
```

### Using Cron from a Clam

```javascript
// Schedule a recurring job
const result = await cron({
  op: "add",
  schedule: {
    cron: "0 9 * * 1-5",  // 9am weekdays
    timezone: "America/New_York"
  },
  payload: {
    message: "Good morning! Check for urgent emails.",
    deliver: true
  }
});

// List existing jobs
const jobs = await cron({ op: "list" });

// Remove a job
await cron({ op: "remove", id: "job-abc123" });
```

---

## CLAM.md Authoring

```yaml
---
manifest_version: "1"
description: "Fetches cryptocurrency price from CoinGecko API"
language: javascript
tools:
  - http_request
inputs:
  - name: symbol
    type: string
    description: "Coin symbol (e.g. BTC, ETH)"
    required: true
  - name: currency
    type: string
    description: "Quote currency (default: USD)"
    required: false
metadata:
  reusable: true
  source_request: "what is the price of BTC"
---

Fetches the current USD price of a cryptocurrency using the CoinGecko public API.
No API key required. Returns price, market cap, and 24h change.
```

### Reusability

Set `metadata.reusable: true` and `metadata.source_request: "<normalized request>"` to enable exact-match reuse. The system applies NFKC Unicode normalization + punctuation stripping to both the stored request and incoming messages before comparison.

A clam is reused when:
1. `metadata.reusable: true`
2. The normalized current request matches `metadata.source_request`
3. The clam is in `clams/` (not `build/`)

When reused, no LLM call is made — the clam is selected and executed immediately.

---

## Runtime Error Codes

All errors are returned as structured `ClamErrorPayload` with stable codes:

| Code | Stage | Description |
|------|-------|-------------|
| `runtime_timeout_unresponsive` | RUNTIME | Clam exceeded timeout + grace period |
| `runtime_execution_error` | RUNTIME | JavaScript threw uncaught exception |
| `capability_violation` | RUNTIME | Tool call violated capability policy |
| `pre_runtime_secret_requirements_unresolved` | PRE_RUNTIME | Required secret not found |
| `incompatible_language` | COMPATIBILITY | Non-JavaScript language specified |
| `input_unavailable` | PRE_RUNTIME | Secret resolution failed, no prompt available |
| `authorization_header_conflicts_with_auth` | RUNTIME | Both manual header and auth field specified |

---

## Capability Policies

Clam metadata can restrict tool usage via `capability_policy`:

```yaml
metadata:
  capability_policy:
    - method: "http_request"
      constraints:
        - param: "method"
          is_in: ["GET", "POST"]
        - param: "url"
          starts_with: "https://api.coinbase.com/"
      max_calls: 5
```

### Constraint DSL

| Constraint | Example | Description |
|-----------|---------|-------------|
| `is_in` | `is_in: ["GET", "POST"]` | Value must be in list |
| `starts_with` | `starts_with: "https://api."` | String prefix match |
| `<=` | `<= 10000` | Numeric less-than-or-equal |
| `>=` | `>= 0` | Numeric greater-than-or-equal |

Violations during execution raise `CapabilityEvaluationError` and terminate the clam.
