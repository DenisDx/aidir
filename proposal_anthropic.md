# Proposal: Anthropic Messages Protocol Support

## Decision

Add first-class support for the Anthropic Messages protocol in both directions:

1. A new inbound endpoint with `api: "anthropic"` serves `POST /v1/messages`,
   `GET /v1/models`, and the initial `POST /v1/messages/count_tokens`
   compatibility route.
2. A new outbound provider API `api: "anthropic"` reaches Anthropic-compatible
   servers through `POST {baseUrl}/v1/messages`.
3. A per-model `protocol` field lets a mixed provider route individual models
   through Messages. This is required for OpenCode Go, whose Qwen, MiniMax, and
   Claude models use the Anthropic path while GLM, Kimi, DeepSeek, and MiMo use
   Chat Completions.
4. The Messages conversion lives in a pure, separately testable module. The
   endpoint and workers only move data between that module, the task queue, and
   HTTP.
5. Protocol selection is always explicit configuration. It is never inferred
   from a model name, an upstream error, or a successful probe.

The internal canonical chat representation remains the OpenAI Chat Completions
message shape already used by `Task_agent`, context builders, injected tools, and
`call_openai`. Worker results remain in the existing Ollama-shaped result format.
The Messages module converts between those shapes and Anthropic Messages.

## Current State and Evidence

- `core/app.py:731` starts only `ollama`, `openaix`, and `mcp` endpoints; every
  other `api` value is skipped with a warning.
- `config.descriptor.json5:603` restricts endpoint APIs to
  `["openaix", "ollama", "mcp"]`, and `config.descriptor.json5:776` restricts
  provider APIs to
  `["ollama", "openaix", "openai", "opencode-go", "llama-cpp", "smart"]`.
- `core/endpoints/endpoint_openaix.py` exposes `/v1/chat/completions`,
  `/api/chat`, embeddings, and model discovery. It does not expose
  `/v1/messages`.
- `workers/agent/call_openai/app.py:46` hardcodes
  `{baseUrl}/v1/chat/completions`.
- `proposal_opencode_go.md` explicitly excludes Anthropic Messages and Responses
  API from its scope and states that a future named adapter may add them.
- OpenCode Go documents that not every model uses Chat Completions. Qwen,
  MiniMax, and Claude families are served by the Anthropic Messages path, for
  example `https://opencode.ai/zen/go/v1/messages`; GLM, Kimi, DeepSeek, and
  MiMo are served by `/v1/chat/completions`.
- The WebUI already offers `protocol: "anthropic"`
  (`webui/frontend/index.html:952`), and the backend sends it to
  `http://127.0.0.1:{port}/v1/messages`
  (`webui/backend/app.py:1471`). No endpoint implements that path, so the option
  currently produces a 404.

## Goals

- Anthropic SDK clients and coding agents can use aidir as an Anthropic server.
- OpenAI and Ollama clients can use models that only speak Anthropic Messages.
- Anthropic clients can use Chat Completions and Ollama providers.
- OpenCode Go models are routed to the endpoint each model actually supports.
- Synchronous and streaming responses preserve native Anthropic bytes when no
  conversion is required.
- Tool use, tool results, system prompts, images, usage, stop reasons, and errors
  survive conversion in both directions.

## Non-Goals

The initial release does not implement:

- the OpenAI Responses API;
- Anthropic Batches, Files, Models administration, or organization APIs;
- assistant-side management of prompt caching (`cache_control` is accepted and
  preserved only on an unchanged native path);
- exact token counting for every model; `count_tokens` is explicitly
  approximate in phase 1 and may be replaced by a provider call later;
- automatic protocol discovery from model ids;
- embeddings through the Messages endpoint.

## External Contract

