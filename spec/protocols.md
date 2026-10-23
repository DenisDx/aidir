# aidir — Protocol and API reference

> Satellite document of [SPEC_NEW.md](../SPEC_NEW.md). See
> [§5 Protocols and APIs](../SPEC_NEW.md#5-protocols-and-apis) there for the
> architecture context.
>
> This document describes the complete HTTP surface: every route, the request and
> response contracts, the headers, the error envelopes and the streaming rules.
>
> The OpenAIx protocol extensions over plain OpenAI/Ollama are additionally specified
> normatively in [../openaix_spec.md](../openaix_spec.md). Where the two disagree,
> `openaix_spec.md` wins for the extension semantics and this document wins for the
> current route table.

---

## 1. Common conventions

### 1.1 Request handling pipeline

Every endpoint runs the same ASGI stack:

```
RequestBodyLimitMiddleware      reject a body above http.max_request_size with 413
attach_request_id_middleware    set/propagate X-Request-ID
_attach_ingress_logging         log the inbound method and path
@app.exception_handler           500 {"error": {"code": "INTERNAL_ERROR", ...}}
```

The body limit is enforced from the declared `Content-Length` first, then against the
buffered body. The whole body is buffered in memory, so the effective per-request
memory cost is the limit itself (100 MiB by default).

### 1.2 Request id

`X-Request-ID` is taken from the request header, or generated as a 16-hex-character
uuid. It is always set on the response and attached to every error body and to every
log line produced while handling the request.

### 1.3 Authentication model (endpoints)

Authorization is **optional**. The rules:

1. Extract the token: the `x-api-key` header first, then `Authorization: Bearer`.
2. If no token was supplied → the request proceeds **anonymously**.
3. If a token was supplied → it must match an entry in `config.users.items[].token`
   (compared with `hmac.compare_digest`), otherwise **401**.
4. With a valid token:
   - if the body carries no `envid`, the account's `autoassign_envid` is applied;
   - if the body carries an `envid`, it must be listed in the account's `envids[]`
     (**403** otherwise) and must exist in the envid registry (**400 INVALID_ENVID**).

The routes listed as *unauthenticated* below skip this entirely.

### 1.4 Error envelopes

```jsonc
// OpenAI style
{"error": {"message": "...", "type": "<code>", "code": "<code>", "param": "...", "task_id": "..."}}

// Ollama style
{"error": {"code": "...", "message": "...", "task_id": "..."}}

// Anthropic style
{"type": "error", "error": {"type": "timeout_error", "message": "..."}}
```

`errors_compatibility_mode` (default `true`) selects between the OpenAI and the
Ollama shape. With `false`, every error uses the flat
`{"error": {"code", "message"}}` shape.

**The upstream-error exception.** When the failure came from the executor and
carries an original body, the client receives the **upstream's own** status, body
and content type. aidir does not re-wrap it. This applies to `llama-cpp` providers
explicitly, and to every provider through `upstream_error_response`. Locally
generated failures (connection failures, timeouts, validation) use the envelopes
above.

Status mapping for locally generated failures:

| Situation | Status | Code |
|---|---|---|
| Queue timeout | 504 | `QUEUE_TIMEOUT` |
| Run timeout | 504 | `TIMEOUT` |
| Upstream timeout | 504 | `UPSTREAM_TIMEOUT` |
| Task canceled | 503 | `CANCELED` |
| Any other worker failure | 502 | `UPSTREAM_ERROR` |
| Unknown model | 404 | `INVALID_MODEL` / `model_not_found` |
| Embedding request on a chat-only model | 422 | `EMBEDDING_NOT_SUPPORTED` |
| Routing loop | 409 | `ROUTING_LOOP` |
| Too many hops | 409 | `ROUTING_HOPS_EXCEEDED` |
| The service is restarting | 503 | `RESTART_IN_PROGRESS` |

### 1.5 Routing trace headers

| Header | Direction | Meaning |
|---|---|---|
| `X-Aidir-Route-Id` | both | The route id of the current hop |
| `X-Aidir-Visited-Instances` | both | The instance ids already visited |

The endpoint rejects a request that names the current instance in the visited list
(**409 `ROUTING_LOOP`**) or that exceeds `routing.max_hops` (default 8, **409
`ROUTING_HOPS_EXCEEDED`**). When the target provider has `api: "openaix"`, the
worker adds both headers to the upstream request.

### 1.6 Streaming rules (all endpoints)

The endpoint waits for the **first chunk** before opening the response, so a failure
before any output keeps a meaningful HTTP status.

Once streaming has begun:

- the HTTP status is already sent and cannot change;
- a failure is delivered as an error event in the stream's own format
  (SSE `data: {...}` for `/v1/*`, NDJSON `{"error": ..., "done": true}` for `/api/*`);
- an upstream error that arrives mid-stream is forwarded as the original JSON error
  object, not as an aidir envelope.

---

## 2. OpenAIx endpoint (`api: "openaix"`, default port 21434)

### 2.1 Route table

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/chat` | token | Ollama-compatible chat |
| POST | `/api/embed` | token | Ollama-compatible embeddings |
| GET | `/api/tags` | none | Ollama-shaped model list |
| POST | `/api/show` | none | Ollama-shaped model metadata |
| POST | `/v1/chat/completions` | token | OpenAI-compatible chat |
| POST | `/v1/embeddings` | token | OpenAI-compatible embeddings |
| GET | `/v1/models` | none | OpenAI-shaped model list |
| GET | `/api/providers/{provider_id}/models/{model_id}/queue-state` | none | Queue state |
| GET | `/v1/providers/{provider_id}/models/{model_id}/queue-state` | none | Queue state |
| GET | `/api/models/{model_id}/queue-state` | none | Queue state, alias only |
| GET | `/v1/models/{model_id}/queue-state` | none | Queue state, alias only |
| GET | `/api/resources` | peer auth | Peer resource list |
| GET | `/api/resources/{resource_id}` | peer auth | One peer resource |
| GET | `/v1/resources` | peer auth | Peer resource list |
| GET | `/v1/resources/{resource_id}` | peer auth | One peer resource |
| GET | `/v1/tasks` | peer auth | Peer task list |
| GET | `/health` | none | `{"status": "ok"}` |

The `{model_id}` path converter accepts slashes, so a model id may contain them.

### 2.2 `POST /api/chat`

Request — the Ollama chat payload plus the aidir extensions:

```jsonc
{
  "model": "llama3",                     // required
  "messages": [ {"role": "user", "content": "..."} ],
  "stream": false,
  "options": { "temperature": 0.2 },    // generation parameters
  "tools": [ ... ],                      // caller-owned tool definitions
  "worker": "call_ollama",              // aidir: force a worker
  "envid": "proj_x",                    // aidir: execution environment
  "timeout": 120,                       // aidir: run timeout (and the upstream call)
  "queue_timeout": 300,                 // aidir: queue timeout
  "context_builder": { ... },           // aidir: per-request context overrides
  "log": { "options": { "save_llm_request": true } },
  "priority": 5
}
```

Response, `stream: false` — the upstream's Ollama-shaped body:

```jsonc
{
  "model": "...",
  "created_at": "...",
  "message": { "role": "assistant", "content": "..." },
  "done": true,
  "done_reason": "stop",
  "prompt_eval_count": 12,
  "eval_count": 34
}
```

Response, `stream: true` — `application/x-ndjson`, one JSON object per line, the last
one carrying `"done": true`. When the executor also speaks Ollama, the **original
bytes and line delimiters** are forwarded.

**Timeout coupling.** When only `timeout` is given, `queue_timeout` becomes
`run_timeout + max(0, default_queue - default_run)`. This preserves the intended
split between the two phases when the caller shortens only the run phase.

### 2.3 `POST /v1/chat/completions`

Request — the OpenAI chat payload plus the same aidir extensions.

Response, `stream: false`:

```jsonc
{
  "id": "chatcmpl-<task_id>",
  "object": "chat.completion",
  "created": 1730000000,
  "model": "...",
  "choices": [ { "index": 0, "message": { "role": "assistant", "content": "..." },
                 "finish_reason": "stop" } ],
  "usage": { "prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46 }
}
```

`finish_reason` comes from the Ollama `done_reason`: `stop` → `stop`,
`tool_calls` → `tool_calls`.

Response, `stream: true` — `text/event-stream` with `data: {...}\n\n` frames shaped
as `chat.completion.chunk`, terminated by `data: [DONE]\n\n`.

**The `[DONE]` terminator is emitted only when the stream was not a native
passthrough.** When the upstream spoke OpenAI, its own terminator already arrived
and aidir does not append a second one.

**Reasoning.** Ollama `thinking` is exposed as OpenAI `reasoning_content` in both
the synchronous body and the streamed delta. Reasoning is never substituted for an
empty `content`.

### 2.4 `POST /api/embed` and `POST /v1/embeddings`

Both create a normal queued `Task_agent` with `request_kind: "embed"`.

Validation:

- `model` is required and must be a non-empty string;
- `input` must be a string or an array of strings (token arrays and mixed arrays are
  rejected);
- `stream` is rejected: *"Embedding requests do not support streaming"*;
- `tools`, `tool_choice` and `context_builder` are rejected;
- the model must declare `embedding: true`, otherwise **422
  `EMBEDDING_NOT_SUPPORTED`** (checked before *and* after smart routing).

`/api/embed` response (Ollama shape):

```jsonc
{ "model": "...", "embeddings": [[0.1, 0.2, ...]], "prompt_eval_count": 8 }
```

`/v1/embeddings` response (OpenAI shape):

```jsonc
{
  "object": "list",
  "data": [ { "object": "embedding", "embedding": [ ... ], "index": 0 } ],
  "model": "..."
}
```

`encoding_format: "base64"` packs the float vector as little-endian `float32` bytes
and base64-encodes them. `user` from the request body is stored in the task metadata
only. `usage` is included only when the upstream supplied counters.

Embeddings skip the context chain, the internal tools, the tool loop and the chat
generation defaults.

### 2.5 `GET /api/tags` and `GET /v1/models`

Both are synthesized **entirely from the configuration**; the upstream is never
contacted.

```jsonc
// /api/tags
{"models": [{"name": "alias-or-id", "model": "alias-or-id", "real_id": "internal-id",
             "modified_at": "<now>", "size": 0, "digest": "", "details": {}}]}

// /v1/models
{"object": "list", "data": [{"id": "alias-or-id", "real_id": "internal-id",
                             "object": "model", "created": <ts>, "owned_by": "aidir"}]}
```

`real_id` is the internal model id selected by the current routing rules — an aidir
extension over both upstream APIs. `size` is always `0` and `digest` always `""`;
clients that rely on those fields for cache management will misbehave.

### 2.6 `POST /api/show`

Ollama-shaped metadata synthesized from the configuration:

```jsonc
{
  "modelfile": "",
  "parameters": "temperature=0.2\ntop_p=0.9",
  "template": "",
  "details": { "parent_model": "", "format": "gguf", "family": "unknown",
               "families": ["unknown"], "parameter_size": "", "quantization_level": "" },
  "model_info": { "aidir.provider": "...", "aidir.provider_api": "...",
                  "aidir.model": "...", "aidir.requested_name": "...",
                  "aidir.context_window": 32768, "aidir.context_length": 0 },
  "capabilities": ["completion", "chat"]
}
```

An unknown model is **404 `INVALID_MODEL`**.

### 2.7 The queue-state endpoints

`GET /api/providers/{provider_id}/models/{model_id}/queue-state` and its three
twins. The query parameter `priority` defaults to `5`; a negative value is **400
`INVALID_REQUEST`**.

```jsonc
{
  "provider": "ollama_local",
  "model": "llama3",
  "priority": 5,
  "can_run_now": true,
  "queued_count_below_priority": 0,
  "queued_count_total": 3,
  "priority_counts": [ {"priority": 5, "count": 3} ],
  "resource_ready": true,
  "resource_ready_after_unload": true,
  "resource_reusable": false,
  "active_count": 2,
  "max_concurrent_requests": 4,
  "session_resources": { "local_machine": { "VRAM": 1200 } },
  "model_loaded": true,
  "admission": "ready",
  "free": { "VRAM": 5000 },
  "needed": { "VRAM": 1200 },
  "memory_slots": 4
}
```

When the resolved route differs from the requested one, `requested_provider` and
`requested_model` are added.

`provider_id: "default"` resolves through the alias rules; a smart provider is
resolved asynchronously through the `SmartRouter`.

`can_run_now` requires **both** an admissible model **and**
`blocked_by_same_or_higher == 0`, where `blocked = queued_count_total -
queued_count_below_priority`.

Unknown provider or model → **404 `INVALID_MODEL`**. Queue unavailable → **503
`QUEUE_UNAVAILABLE`**.

The counters are computed by scanning both ZSETs and issuing one `HGETALL` per
queued task, so the endpoint is `O(queued tasks)` and is unauthenticated.

### 2.8 The peer endpoints

`GET /v1/resources`, `GET /v1/resources/{id}`, `GET /api/resources`,
`GET /api/resources/{id}`.

```jsonc
{
  "object": "list",
  "protocol_version": 1,
  "data": [
    {
      "id": "local_machine",
      "type": "cuda",
      "limits": { "VRAM": 22000 },
      "units": { "VRAM": "MiB" },
      "availability": { "source": "command", "status": "ok",
                        "available": { "VRAM": 8000 }, "observed_at": <ts>, "error": null },
      "telemetry": { "sensors": [ { "id": "gpu_temperature", "label": "...",
                                    "unit": "C", "status": "ok", "value": 51.0,
                                    "updated_at": <ts>, "error": null } ] }
    }
  ]
}
```

Only the sensor ids listed in `resources[].telemetry.sensors` are published, and
only their scalar fields. The sensor command, the monitoring configuration, the
credentials and the consumer list are never exposed.

`GET /v1/tasks` returns the active tasks (`created`, `queued`, `running`), sorted by
`updated_at`/`created_at` descending, capped at 1000 rows:

```jsonc
{ "object": "list", "protocol_version": 1,
  "data": [ { "id": "...", "type": "agent", "status": "running",
              "worker_id": "openaix", "priority": 5,
              "created_at": "...", "updated_at": "...",
              "route_provider_id": "ollama_local", "model_id": "...",
              "requested_model": "...", "route_id": "...",
              "llm_call_count": 2, "envid": "...", "parent_worker": "...",
              "request_preview": "<300 chars>" } ] }
```

No request or response bodies are exposed.

**`auth_mode`** on the peer's `openaix` endpoint governs both families:
`disabled` (default) publishes them unauthenticated; `endpoint` requires the normal
bearer-token and environment authorization checks.

---

## 3. Anthropic Messages endpoint (`api: "anthropic"`, default port 21435)

### 3.1 Route table

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/v1/messages` | token | Anthropic Messages |
| POST | `/v1/messages/count_tokens` | token | Local token estimate |
| GET | `/v1/models` | none | Anthropic-shaped model list |
| GET | `/health` | none | Health check |

The endpoint **replaces** the OpenAIx route table; it does not also serve
`/api/chat`.

### 3.2 `POST /v1/messages`

Request:

```jsonc
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 1024,                  // required, positive integer
  "system": "You are ...",             // string or a block list
  "messages": [
    { "role": "user", "content": "Hello" },
    { "role": "user", "content": [ { "type": "text", "text": "..." },
                                    { "type": "image", "source": { "type": "base64", ... } } ] },
    { "role": "assistant", "content": [ { "type": "tool_use", "id": "toolu_1",
                                          "name": "search", "input": { ... } } ] },
    { "role": "user", "content": [ { "type": "tool_result", "tool_use_id": "toolu_1",
                                     "content": "..." , "is_error": false } ] }
  ],
  "stream": false,
  "tools": [ { "name": "search", "input_schema": { ... } } ],
  "tool_choice": { "type": "auto" },
  "thinking": { "type": "enabled", "budget_tokens": 1024 },
  "envid": "proj_x",                   // aidir extension
  "timeout": 120,                      // aidir extension
  "context_builder": { ... }           // aidir extension
}
```

Only the `user` and `assistant` roles are accepted. `system` becomes a leading
system message. Content blocks map as:

| Anthropic block | Internal representation |
|---|---|
| `text` | A text content part |
| `image` | An `image_url` part (base64 → a `data:` URL, or the plain URL) |
| `tool_use` | An OpenAI `tool_calls` entry **plus** an opaque copy in `anthropic_blocks` |
| `tool_result` | A separate `{"role": "tool", "tool_call_id", "content", "is_error"}` message |
| `thinking` / `redacted_thinking` | Kept opaque in `anthropic_blocks`; the text is mirrored into `reasoning_content` |

Response:

```jsonc
{
  "id": "msg_<task_id>",
  "type": "message",
  "role": "assistant",
  "model": "...",
  "content": [ { "type": "text", "text": "..." } ],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": { "input_tokens": 12, "output_tokens": 34 }
}
```

**Native passthrough.** When the upstream also speaks Messages, the upstream's exact
body, status and content type are returned unchanged — and the streamed events are
forwarded verbatim, without aidir's synthesized `finish()` events. A client sees
either a fully native stream or a fully synthetic one, never a mixture.

Stop-reason mapping:

| Internal | Anthropic |
|---|---|
| `stop` | `end_turn` |
| `length` | `max_tokens` |
| `tool_calls` | `tool_use` |
| `content_filter` | `refusal` |
| (default) | `end_turn` |

Error types:

| HTTP status | Anthropic type |
|---|---|
| 400 / 413 / 422 | `invalid_request_error` |
| 401 | `authentication_error` |
| 403 | `permission_error` |
| 404 | `not_found_error` |
| 429 | `rate_limit_error` |
| 500 / 502 | `api_error` |
| 503 / 529 | `overloaded_error` |
| 504 | `timeout_error` |

**Thinking signatures.** A thinking block keeps its `signature` only while the
corresponding assistant turn is unchanged. A context rewrite drops the signatures
with a warning. The synthesized stream encoder never emits `signature_delta`, and a
non-streaming result carries an **empty** thinking signature.

### 3.3 `POST /v1/messages/count_tokens`

Request: the same shape, but only `model` and a non-empty `messages` array are
required (the token must be valid).

```jsonc
{ "input_tokens": 128 }
```

The estimate is `ceil(total_characters / 4)` over the system text, the message text
and the serialized tool schemas. It is deterministic, local, creates **no task** and
calls **no model**. Images are not counted.

### 3.4 `GET /v1/models`

```jsonc
{
  "data": [ { "type": "model", "id": "...", "display_name": "...", "created_at": "..." } ],
  "has_more": false,
  "first_id": "...",
  "last_id": "..."
}
```

---

## 4. MCP endpoint (`api: "mcp"`, default port 20001)

### 4.1 Route table

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/mcp` | **none** | JSON-RPC 2.0 |
| GET | `/health` | none | Health check |

### 4.2 JSON-RPC methods

| Method | Behavior |
|---|---|
| `initialize` | `protocolVersion` (config `protocolVersion`, default `2024-11-05`), `capabilities.tools.listChanged = false`, `serverInfo` (config `serverName` / `serverVersion`, defaults `aidir-mcp` / `0.1.0`) |
| `ping` | `{"jsonrpc": "2.0", "id": …, "result": {}}` |
| `notifications/initialized` | HTTP **204 No Content** |
| `tools/list` | `{"result": {"tools": [...]}}` |
| `tools/call` | Runs a tool worker |
| anything else | HTTP **404** with `{"error": {"code": -32601, "message": "Method not found: …"}}` |

A parse failure is HTTP **400** with `{"error": {"code": -32700, "message": "Parse
error"}}`. An unhandled exception is HTTP 500 with the JSON-RPC `-32603` envelope and
the request id in `data`.

### 4.3 The tool registry

The registry comes from `config.endpoints[mcp].tools`, a map of
`tool_name → worker_id` or `tool_name → {worker: worker_id}`. For each entry:

- if the worker is a `BaseToolWorker`, its `get_tool_description()` supplies the
  metadata — a list contributes all its entries, a dict contributes one;
- otherwise a minimal synthetic spec is generated:
  `{"name", "description": "Tool <name>", "inputSchema": {"type": "object", "properties": {}}, "worker": worker_id}`;
- the worker id is stamped onto every description, and the registry is keyed by the
  description's `name` — so one worker may publish several tools.

`tools/list` emits `{"name", "description", "inputSchema"}` per entry.

**Note:** `endpoints[].tools` is marked deprecated in the descriptor — internal
tools belong in `workers.items.<worker_id>.tools`. The MCP endpoint still reads only
the endpoint-level block.

### 4.4 `tools/call`

```jsonc
// request
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "search", "arguments": {"query": "aidir"}}}

// success
{"jsonrpc": "2.0", "id": 1, "result": { /* the tool worker's data */ }}
```

The call becomes a queued `Task_tool` with `payload = {"tool": name, "arguments":
arguments}` and `worker_id` taken from the registry. The timeouts come from
`tasks.queue_timeout` and `tasks.run_timeout`.

| Outcome | HTTP | JSON-RPC code |
|---|---|---|
| Unknown tool | 422 | `-32602` |
| The service refused the task | 503 | `-32000` |
| Timeout | 504 | `-32001` |
| The worker failed | 502 | `-32002` |
| Canceled | 503 | `-32003` |
| Unknown state | 500 | `-32004` |

---

## 5. WebUI backend (default port 20082)

### 5.1 Authentication

`config.webui.auth`:

```json5
{
  "mode": "login",            // "login" | "open"
  "session_ttl": 86400,
  "users": [ { "login": "...", "password": "...", "permissions": ["all"] } ]
}
```

`ROOT_USER` and `ROOT_PASSWORD` are only `.env` placeholders substituted into
`webui.auth.users[0]`; the code never reads the environment variables itself.

Passwords are compared in constant time (`hmac.compare_digest`) and are stored in
plaintext. `"open"` disables the login entirely and is only safe behind an
authenticating proxy.

`POST /api/auth/login` sets an `aidir_token` cookie (`httponly`, `samesite=strict`,
`max-age = session_ttl`) and also returns the token, which may be used as
`Authorization: Bearer`. Sessions live in Redis under `aidir:session:<token>` with
the TTL above.

`GET /api/auth/me` returns `{ok, login, permissions}`; it is the endpoint the
front end uses to restore a session on page load.

`POST /api/auth/logout` deletes the session named by the cookie.

**Known gap:** logout deletes only the cookie's session; a session established with
the `Authorization` header stays alive in Redis.

### 5.2 Task routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks` | The active task list: local Redis + in-memory queue + the OpenAIx peers |
| GET | `/api/tasks/viewer/meta` | The filter vocabulary: statuses, workers, envids, recent routes |
| GET | `/api/tasks/viewer/search` | The bounded filtered search |
| GET | `/api/tasks/viewer/{task_id}` | The full task JSON (live memory, else Redis) |
| GET | `/api/tasks/viewer/{task_id}/detail` | The summary plus the lazy audit-event manifest |
| GET | `/api/tasks/viewer/{task_id}/request` | The `client_request` audit event |
| GET | `/api/tasks/viewer/{task_id}/raw` | One audit event by `type` + `event_id` |
| GET | `/api/tasks/viewer/audit-files/{file_id}` | A file-backed audit body |
| POST | `/api/tasks/{task_id}/terminate` | Cancel and delete a live task |
| POST | `/api/tasks/{task_id}/extend-timeout` | Add 60 s to the active timeout |

**`/api/tasks/viewer/search`** filters (all repeatable and comma-splittable):

| Parameter | Semantics |
|---|---|
| `status` | Matched against `task.status`, lowercased |
| `worker` | Matched against `task.worker_id` |
| `route` | Matched against the `endpoint\x1fmodel` route value |
| `envid` | Exact match |
| `created_from` / `created_to` | ISO-8601 bounds on `created_at` |
| `last_operation_from` / `last_operation_to` | ISO-8601 bounds on the last status transition |
| `limit` | Default 300, clamped to `[1, 1000]` |

The response is `{"tasks": [...], "count": <total matching>}`. Each row is a
whitelisted summary: `id`, `type`, `status`, `worker_id`, the timestamps, `priority`,
`llm_call_count`, the timeouts, `external`, `request_preview`, `route_provider_id`,
`model_id`, `envid`, `error_code`, and — only for a failed task — the first 1000
characters of the error message.

**`/api/tasks/viewer/{task_id}/raw`** requires `type` (one of `client_request`,
`client_response`, `llm_request`, `llm_response`) and `event_id`. It double-checks
ownership: an event whose `task_id` or `type` does not match is a 404. A task
therefore cannot read another task's audit event by guessing an id.

**`/api/tasks/viewer/audit-files/{file_id}`** resolves the opaque id, rejects an
absolute path or one containing `..`, requires the file to live under the audit
directory, sets `X-Content-Type-Options: nosniff`, and serves images inline and
everything else as an attachment. The download name is the opaque id, never the real
file name.

### 5.3 Status, resources and control

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | The instance, the workers, the task count, the resources, the runtime flags, the health |
| POST | `/api/cron/repair` | Add or repair the single aidir crontab entry |
| POST | `/api/cron/embedded/enable` | Set `cron.embedded = true` atomically |
| POST | `/api/resources/{resource_id}/use` | Enable or disable a resource for new reservations |
| POST | `/api/resources/{resource_id}/force-release` | Unload the idle models |
| POST | `/api/restart` | Graceful restart |

`force-release` answers **409** with the blocking consumer list when active tasks
hold the resource; it never interrupts a running task.

### 5.4 Logs

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/logs/files` | The list of log files (`.log` and `.jsonl` only) |
| GET | `/api/logs?file=&lines=` | The last N lines of a file |
| GET | `/api/logs/search?file=&contains=&limit=` | A substring search, with `count` and `truncated` |
| WS | `/ws/logs?file=&token=` | The live line stream |

**The WebSocket protocol.** The client connects with `Upgrade: websocket` and may
pass the session token as the `token` query parameter; otherwise the `aidir_token`
cookie authenticates the handshake. An invalid session closes the socket with code
**4001** and it is never accepted.

Once accepted, the server sends **one text frame per log line** — no JSON envelope,
no sequence numbers. The initial burst is at most the last 200 complete lines and at
most the last 256 KiB. After that the server polls the file once a second and emits
the appended bytes, handling three failure modes explicitly: an inode change
(rotation) resets the cursor, a truncation resets the cursor, and a partial trailing
line is buffered until its newline arrives.

The file name is resolved defensively: an empty name means `all.log`, a name without
a dot gets `.log` appended, the basename must equal the request, the suffix must be
`.log` or `.jsonl`, and the resolved parent must be exactly the logs root.
Otherwise the request is **400**. Path traversal is structurally impossible.

### 5.5 Catalog and test routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/workers/models` | The workers list plus the provider/model catalog |
| GET | `/api/endpoints/info` | The configured endpoints plus their MCP tools |
| POST | `/api/test/llm` | Proxy to the first `ollama` endpoint's `/api/chat` |
| POST | `/api/test/mcp` | Proxy JSON-RPC to the selected `mcp` endpoint |
| GET | `/api/test/agent/endpoints` | The chat-capable endpoints |
| GET | `/api/test/agent/catalog` | The provider → models map plus the envid list |
| GET | `/api/test/agent/models?endpoint_id=&protocol=` | A live model list from the endpoint |
| POST | `/api/test/agent` | Proxy a chat request in the selected protocol |

`/api/test/llm` is **hard-wired to the `ollama` API** — there is no endpoint
selection. `/api/test/agent` does support selection and maps the protocol as follows:

| Protocol | Target |
|---|---|
| `anthropic` | `/v1/messages` (the body is converted to the Messages shape) |
| `openai` | `/v1/chat/completions` |
| anything else | `/api/chat` |

An `anthropic` endpoint with a non-Anthropic protocol is **422**, and an `mcp`
endpoint is **422**.

The timeouts come from `webui.request_timeouts` (`ollama_chat`, `mcp_proxy`,
`model_list`, `agent_test`).

### 5.6 Settings routes

The host routes (the library owns everything under `/api/settings`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/settings-info` | The document ids, the asset base, the current revision, the assistant description |
| POST | `/api/settings/chat` | One turn of the configuration assistant |
| GET | `/api/settings/chat/history` | The caller's own conversation turns |
| DELETE | `/api/settings/chat` | Forget the caller's conversation |
| POST | `/api/settings/reload` | Re-read every document and re-apply it |

`POST /api/settings/chat` never writes. It returns
`{ok, message, staged_operations, activity, rounds, model}`; the browser stages the
operations in the tree and the user presses **Apply**. A disabled assistant is
**503 `UNSUPPORTED_CAPABILITY`**; an empty message is **400**.

The library routes mounted under `/api/settings` are listed in
[../spec/configuration.md](../spec/configuration.md).

---

## 6. WebUI front end

### 6.1 The pages

| Page | Contents |
|---|---|
| Dashboard | The restart card, the active-task / worker / health cards, the task queue table, the resource table, the live log card |
| Task Viewer | The filter card and the task table |
| Test LLM | The configuration, the tools, the dialog |
| Test MCP | The endpoint/method settings, the available tools, the request and the result |
| Agent Request | The endpoint and protocol, the context builder, the request logging, the timeout, the dialog |
| Settings | The editor pane and the assistant pane |

`showPage(name)` toggles the `.active` class and triggers the target page's lazy
loader.

### 6.2 The boot order

```html
<script src="/api/settings/assets/aiset.js"></script>
<script src="/api/settings/assets/aiset-tree.js"></script>
<script src="app.js"></script>
<script src="task_steps_viewer.js"></script>
```

The asset URLs are under the **API** prefix because nginx forwards `/api/`, `/ws/`
and `/assets/`, while the SPA fallback answers every other path with
`200 text/html`. The scripts must be classic, not `type="module"`: a module script
is deferred and would race with `app.js`.

The boot block of `app.js` binds the settings and task-viewer UI and installs the
resize handler. It deliberately does **not** initialize the settings backend.

The session probe at the end of `app.js` calls `/api/auth/me`. On success
`onLoggedIn()` runs, in this order:

1. show the app screen,
2. show the dashboard,
3. `initSettingsBackend()` — the editor is created **only after authentication**,
4. bind the chat UI,
5. load the tasks,
6. ensure the task-viewer metadata,
7. start the live logs,
8. initialize the test pages,
9. load the agent catalog,
10. start the 3-second task refresh timer.

`doLogout()` stops the live logs, clears the timer, destroys the settings editor
and returns to the login screen.

### 6.3 Polling

| Interval | Where | What |
|---|---|---|
| 3000 ms | `app.js` | The dashboard refresh: `/api/tasks` and `/api/status` in parallel |
| 1000 ms | the WebSocket handler | The log file poll |
| 0.1–10 s | the user | The WebSocket reconnect delay after a close |
| on resize | `app.js` | `sizeSettingsWorkspace()` recomputes `--cfg-height` |

The task refresh has an in-flight guard, because `/api/tasks` takes about four
seconds above 12000 stored task hashes.

### 6.4 The settings page

Two panes: `#cfg-aiset-card` (the editor) and `#cfg-chat-card` (the assistant).

The editor is mounted by the library:

```js
settingsEditor = window.aiset.mountTree('#settings-tree', {
  document: documentId,
  apiBase: '/api/settings',
  view: 'source',
  showAbsent: ...,
  valuesAsText: ...,
});
```

`mountTree` fills the host element directly and creates **no wrapper**, so the host
stylesheet must scope its rules to `.aiset-tree-editor` — that class has to be on
`#settings-tree`.

Three different mechanisms share the word "reload":

| Control | Owner | Effect |
|---|---|---|
| **Apply** | the library toolbar | Validate the staged batch, then commit it atomically |
| **Reload** | the library toolbar | Discard the staged changes, re-fetch the tree |
| **Reload settings** | the host button | `POST /api/settings/reload` — re-read the documents **and** re-apply them to the process |
| **Revert all** | the library toolbar | Purely client-side: it clears the staged batch |

The assistant's operations arrive at the editor through
`settingsEditor.stageOperations(staged, 'assistant')` and the user is told to review
them and press Apply.

### 6.5 The design split

| Owned by the library | Owned by this project |
|---|---|
| The markup and DOM structure | The palette (`--aiset-*` variables) |
| `aiset.css` — the light-theme defaults and the structural rules | `webui/frontend/settings.css` — the variable mapping, the badges, the toolbar buttons, the two-pane workspace, the assistant pane |

Load order is load-bearing: the host sheet must come **after** the library sheet,
because equal specificity is decided by order.

`test_settings_styles.py` fails if a class the editor emits is left undesigned, if
the order is reversed, if a host rule escapes the editor scope, or if a host control
carries an inline style.

---

## 7. WebUI asset and proxy rules

nginx forwards three prefixes to the WebUI backend and serves everything else from
the static frontend:

```nginx
location ^~ /api/    { proxy_pass http://webui_backend; proxy_read_timeout 300s; }
location ^~ /ws/     { proxy_pass http://webui_backend; proxy_read_timeout 3600s; }
location ^~ /assets/ { proxy_pass http://webui_backend; }
location ~* \.(?:js|css|map|json|svg|ico|png|jpe?g|gif|webp|woff2?|ttf|eot)$ { try_files $uri =404; }
location /           { try_files $uri $uri/ /index.html; }
```

Two rules are load-bearing:

1. **Every proxied prefix needs `^~`.** A regex location beats a plain prefix
   location, so without `^~` the asset-extension rule answers
   `/api/settings/assets/aiset.js` with a 404. `nginx -t` passes in both cases; only
   a request reveals the mistake.
2. **A missing file that names an extension must be a 404**, not the SPA page.
   Otherwise a missing script arrives as `200 text/html` and the browser can only
   report it as a missing global.

**The test must check that every asset the page names is under a prefix the
deployment forwards** — not that the backend serves it. The two questions have
different answers from the browser's point of view than from a test client's.

---

## 8. Protocol quick reference

| Question | Answer |
|---|---|
| Which protocols does an endpoint speak? | `config.endpoints[].api` |
| Which worker serves a task? | The endpoint's worker, then the provider's `api` → worker mapping, then `workers.default` |
| Which protocol does a provider speak? | `config.models.providers.<id>.api` |
| Can one provider speak two protocols? | Yes — a per-model `protocol` field overrides the provider `api` |
| Which protocol did the executor use? | `task.upstream_response.protocol` |
| When is the response byte-identical? | When `upstream_response.protocol` equals the client's protocol |
| Where is the request body retained? | The audit journal, as a `client_request` event |
| Where is the upstream body retained? | The audit journal, as an `llm_request` / `llm_response` event |
