# OpenAIx Protocol Specification (aidir)

## 1. Scope and compatibility

OpenAIx in this project is a **hybrid endpoint** that supports:

1. Ollama-compatible chat API (`/api/chat`, `/api/tags`)
2. OpenAI-compatible chat API (`/v1/chat/completions`, `/v1/models`)

Important:

1. This is **not** a full implementation of OpenAI API or Ollama API.
2. It is an aidir-specific implementation with explicit extensions and limitations.

## 2. Endpoints (all alternatives)

Base URL (example): `http://127.0.0.1:21434`

1. `POST /api/chat` - Ollama-compatible chat endpoint (with OpenAIx extensions)
2. `GET /api/tags` - Ollama-compatible models listing
3. `POST /v1/chat/completions` - OpenAI-compatible chat endpoint (with OpenAIx extensions)
4. `GET /v1/models` - OpenAI-compatible models listing
5. `GET /api/providers/{provider}/models/{model}/queue-state` and `GET /v1/providers/{provider}/models/{model}/queue-state` - read-only queue state for a provider/model pair
6. `GET /health` - health check (`{"status":"ok"}`)

## 3. Authentication and envid behavior

Authorization is optional. If `Authorization: Bearer <token>` is not provided, request is processed as anonymous.

If bearer token is provided:

1. Token must match `users.items[].token`, otherwise `401`.
2. If request has no `envid`, endpoint may auto-assign `users.items[].autoassign_envid`.
3. If `envid` is present (provided or auto-assigned), endpoint checks access against `users.items[].envids`.
4. If `envid` does not exist in registry, returns `400 INVALID_ENVID`.

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
5. `max_tokens: number`
6. `stop: string|array`
7. `response_format: object`
8. `seed: integer`
9. `options: object` - forwarded to upstream Ollama `/api/chat` options

### 4.4 OpenAIx extensions (`/api/chat`)

1. `worker: string` - explicit worker override
2. `envid: string` - target environment id
3. `timeout: integer` - sets both queue and run timeout for this task
4. `context_builder: object` - per-request context behavior override
5. `log: object`
6. `log.options.save_llm_request: boolean` - per-request LLM call logging override

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
8. `max_tokens: number`
9. `stop: string|array`

### 5.2 OpenAIx extensions supported on `/v1/chat/completions`

1. `worker: string`
2. `envid: string`
3. `context_builder: object`
4. `log: object`
5. `log.options.save_llm_request: boolean`

### 5.3 Important limitations for `/v1/chat/completions`

1. `timeout` is not mapped from OpenAI request body in current implementation.
2. Many OpenAI fields are not implemented (for example `n`, `presence_penalty`, `frequency_penalty`, `logprobs`, etc.).
3. Unknown fields are ignored by the mapping layer.

## 6. Responses

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

1. If final assistant `content` is empty and `thinking` is non-empty, worker may substitute `content = thinking`.

## 6.2 `POST /api/chat` stream response

1. Content type: `application/x-ndjson`
2. Body: newline-delimited JSON chunks from upstream
3. Final chunk has `done: true`

## 6.3 `POST /v1/chat/completions` non-stream response

Mapped to OpenAI `chat.completion` shape:

1. `id: "chatcmpl-<task_id>"`
2. `object: "chat.completion"`
3. `created: unix_ts`
4. `model: string`
5. `choices[0].index: 0`
6. `choices[0].message.role: "assistant"`
7. `choices[0].message.content: string`
8. `choices[0].finish_reason: "stop"`
9. `usage` (optional):
   1. `prompt_tokens`
   2. `completion_tokens`
   3. `total_tokens`

Usage is built from Ollama counters:

1. `prompt_tokens <- prompt_eval_count`
2. `completion_tokens <- eval_count`

## 6.4 `POST /v1/chat/completions` stream response

1. Content type: `text/event-stream`
2. Chunks format: `data: {json}\n\n`
3. Final marker: `data: [DONE]\n\n`

Chunk JSON shape:

1. `id: "chatcmpl-<task_id>"`
2. `object: "chat.completion.chunk"`
3. `created: unix_ts`
4. `model: string`
5. `choices[0].index: 0`
6. `choices[0].delta.content: string` (when present)
7. `choices[0].finish_reason: null|"stop"`

## 6.5 Models listing

### `GET /v1/models`

OpenAI-like:

1. `object: "list"`
2. `data[]` items:
   1. `id`
   2. `object: "model"`
   3. `created`
   4. `owned_by: "aidir"`

### `GET /api/tags`

Ollama-like:

1. `models[]` items:
   1. `name`
   2. `model`
   3. `modified_at`
   4. `size` (0)
   5. `digest` (empty)
   6. `details` (object)

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
4. Streaming chunks do not include full OpenAI delta semantics (for example role deltas).
5. Non-stream OpenAI response is normalized to a single `choices[0]`.

## 9. Short list of OpenAIx extensions

1. `worker` request field for explicit worker routing
2. `envid` request field with user-scoped access control
3. `context_builder` per-request context pipeline overrides
4. `log.options.save_llm_request` per-request call logging control
5. `/api/chat` support for `timeout` (task queue/run timeout override)
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
    "timeout": 120,
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
