# OpenAIx Protocol Specification (aidir)

## 1. Scope and compatibility

OpenAIx in this project is a **hybrid endpoint** that supports:

1. Ollama-compatible chat API (`/api/chat`, `/api/tags`)
2. OpenAI-compatible chat API (`/v1/chat/completions`, `/v1/models`)
3. Ollama-compatible embeddings (`/api/embed`)
4. OpenAI-compatible embeddings (`/v1/embeddings`)

Important:

1. This is **not** a full implementation of OpenAI API or Ollama API.
2. It is an aidir-specific implementation with explicit extensions and limitations.

## 2. Endpoints (all alternatives)

Base URL (example): `http://127.0.0.1:21434`

1. `POST /api/chat` - Ollama-compatible chat endpoint (with OpenAIx extensions)
2. `POST /api/embed` - Ollama-compatible non-streaming embeddings
3. `GET /api/tags` - Ollama-compatible models listing
4. `POST /v1/chat/completions` - OpenAI-compatible chat endpoint (with OpenAIx extensions)
5. `POST /v1/embeddings` - OpenAI-compatible non-streaming embeddings
6. `GET /v1/models` - OpenAI-compatible models listing
7. `GET /api/providers/{provider}/models/{model}/queue-state` and `GET /v1/providers/{provider}/models/{model}/queue-state` - read-only queue state for a provider/model pair
8. `GET /health` - health check (`{"status":"ok"}`)
9. `GET /api/resources`, `GET /v1/resources`, `GET /api/resources/{resource_id}`, and `GET /v1/resources/{resource_id}` - aidir peer resource availability extension

## 3. Authentication and envid behavior

Authorization is optional. If `Authorization: Bearer <token>` is not provided, request is processed as anonymous.

If bearer token is provided:

1. Token must match `users.items[].token`, otherwise `401`.
2. If request has no `envid`, endpoint may auto-assign `users.items[].autoassign_envid`.
3. If `envid` is present (provided or auto-assigned), endpoint checks access against `users.items[].envids`.
4. If `envid` does not exist in registry, returns `400 INVALID_ENVID`.

### 3.1 Peer resource access

The resource endpoints are an aidir peer extension. Their access mode is
configured per OpenAIx endpoint:

```json5
"peer_resources": {
  "auth_mode": "disabled" // "disabled" or "endpoint"
}
```

`disabled` is the default and permits read-only resource requests without a
bearer token. `endpoint` requires a valid bearer token and applies the ordinary
environment authorization rules. Responses contain only resource ID, type,
limits, units, availability state, and optional explicitly whitelisted telemetry
readings; commands, credentials, consumers, sensor configuration, and internal
reservations are never exposed.

### 3.2 Peer resource response

`GET /v1/resources` and `GET /api/resources` return:

```json
{
  "object": "list",
  "protocol_version": 1,
  "data": [
    {
      "id": "gpu_0",
      "type": "cuda",
      "limits": {"VRAM": 22000},
      "units": {"VRAM": "MiB"},
      "availability": {
        "source": "command",
        "status": "ok",
        "available": {"VRAM": 22173},
        "observed_at": "2026-10-05T10:00:00+00:00",
        "error": null
      },
      "telemetry": {
        "sensors": [
          {
            "id": "gpu_temperature",
            "label": "GPU temperature",
            "unit": "C",
            "status": "ok",
            "value": 44,
            "updated_at": "2026-10-05T10:00:00+00:00",
            "error": null
          }
        ]
      }
    }
  ]
}
```

The single-resource variants return one item from `data`. A remote aidir
provider is detected by this extension at runtime. Definitive incompatible or
unauthorized responses are cached only until the local aidir process restarts;
timeouts and transport failures are retried.

## 4. Request schema: `POST /api/chat`

This endpoint accepts Ollama-like payload and OpenAIx extensions.

### 4.1 Required/primary fields

1. `model: string` - model id/name
2. `messages: array` - message history
3. `stream: boolean` - streaming mode (`false` by default)

### 4.2 `messages[]` item shape (accepted in practice)

The implementation does not hard-validate message shape, but the following forms are expected/used:

1. `role: string` (`system`, `user`, `assistant`, `tool`)
2. `content: string`
3. `name: string` (tool name for `tool` role)
4. `tool_call_id: string` (tool linkage)
5. `tool_calls: array` (assistant tool calls, OpenAI-like format)