### Inbound

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/messages` | Synchronous or SSE Messages request |
| `GET` | `/v1/models` | Anthropic-shaped model list |
| `POST` | `/v1/messages/count_tokens` | Best-effort token estimate |
| `GET` | `/health` | Existing endpoint health convention |

The endpoint accepts the standard Anthropic headers:

- `x-api-key` and/or `Authorization: Bearer` for aidir API-user
  authentication;
- `anthropic-version`; an absent value is treated as `2023-06-01` for
  compatibility, and unsupported future versions are accepted but logged once;
- `anthropic-beta` is accepted and ignored in phase 1. An optional allowlist can
  forward selected beta headers later.

Every response includes `request-id` and `x-request-id` when a request id is
available, matching the existing request-id middleware.

### Outbound

| Method | Path | Purpose |
|---|---|---|
| `POST` | `{baseUrl}/v1/messages` | Messages inference |
| `GET` | `{baseUrl}/v1/models` | Session-free availability probe |

`baseUrl` follows the existing provider convention: it is the API root without
`/v1`. A legacy trailing `/v1` is normalized exactly as `call_opencode_go` does
today.

Outbound headers:

- `content-type: application/json`;
- `anthropic-version: 2023-06-01` unless the provider overrides it;
- `x-api-key: <key>` for `auth.type: "x-api-key"`;
- `Authorization: Bearer <token>` for `auth.type: "bearer"`;
- configured static headers from `auth.headers`;
- for OpenCode Go Messages models: the existing coding-agent `User-Agent`,
  `x-opencode-session`, and the OpenCode authentication headers described
  below.

The exact OpenCode Go authentication combination must be verified in phase 0.
Public documentation currently shows both `Authorization: Bearer` for the
inference API and `x-api-key` plus `anthropic-version` for Anthropic-compatible
gateway paths. Until the live fixture settles it, the OpenCode Go Messages
sender may send both headers with the same credential; the Chat Completions path
must not gain Anthropic-only headers.

## Internal Representation

`Task_agent.payload` remains an OpenAI Chat Completions-shaped request:

- `messages`: OpenAI roles and content parts;
- `tools`: OpenAI function tools;
- `tool_choice`: OpenAI tool-choice shape;
- `max_completion_tokens` / `num_predict`, temperature, `top_p`, `top_k`,
  `stop`, and other generation fields;
- `thinking` as the internal request-level reasoning extension.

This is already the shape consumed by the context chain, internal tool loop, and
`call_openai`. `call_anthropic` converts it to Messages on each upstream turn,
and the endpoint converts the resulting Ollama-shaped worker data back to
Messages for the client.

### Request Conversion: Messages to Internal

| Messages request | Internal request |
|---|---|
| `model` | `model`, resolved through the existing alias-aware router |
| `system` string or text blocks | one leading `system` message; text blocks are joined with blank lines |
| user/assistant `messages` | same roles |
| string content | string content |
| `text` block | OpenAI `{"type": "text", "text": ...}` part |
| `image` block with `base64`, `url`, or `file` source | OpenAI `image_url`; base64 becomes a data URL, URL is passed through, file ids are rejected in phase 1 |
| user `tool_result` block | `role: "tool"` message with `tool_call_id`; `is_error` is kept as internal metadata |
| assistant `tool_use` block | assistant `tool_calls` entry with JSON-encoded `arguments` |
| assistant `thinking` / `redacted_thinking` block | `reasoning_content` plus an opaque `anthropic_blocks` extension |
| `max_tokens` | `max_completion_tokens` (mapped to `num_predict` by existing workers) |
| `stop_sequences` | `stop` |
| `temperature`, `top_p`, `top_k` | same generation options |
| `tools` | OpenAI function tools |
| `tool_choice` | `auto`, `required`, `none`, or one named function |
| `thinking` | internal `thinking` request extension |
| `stream` | `task.stream` |
| `metadata`, `service_tier`, `context_management`, `container` | dropped; not silently forwarded as prompt content |

A user message containing both `tool_result` blocks and ordinary text is split
into tool messages followed by a user message, because OpenAI-style history
requires tool results immediately after the assistant tool call.

### Response Conversion: Internal to Messages

The endpoint returns the original Anthropic response when
`task.upstream_response` has protocol `anthropic` and the route was not modified
by an incompatible conversion. Otherwise it builds:

```json
{
  "id": "msg_<task or upstream id>",
  "type": "message",
  "role": "assistant",
  "model": "<external model alias>",
  "content": [
    {"type": "text", "text": "..."},
    {"type": "tool_use", "id": "toolu_...", "name": "...", "input": {}}
  ],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {"input_tokens": 0, "output_tokens": 0}
}
```

Finish reasons map as follows:

| Internal | Anthropic `stop_reason` |
|---|---|
| `stop` | `end_turn` |
| `length` | `max_tokens` |
| `tool_calls` | `tool_use` |
| content filter / refusal | `refusal` |
| unknown | `end_turn` |

Usage maps `prompt_tokens` to `input_tokens` and `completion_tokens` to
`output_tokens`. Cache fields (`cache_creation_input_tokens`,
`cache_read_input_tokens`) are preserved when the upstream supplied them.

### Streaming

The module implements two explicit state machines:

1. **Encoder** — internal Ollama-shaped chunks to Anthropic SSE:
   `message_start`, `content_block_start`, `content_block_delta`,
   `content_block_stop`, `message_delta`, `message_stop`, and `error`.
   Text deltas become `text_delta`; tool-call argument deltas become
   `input_json_delta`; reasoning deltas become `thinking_delta` when the client
   requested thinking.
2. **Decoder** — Anthropic SSE events to internal chunks. It accumulates
   `tool_use` input JSON until `content_block_stop`, tracks `message_delta`
   usage and stop reason, and turns `error` events into explicit worker
   failures.

Native Anthropic SSE is passed through byte-for-byte when both the client and
the upstream speak Anthropic and no internal tool loop or context rewrite
requires resynthesis. The existing `UpstreamChunk(protocol="anthropic")`
mechanism carries those bytes through the task queue and audit path.

### Thinking and Opaque Blocks

Thinking signatures are model-issued opaque values. Losing one breaks a later
tool-use turn on Anthropic.

Initial behavior:

- Assistant messages carry an internal `anthropic_blocks` extension containing
  the original content blocks and signatures.
- When the context chain does not modify an assistant message, `call_anthropic`
  replays those blocks unchanged, including `thinking` and
  `redacted_thinking`.
- When a context builder, trim, or conversion changes the message, the
  extension is invalidated and the signature is dropped with a warning.
- Non-Anthropic targets strip `anthropic_blocks` before serialization.
- `cache_control` markers are kept only inside unchanged opaque blocks. aidir
  does not add or move cache breakpoints.

This is enough to keep ordinary multi-turn tool use working. Full preservation
after context rewriting is a follow-up.

## Components

### New: `core/messages_protocol.py`

Pure conversion functions with no HTTP, Redis, or FastAPI dependencies:

- `messages_request_to_internal(body) -> dict`
- `internal_result_to_messages(result, *, model, task_id) -> dict`
- `internal_chunk_to_messages_events(chunk, state) -> list[dict]`
- `messages_event_to_internal_chunk(event, state) -> dict | None`
- `messages_tools_to_internal`, `internal_tools_to_messages`
- `messages_tool_choice_to_internal`, `internal_tool_choice_to_messages`
- `messages_error_payload`, `internal_error_to_messages`
- `estimate_input_tokens(body) -> int` for `count_tokens`

The module also owns the stop-reason and usage mapping tables, so endpoints and
workers cannot drift apart.

### New: `core/endpoints/endpoint_anthropic.py`

`Endpoint_anthropic(BaseEndpoint)` or a subclass of `Endpoint_ollama` that reuses
the existing authentication, model-alias route resolution, smart routing, task
creation, timeout, audit, and OpenCode session metadata code.

Routes:

- `POST /v1/messages`
- `POST /v1/messages/count_tokens`
- `GET /v1/models`
- `GET /health`

The request flow mirrors `_handle_openai_chat`:

1. Read and validate JSON.
2. Authenticate `x-api-key` or Bearer and apply `envid`.
3. Convert Messages to the internal request.
4. Resolve model/provider/worker and smart routes.
5. Attach OpenCode session metadata when the resolved provider needs it.
6. Enqueue the task.
7. Return a native, converted, or streamed Messages response.

Errors use the Anthropic envelope:

```json
{
  "type": "error",
  "error": {"type": "invalid_request_error", "message": "..."}
}
```

### New: `workers/agent/call_anthropic/app.py`

`CallAnthropicWorker` reuses `CallOpenAIWorker`'s context chain, retry policy,
audit/call-log integration, timeout handling, and internal tool loop. The
transport hooks change:

- target URL: `{baseUrl}/v1/messages`;
- request conversion: internal OpenAI shape to Messages;
- headers: Anthropic version and configured authentication;
- response conversion: Messages to Ollama-shaped internal result;
- streaming: Anthropic SSE decoder;
- transparent native response and chunk capture with protocol `anthropic`.

To avoid copying `execute`, phase 1 extracts small hooks from
`CallOpenAIWorker`:

- `_chat_url(provider_id) -> str`
- `_prepare_upstream_payload(payload, stream) -> dict`
- `_response_to_internal(data, streaming) -> dict`

Defaults keep the current Chat Completions behavior; `call_anthropic` overrides
them. `call_llama_cpp` and `call_opencode_go` continue to inherit the same base.

### Modified: `workers/agent/call_opencode_go/app.py`

The worker selects the transport from the resolved model protocol:

- `chat-completions` -> existing OpenAI path and `/v1/chat/completions`;
- `anthropic-messages` -> Messages path and `/v1/messages`.

Both paths use the same resolved OpenCode session value, `User-Agent`, base-URL
normalization, and credential. Session creation and deletion stay owned by the
endpoint metadata plus `OpenCodeSessionStore`; no per-model session logic is
added.

## Configuration

### Endpoint

```json5
{
  "endpoints": [
    {
      "id": "anthropic",
      "api": "anthropic",
      "port": "${ANTHROPIC_ENDPOINT_PORT:-21435}",
      "bindAddress": "${ANTHROPIC_ENDPOINT_HOST:-0.0.0.0}",
      "worker": "openaix"
    }
  ]
}
```

The endpoint-level worker is only the fallback used before model routing, as it
is today. The resolved provider selects the actual worker.

### Generic Anthropic Provider

```json5
{
  "models": {
    "providers": {
      "anthropic_cloud": {
        "api": "anthropic",
        "baseUrl": "https://api.anthropic.com",
        "anthropic_version": "2023-06-01",
        "auth": {
          "type": "x-api-key",
          "key": "${ANTHROPIC_API_KEY}"
        },
        "models": [
          {
            "id": "claude-sonnet-4-6",
            "alias": "claude",
            "max_output_tokens": 8192
          }
        ]
      }
    }
  },
  "workers": {
    "items": {
      "call_anthropic": {
        "enabled": true,
        "request_timeout": "${REQUEST_TIMEOUT:-100}"
      }
    }
  }
}
```

### OpenCode Go Mixed Provider

```json5
{
  "models": {
    "providers": {
      "opencode_go": {
        "api": "opencode-go",
        "baseUrl": "${OPENCODE_GO_BASE_URL:-https://opencode.ai/zen/go}",
        "auth": {
          "type": "bearer",
          "token": "${OPENCODE_GO_API_KEY}"
        },
        "opencode_session": {
          "ttl_seconds": 86400,
          "anonymous_ttl_seconds": 3600
        },
        "models": [
          {"id": "glm-5.3-flash", "alias": "opencode-glm-5.3-flash"},
          {"id": "kimi-k3", "alias": "opencode-kimi-k3"},
          {
            "id": "minimax-m3",
            "alias": "opencode-minimax-m3",
            "protocol": "anthropic-messages"
          },
          {
            "id": "qwen3.8-max",
            "alias": "opencode-qwen3.8-max",
            "protocol": "anthropic-messages"
          }
        ]
      }
    }
  }
}
```

`protocol` values are `chat-completions` and `anthropic-messages`. When omitted,
the provider `api` supplies the default. A model-level value overrides the
provider default for both direct and smart routes. The field is not a model
name heuristic; it must be declared.

### Descriptor Changes

- `endpoints.*.api` enum gains `"anthropic"`.
- `models.providers.*.api` enum gains `"anthropic"`.
- `models.providers.*.auth.type` enum gains `"x-api-key"`.
- `models.providers.*.anthropic_version` and `anthropic_beta` are declared.
- `models.providers.*.models[].protocol` enum is added and inherits the provider
  default when absent.
- `models.providers.*.models[].max_output_tokens` is added for upstreams that
  require a `max_tokens` value when the client omitted one.
- `webui.agent_test` continues to allow only its supported protocols, but the
  tester must send a valid Messages body.

The shipped examples add one Anthropic endpoint, one generic provider stub, and
the Messages-protocol OpenCode Go model entries. Secrets remain `${...}`
placeholders and are added to `.env.example`.

## Routing

`_resolve_model_route` gains `resolved_protocol`:

```text
provider api      model protocol          worker
opencode-go       chat-completions        call_opencode_go
opencode-go       anthropic-messages      call_opencode_go
openai            chat-completions        call_openai
anthropic         anthropic-messages      call_anthropic
ollama            n/a                     call_ollama
openaix           n/a                     openaix
llama-cpp         n/a                     call_llama_cpp
smart             resolved candidate      candidate worker
```

The route metadata keeps the selected protocol so a queued task, retry, audit
record, and redacted call log agree about which transport was used.

Smart routing:

- an `anthropic` provider is probed with `GET {baseUrl}/v1/models` and the
  configured Anthropic auth headers;
- an `opencode-go` provider keeps its existing session-free `/v1/models` probe
  for both Chat Completions and Messages models;
- a failed probe does not change the configured protocol;
- a `smart` alias may mix workers; the selected candidate supplies its own
  protocol and worker.

## Authentication and Security

- Inbound authentication checks `x-api-key` first, then `Authorization: Bearer`.
  Both resolve against existing `users.items[].token` values with constant-time
  comparison.
- The inbound key is aidir's user credential. It is never forwarded to an
  upstream provider merely because it was supplied as `x-api-key`.
- Provider credentials come only from `models.providers.*.auth`.
- Audit events, task payloads, call logs, and ordinary logs redact
  `x-api-key`, `Authorization`, `x-opencode-session`, and the raw identity
  values behind managed sessions.
- The `anthropic-version`, `anthropic-beta`, and OpenCode headers are metadata,
  not prompt content, and never appear in the transcript.
- Request-size limits, audit rejection records, request ids, and the existing
  per-endpoint timeout semantics apply unchanged.
- `count_tokens` uses only the request body and does not call a model; it cannot
  be used to bypass queue limits because it does not create a task.

## Compatibility Matrix

| Client protocol | Upstream protocol | Behavior |
|---|---|---|
| Anthropic | Anthropic | Native response and SSE passthrough when unchanged; converted otherwise |
| Anthropic | OpenAI Chat Completions | Messages converted to internal; response and SSE synthesized as Anthropic |
| Anthropic | Ollama | Messages converted to internal; `openaix` worker converts to Ollama |
| OpenAI | Anthropic | Existing OpenAI endpoint creates internal task; `call_anthropic` sends Messages; response converted back |
| Ollama | Anthropic | Existing Ollama endpoint creates internal task; `call_anthropic` sends Messages; response converted back |
| Any | OpenCode Go Messages model | Model `protocol` selects `call_opencode_go` Messages transport |

## Error Mapping

| Condition | HTTP | Anthropic error type |
|---|---|---|
| Invalid JSON/body/field | 400 | `invalid_request_error` |
| Missing or invalid API key | 401 | `authentication_error` |
| `envid` not allowed | 403 | `permission_error` |
| Unknown model | 404 | `not_found_error` |
| Embedding model requested for chat | 422 | `invalid_request_error` |
| Queue/worker unavailable | 503 | `overloaded_error` |
| Task or upstream timeout | 504 | `timeout_error` |
| Upstream 429 | 429 | `rate_limit_error` |
| Upstream 5xx or unknown worker failure | 502 | `api_error` |
| Anthropic upstream 529 | 529 | `overloaded_error` |

Native Anthropic upstream errors are returned byte-for-byte when the client also
speaks Anthropic. Cross-protocol clients receive the target protocol's envelope,
including OpenAI's `error.type`/`error.code` and Ollama's
`{"error": {"code", "message"}}` shape.

## Testing

Add focused suites rather than extending the large transparency test with
protocol-specific cases:

- `test_anthropic_protocol.py` — pure conversions:
  - system string and block forms;
  - text, image, tool_use, tool_result, thinking, redacted_thinking;
  - consecutive tool results and mixed text/tool-result user turns;
  - tools and tool_choice both directions;
  - `max_tokens`, stop sequences, generation options;
  - usage, cache fields, stop reasons, error envelopes;
  - stream encoder state sequence and decoder partial-block handling;
  - invalid input rejected before task creation.
- `test_anthropic_endpoint.py`:
  - `POST /v1/messages` sync and stream through a fake worker;
  - `x-api-key` and Bearer authentication, invalid tokens, `envid` policy;
  - `GET /v1/models` Anthropic shape and alias resolution;
  - `count_tokens` estimate and no task creation;
  - native response bytes and fragmented SSE preserved;
  - request-id, audit rejection, body-size limit;
  - timeout, queue failure, worker failure, upstream error mapping.
- `test_anthropic_worker.py`:
  - fake Anthropic upstream sync and SSE fixtures;
  - OpenCode Go Messages headers: `User-Agent`, `x-opencode-session`,
    `anthropic-version`, and the verified credential combination;
  - session-free `/v1/models` probe never creates a session;
  - caller-supplied session is forwarded and deleted after the task;
  - internal tool loop over a Messages upstream;
  - retries and transparent error preservation.
- Routing tests:
  - direct `anthropic` provider selects `call_anthropic`;
  - direct OpenCode Go `protocol: anthropic-messages` selects
    `call_opencode_go` and the `/v1/messages` URL;
  - smart alias with mixed protocols selects the correct candidate worker;
  - invalid `protocol`, missing `baseUrl`, missing auth, and invalid
    `anthropic_version` fail configuration validation.
- Cross-protocol tests:
  - OpenAI endpoint to Anthropic provider;
  - Ollama endpoint to Anthropic provider;
  - Anthropic endpoint to OpenAI, Ollama, and OpenCode Go models.
- Descriptor gates:
  - `test_config_descriptor.py` loads every shipped configuration and worker
    descriptor with the new keys;
  - the example configurations validate strictly.
- WebUI tester:
  - protocol `anthropic` builds a valid Messages request;
  - an endpoint that does not speak the selected protocol is rejected in the
    UI instead of producing a 404.
- Redaction regression:
  - keys, session ids, and managed identity values never appear in task
    results, audit payloads, or ordinary logs.

Live validation uses redacted synchronous and streaming fixtures captured
against a reachable OpenCode Go deployment and a generic Anthropic-compatible
server.

## Execution Phases

### Phase 0 — Verify external contracts

Capture redacted fixtures for:

- OpenCode Go Messages: URL, credential header(s), `anthropic-version`, session
  behavior, sync body, SSE body, error envelope;
- generic Anthropic Messages: sync, streaming, tool use, usage, errors;
- model families that are Messages-only versus Chat Completions-only.

Exit criterion: the request URL, headers, and expected response shapes are
verified, and any documentation conflict about OpenCode authentication is
resolved in the proposal before transport code is written.

### Phase 1 — Conversion module and descriptor

Implement `core/messages_protocol.py` with unit tests, then add descriptor
fields: endpoint API, provider API, auth type, model protocol,
`anthropic_version`, and `max_output_tokens`.

Exit criterion: pure conversion tests pass and the shipped configurations load
with no validation errors.

### Phase 2 — Inbound endpoint

Add `Endpoint_anthropic`, register it in `core/app.py`, and implement
authentication, task creation, sync responses, model listing, error mapping, and
`count_tokens`.

Exit criterion: Anthropic SDK-style requests reach fake workers and return valid
sync responses; unknown models and invalid bodies are rejected before queueing.

### Phase 3 — Outbound transport and OpenCode Go split

Extract the three transport hooks from `CallOpenAIWorker`, implement
`call_anthropic`, and make `call_opencode_go` choose the transport from the
resolved model protocol.

Exit criterion: OpenAI, Ollama, and Anthropic inbound clients can all reach a
generic Anthropic provider; OpenCode Go Messages models use `/v1/messages` with
the verified headers while Chat Completions models keep their existing path.

### Phase 4 — Streaming, tools, and thinking fidelity

Implement both SSE state machines, the internal tool loop over Messages, opaque
thinking-block preservation, and native passthrough.

Exit criterion: sync and stream fixtures match expected sequences, internal
tools execute against an Anthropic upstream, and a multi-turn tool-use
conversation keeps its signatures.

### Phase 5 — WebUI, documentation, and release validation

Fix the WebUI tester, document the endpoint/provider/model fields and
limitations in `README.md` and localized READMEs, add the live smoke test, and
run the redaction review.

Exit criterion: the example configuration works against verified deployments,
the WebUI sends a valid Anthropic body, and every focused suite passes.

## Files to Create or Modify

Create:

- `proposal_anthropic.md` (this document);
- `core/messages_protocol.py`;
- `core/endpoints/endpoint_anthropic.py`;
- `workers/agent/call_anthropic/app.py`;
- `test_anthropic_protocol.py`;
- `test_anthropic_endpoint.py`;
- `test_anthropic_worker.py`;
- optional `test_anthropic_live.py` or a section in `TEST.md`.

Modify:

- `core/app.py` — register the endpoint;
- `core/endpoints/__init__.py` — export if needed;
- `core/endpoints/endpoint_ollama.py` — shared `x-api-key` extraction,
  `resolved_protocol` routing, model-protocol lookup, and Anthropic probing;
- `core/smart_router.py` — Anthropic probe support;
- `workers/agent/call_openai/app.py` — transport hooks;
- `workers/agent/call_opencode_go/app.py` — dual transport;
- `config.descriptor.json5` — endpoint/provider/auth/model fields;
- `config.json5`, `config.json5.example`, `.env.example` — examples;
- `webui/backend/app.py`, `webui/frontend/app.js`,
  `webui/frontend/index.html` — tester protocol handling;
- `README.md`, `README_*.md` — user documentation;
- `DOCUMENTS.MD` — document index;
- `TEST.md` — acceptance procedures;
- `memory/context.md`, `memory/history.md` — project state.

## Risks and Open Questions

1. **OpenCode Go authentication conflict.** Official inference documentation
   shows Bearer, while Anthropic-compatible gateway reports use `x-api-key`.
   Phase 0 must settle it. Sending both headers is the fallback, but must be
   covered by a live test.
2. **Base URL variants.** Some documentation uses
   `https://opencode.ai/zen/go` and others
   `https://opencode.ai/inference/go/anthropic`. `baseUrl` remains configurable;
   no path is inferred from a model name.