### 4.3 Optional standard-ish generation fields

1. `tools: array`
2. `tool_choice: string|object`
3. `temperature: number`
4. `top_p: number`
5. `repeat_penalty: number`
6. `repetition_penalty: number`
7. `repeat_last_n: integer`
8. `num_predict: integer`
9. `max_tokens: number`
10. `seed: integer`
11. `presence_penalty: number`
12. `frequency_penalty: number`
13. `top_k: integer`
14. `min_p: number`
15. `stop: string|array`
16. `response_format: object`
17. `options: object` - forwarded to upstream Ollama `/api/chat` options

### 4.4 OpenAIx extensions (`/api/chat`)

1. `worker: string` - explicit worker override
2. `envid: string` - target environment id
3. `timeout: integer` - sets the queue and run timeout for this task and the timeout for each upstream LLM call
4. `queue_timeout: integer` - overrides only the queue timeout when used with `timeout`
5. `context_builder: object` - per-request context behavior override
6. `log: object`
7. `log.options.save_llm_request: boolean` - per-request LLM call logging override

Generation parameter handling:

1. OpenAIx accepts top-level `temperature`, `top_p`, `repeat_penalty`, `repetition_penalty`, `repeat_last_n`, `num_predict`, `max_tokens`, `seed`, `presence_penalty`, `frequency_penalty`, `top_k`, and `min_p`.
2. Before sending to Ollama, these fields are mapped into `options.temperature`, `options.top_p`, `options.repeat_penalty`, `options.repeat_last_n`, `options.num_predict`, `options.seed`, `options.presence_penalty`, `options.frequency_penalty`, `options.top_k`, and `options.min_p`.
3. If the caller already supplied the corresponding `options.*` value, `options.*` wins.
4. Worker config may define overrideable defaults under `workers.items.<worker_id>.generation_defaults` using either the OpenAI/OpenAIx aliases or the direct Ollama option names.
5. Provider model config may also define the same generation fields under `models.providers.<provider>.models[]`; these act as per-model defaults and are overridden by request values.
6. Provider model config may define `upstream_retry_count` and `error_retry_count` under `models.providers.<provider>.models[]`.
7. `upstream_retry_count` retries one outbound LLM HTTP call when the upstream fails with timeout/connect/HTTP/non-JSON errors; `error_retry_count` retries other worker-side exceptions for that same call.
8. For smart-routed models, retry policy is taken from the requested smart model config first, then falls back to the resolved concrete provider/model config.

### 4.5 Processing notes

1. Message history is truncated when too long: keeps all `system` + last non-system messages up to total window logic in worker.
2. Context chain is applied synchronously before model call:
   1. `context_builder`
   2. `context_add_internal_tools`
   3. `context_render_openclaw_style`
3. Internal tools can be auto-executed server-side (tool loop) if model requests tool calls that map to local workers.

### 4.6 Tools injection control (current behavior)

Tools injection is controlled by the context worker chain, mainly `context_add_internal_tools`.

Effective config priority (highest to lowest):

1. Per-request override:
   1. `context_builder.context_add_internal_tools.tools` in request payload
2. Source worker tools config (prepared by openaix):
   1. `workers.items.<source_worker>.tools`
3. Default config of worker `context_add_internal_tools`:
   1. `workers.items.context_add_internal_tools.tools`
4. Legacy compatibility payload fields:
   1. `worker_tools_config`
   2. `context_add_internal_tools.tools`

Additional auto-discovery:

1. `context_add_internal_tools` auto-includes tools exposed by loaded workers of type `BaseToolWorker`.
2. Auto-discovered tools are added only when the same tool name is not already explicitly configured.
3. Explicit config values have priority over discovered defaults.

How injected tools appear in model payload:

1. `task.context.tools` is rendered to OpenAI-style `tools[]` entries:
   1. `type: "function"`
   2. `function.name`
   3. `function.description`
   4. `function.parameters` (from `inputSchema`)
2. These tools are then sent to upstream via `payload.tools`.

Minimal config example (worker-level):

```json
{
  "workers": {
    "items": {
      "openaix": {
        "tools": {
          "echo_call": {
            "worker": "echo_agent",
            "description": "Echo input",
            "inputSchema": {
              "type": "object",
              "properties": {
                "message": {"type": "string"}
              }
            }
          }
        }
      }
    }
  }
}
```

Minimal per-request override example:

```json
{
  "context_builder": {
    "context_add_internal_tools": {
      "tools": {
        "echo_call": {
          "worker": "echo_agent"
        }
      }
    }
  }
}
```

## 5. Request schema: `POST /v1/chat/completions`

This endpoint accepts OpenAI-like chat payload, then maps it to internal Ollama-like payload.

### 5.1 Supported OpenAI fields

1. `model: string`
2. `messages: array`
3. `stream: boolean`
4. `tools: array`
5. `tool_choice: string|object`
6. `temperature: number`
7. `top_p: number`
8. `repeat_penalty: number`
9. `repetition_penalty: number`
10. `repeat_last_n: integer`
11. `num_predict: integer`
12. `max_tokens: number`
13. `seed: integer`
14. `presence_penalty: number`
15. `frequency_penalty: number`
16. `top_k: integer`
17. `min_p: number`
18. `stop: string|array`
19. `options: object` (OpenAIx extension passthrough)

### 5.2 OpenAIx extensions supported on `/v1/chat/completions`

1. `worker: string`
2. `envid: string`
3. `context_builder: object`
4. `log: object`
5. `log.options.save_llm_request: boolean`
6. `options: object`
7. `top_k: integer`
8. `min_p: number`
9. `timeout: integer`
10. `queue_timeout: integer`

### 5.3 Important limitations for `/v1/chat/completions`

1. Many OpenAI fields are not implemented (for example `n`, `logprobs`, etc.).
2. Unknown fields are ignored by the mapping layer.

## 6. Responses

## 6.0 Embeddings

`POST /api/embed` and `POST /v1/embeddings` create normal queued `Task_agent` work with `request_kind = "embed"`. They are non-streaming and skip context builders, internal tools, tool loops, and chat generation defaults.

Embedding-capable configured models must set `embedding: true`. The initial accepted input forms are a string and an array of strings. Token arrays and mixed arrays return `400`.

### Ollama-compatible request and response

`POST /api/embed` requires `model` and `input`. It forwards operation-safe Ollama fields such as `truncate`, `keep_alive`, `dimensions`, and `options` to the upstream `/api/embed` route. A successful result preserves the upstream Ollama embedding payload, including `embeddings` and available timing/token fields.

### OpenAI-compatible request and response

`POST /v1/embeddings` requires `model` and `input`. `dimensions` is forwarded to the upstream model; an upstream rejection is returned as an error. `encoding_format` supports `float` (default) and `base64`; the latter encodes the returned float vector as little-endian float32 bytes. `user` is retained in task metadata and is not sent to Ollama, which has no equivalent field.

The response is an OpenAI embedding list with ordered `data[]` entries. `usage.prompt_tokens` and `usage.total_tokens` are included only when upstream counters are available.

## 6.1 `POST /api/chat` non-stream response

Returned payload is upstream Ollama-like JSON (`message`, `done`, timings, token counters, etc.).

Typical fields:

1. `model: string`
2. `created_at: string`
3. `message: object`
4. `message.role: string`
5. `message.content: string`
6. `message.thinking: string` (can appear depending on model)
7. `message.tool_calls: array` (possible)
8. `done: boolean`
9. `done_reason: string`
10. `prompt_eval_count: integer`
11. `eval_count: integer`
12. duration fields (`total_duration`, `load_duration`, etc.)

Special behavior:

1. When client and executor protocols match, direct chat responses preserve the original body and content type, including unknown fields and empty/null content. Reasoning is not copied into `content`.
2. After aidir actually executes an internal tool, intermediate turns stay internal and reasoning fields are removed from the final answer. Merely declaring caller-owned tools does not trigger this rule.
3. When protocols differ, response envelopes are converted with minimal loss: reasoning, detailed usage, original finish reasons, and compatible extra fields are preserved. `thinking` maps to OpenAI `reasoning_content`; OpenAI reasoning maps to Ollama `thinking` without discarding its original fields.

## 6.2 `POST /api/chat` stream response

1. Content type: `application/x-ndjson`
2. Body: newline-delimited JSON chunks from upstream
3. Final chunk has `done: true`
4. Matching Ollama streams retain original event bytes, line delimiters, content type, and additional fields.
5. Aidir-managed tool loops may synthesize streaming from the final synchronous response, omitting reasoning only if an internal tool executed.