3. **Thinking signatures after context rewriting.** Phase 4 preserves them only
   for unchanged messages. If the target client requires them after context
   injection, the opaque-block extension must be extended to track exactly which
   context builders changed a message.
4. **`max_tokens` default.** Anthropic requires it, while OpenAI and Ollama
   clients often omit it. The model-level `max_output_tokens` fallback is
   explicit; a missing value must never produce an invalid upstream request.
5. **`count_tokens` accuracy.** The initial estimate is deterministic but
   approximate. If the target coding agent relies on it for compaction, replace
   it with a provider call or a model-specific tokenizer in a follow-up.
6. **Cache control.** `cache_control` is preserved only on unchanged native
   blocks. aidir does not promise cache behavior across conversions.
7. **Model list metadata.** The Anthropic model list shape differs from the
   existing OpenAI shape. The endpoint must not accidentally change
   `/v1/models` on `openaix`.
8. **Protocol field scope.** `protocol` is intentionally per-model, not
   per-request. Supporting a caller override would let a client bypass
   provider-specific requirements such as OpenCode sessions or required auth.
9. **Streaming passthrough with timeouts.** Native SSE passthrough must still
   honor queue and run timeouts and terminate the task on client disconnect.

## Release Gates

The feature is releasable when:

1. generic and OpenCode-specific sync/stream fixtures pass;
2. direct and smart routing select the correct worker and URL for every
   protocol combination;
3. inbound `x-api-key`/Bearer authentication and `envid` policy match existing
   endpoint behavior;
4. OpenCode Go Messages requests carry the verified credential, User-Agent, and
   exactly one stable session header;
5. health probes and `count_tokens` never create a session or a task;
6. tool use, tool results, and thinking blocks round-trip on the native path;
7. cross-protocol conversions preserve usage, stop reasons, reasoning text, and
   error envelopes;
8. redaction review finds no credential, session, or identity leak;
9. `test_config_descriptor.py` and the focused suites pass;
10. README, localized READMEs, `TEST.md`, `DOCUMENTS.MD`, and memory are updated.