## 6.3 `POST /v1/chat/completions` non-stream response

For an OpenAI executor such as llama.cpp, direct responses are returned unchanged, preserving all choices, identifiers, timestamps, reasoning, usage details, and vendor fields. The shape below describes conversion from an Ollama executor; existing compatible fields are retained rather than replaced:

1. `id: original identifier, or "chatcmpl-<task_id>" when absent`
2. `object: "chat.completion"`
3. `created: original timestamp, or current unix_ts when absent`
4. `model: string`
5. `choices[0].index: 0`
6. `choices[0].message.role: original role, or "assistant" when absent`
7. `choices[0].message.content: string`
8. `choices[0].finish_reason: original done_reason, or "tool_calls"/"stop" when absent`
9. `usage` (optional):
   1. `prompt_tokens`
   2. `completion_tokens`
   3. `total_tokens`

Usage is built from Ollama counters:

1. `prompt_tokens <- prompt_eval_count`
2. `completion_tokens <- eval_count`

Existing OpenAI usage details are preserved. Unknown message fields and compatible top-level fields remain available as extensions.

## 6.4 `POST /v1/chat/completions` stream response

1. Content type: `text/event-stream`
2. Chunks format: `data: {json}\n\n`
3. Final marker: `data: [DONE]\n\n`

Matching OpenAI streams retain original SSE events, event metadata/comments, line delimiters, all choices and deltas, reasoning, usage-only chunks, and executor terminators. Aidir does not append a duplicate terminator. The shape below describes converted or synthesized streams:

1. `id: original identifier, or "chatcmpl-<task_id>" when absent`
2. `object: "chat.completion.chunk"`
3. `created: original timestamp, or current unix_ts when absent`
4. `model: string`
5. `choices[0].index: 0`
6. `choices[0].delta.content: string` (when present)
7. `choices[0].delta.reasoning_content` (when the source provides reasoning)
8. `choices[0].finish_reason: null or original done_reason, with "tool_calls"/"stop" as fallback`
9. Additional compatible fields and detailed usage are preserved.

## 6.5 Models listing

### `GET /v1/models`

OpenAI-like:

1. `object: "list"`
2. `data[]` items:
   1. `id`
  2. `real_id` - internal non-alias model id selected by current endpoint routing rules
  3. `object: "model"`
  4. `created`
  5. `owned_by: "aidir"`

`id` is always the externally callable model name: `alias` when configured, otherwise `id`.

### `GET /api/tags`

Ollama-like:

1. `models[]` items:
   1. `name`
   2. `model`
  3. `real_id` - internal non-alias model id selected by current endpoint routing rules
  4. `modified_at`
  5. `size` (0)
  6. `digest` (empty)
  7. `details` (object)

`name` and `model` are always the externally callable model name: `alias` when configured, otherwise `id`.

  ## 6.6 Queue state

  ### `GET /v1/providers/{provider}/models/{model}/queue-state`

  Also exposed as `GET /api/providers/{provider}/models/{model}/queue-state`.

  This endpoint returns the current queue state for the resource requirements of the selected provider/model pair.

  Path parameters:

  1. `provider` - provider id from `models.providers`
  2. `model` - model `id` or `name` from that provider

  Query parameters:

  1. `priority: integer` - optional, defaults to `5`

  Semantics:

  1. The model is resolved from `models.providers.<provider>.models[]`.
  2. Queue counts include queued tasks whose serialized `resource_requirements` exactly match the resolved model resource requirements.
  3. `can_run_now` is `true` only when the target resources are currently available and there are no queued tasks with priority equal to or higher than the requested one.
  4. Lower numeric value means higher priority, same as task queue ordering.

  Response fields:

  1. `provider`
  2. `model`
  3. `priority`
  4. `can_run_now`
  5. `queued_count_below_priority` - queued tasks for this resource with priority numerically greater than the requested one
  6. `queued_count_total` - total queued tasks for this resource
  7. `priority_counts` - sorted list of `{priority, count}` objects for all queued tasks on this resource

## 7. Error format

Executor errors retained by `call_llama_cpp` bypass the envelopes below: endpoints return the original HTTP status, complete body, and content type, preserving all upstream fields and adding no `task_id`. Streaming requests wait for their first output so an initial rejection can retain its HTTP status. If an error occurs after streaming starts, the original JSON error is emitted as an SSE (`/v1/*`) or NDJSON (`/api/*`) event; the HTTP status remains unchanged. This passthrough is independent of `errors_compatibility_mode`. Locally generated errors, including connection failures and timeouts, retain the behavior below.

Endpoint has compatibility mode (`errors_compatibility_mode`, default `true`).

When compatibility mode is enabled:

1. OpenAI routes (`/v1/*`) return:
   1. `{"error":{"message":"...","type":"...","code":"...","task_id":"..."}}`
2. Ollama routes (`/api/*`) return:
   1. `{"error":{"code":"...","message":"...","task_id":"..."}}`

When compatibility mode is disabled, unified internal envelope is used:

1. `{"error":{"code":"...","message":"...","task_id":"..."}}`

## 8. OpenAI compatibility notes

Compared to standard OpenAI Chat Completions API:

1. This implementation supports only a subset of fields.
2. Request extensions (`worker`, `envid`, `context_builder`, `log`) are non-standard.
3. Internal tool execution loop is non-standard server-side behavior.
4. Matching-protocol direct responses and stream events are transparent; protocol conversion cannot provide byte-identical envelopes.
5. Internal tool loops hide intermediate turns and omit final-stage reasoning after a tool executes. Their client stream may be synthesized from a final synchronous executor response.

## 9. Short list of OpenAIx extensions

1. `worker` request field for explicit worker routing
2. `envid` request field with user-scoped access control
3. `context_builder` per-request context pipeline overrides
4. `log.options.save_llm_request` per-request call logging control
5. `/api/chat` and `/v1/chat/completions` support for `timeout` (queue/run and upstream-call timeout override)
6. Built-in server-side internal tool execution loop
7. Optional protocol-specific error envelope mode (`errors_compatibility_mode`)

## 10. cURL examples

`BASE=http://127.0.0.1:21434`

### 10.1 Health

```bash
curl -s "$BASE/health"
```

### 10.2 Ollama-compatible chat (`/api/chat`, non-stream)

```bash
curl -s "$BASE/api/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3",
    "messages": [
      {"role": "user", "content": "Say hello"}
    ],
    "stream": false
  }'
```

### 10.3 Ollama-compatible chat with OpenAIx extensions

```bash
curl -s "$BASE/api/chat" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -d '{
    "worker": "openaix",
    "envid": "dev",
    "context_builder": {
      "context_add_internal_tools": {
        "tools": {
          "echo_call": {
            "worker": "echo_agent"
          }
        }
      }
    },
    "log": {"options": {"save_llm_request": true}},
    "model": "qwen3",
    "messages": [
      {"role": "user", "content": "Use a tool if needed"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "echo_call",
          "description": "Echo input",
          "parameters": {
            "type": "object",
            "properties": {
              "message": {"type": "string"}
            }
          }
        }
      }
    ],
    "stream": false
  }'
```

### 10.4 OpenAI-compatible chat (`/v1/chat/completions`, non-stream)

```bash
curl -s "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3",
    "messages": [
      {"role": "user", "content": "What is 2+2?"}
    ],
    "stream": false
  }'
```

### 10.5 OpenAI-compatible chat with OpenAIx extensions

```bash
curl -s "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -d '{
    "worker": "openaix",
    "envid": "dev",
    "context_builder": {
      "context_add_internal_tools": {
        "tools": {
          "echo_call": {
            "worker": "echo_agent"
          }
        }
      }
    },
    "log": {"options": {"save_llm_request": true}},
    "model": "qwen3",
    "messages": [
      {"role": "user", "content": "Summarize this in one line"}
    ],
    "stream": false
  }'
```

### 10.6 Streaming (`/v1/chat/completions`)

```bash
curl -N "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3",
    "messages": [
      {"role": "user", "content": "Count from 1 to 5"}
    ],
    "stream": true
  }'
```

### 10.7 Models listing

```bash
curl -s "$BASE/v1/models"
curl -s "$BASE/api/tags"
```

### 10.8 Queue state for a provider/model

```bash
curl -s "$BASE/v1/providers/ollama_local/models/qwen3.5:9b/queue-state?priority=5"
curl -s "$BASE/api/providers/ollama_local/models/qwen3.5:9b/queue-state?priority=5"
```
