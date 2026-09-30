# Proposal: Queue-Managed Embeddings for OpenAIx

## Status

Proposed. This document supersedes the scope decision in `proposal_ollama_embed.md`: the planned first implementation includes both Ollama `POST /api/embed` and OpenAI `POST /v1/embeddings`.

## Objective

Expose embedding models through the existing OpenAIx endpoint while retaining normal aidir routing, task persistence, timeouts, and smart-model selection.

The initial test route is:

```text
client -> aidir OpenAIx -> public embedding alias
  -> remote aidir provider -> concrete embedding model
```

The remote aidir instance owns its own resources and queue. The calling instance must not mirror or reserve remote resources; smart routing uses the remote OpenAIx queue-state extension as availability evidence.

## Design Principles

1. An embedding request is a normal queued inference task, not a direct proxy request.
2. Embeddings reuse `Task_agent`, the normal queue, task persistence, queue timeout, run timeout, and upstream timeout rules.
3. The task has `task.config["request_kind"] = "embed"`; existing chat tasks use `"chat"` or retain their existing behavior during migration.
4. Embedding tasks never enter the context-builder, internal-tool, or tool-loop paths.
5. Embedding responses are non-streaming.
6. A model is eligible only when its model configuration declares `embedding: true`.
7. The caller chooses a public model name only. Provider and worker selection remain internal routing decisions.

## Public API

### Ollama-compatible endpoint

Add `POST /api/embed` to the existing OpenAIx HTTP service.

Accepted request shape:

```json
{
  "model": "embedding-model",
  "input": ["first text", "second text"],
  "truncate": true
}
```

Required fields:

1. `model`: string.
2. `input`: string or an array of strings. Other input forms, including token-ID arrays and mixed arrays, return `400` in the first implementation.

Supported aidir extensions:

1. `envid`: string.
2. `worker`: string, subject to the normal worker override rules.
3. `queue_timeout`: non-negative integer seconds.
4. `timeout`: existing combined queue/run timeout extension, if retained by the endpoint.
5. `log`: existing logging options.
6. `priority`: only if the current endpoint task builder supports it.

`stream`, `tools`, `tool_choice`, and `context_builder` are invalid for this operation and return `400`. Operation-safe Ollama fields, including `truncate`, `keep_alive`, and provider-supported options, are forwarded unchanged after aidir removes its own routing extensions. Upstream validation errors are returned through the normal error contract.

Successful response preserves the upstream Ollama `/api/embed` response, including `model`, `embeddings`, durations, and token counters when present.

### OpenAI-compatible endpoint

Add `POST /v1/embeddings` to the same service.

Accepted request shape:

```json
{
  "model": "embedding-model",
  "input": ["first text", "second text"],
  "encoding_format": "float"
}
```

Required fields:

1. `model`: string.
2. `input`: string or an array of strings. Other forms return `400` in the first implementation.

The endpoint maps the request to the shared internal embed task, then converts the completed vector list into an OpenAI-compatible response:

```json
{
  "object": "list",
  "data": [
    {"object": "embedding", "embedding": [0.1, 0.2], "index": 0}
  ],
  "model": "embedding-model",
  "usage": {"prompt_tokens": 2, "total_tokens": 2}
}
```

Rules:

1. preserve input ordering and assign zero-based `index` values;
2. map trustworthy upstream prompt and total token counters to `usage`; omit unavailable counters rather than inventing values;
3. support `encoding_format: "float"` and `encoding_format: "base64"`; base64 encoding is applied by the OpenAI response adapter after receiving float vectors from the upstream;
4. forward `dimensions` to the upstream model unchanged. If it rejects the parameter or does not support dimension reduction, return its error through the normal error contract;
5. preserve and forward other operation-safe upstream parameters whenever no additional worker, task, or response adapter is required. `user` is retained as aidir request metadata because Ollama has no equivalent upstream field.

### Error Contract

Both protocol adapters share internal error classification:

1. malformed JSON, missing `model`, invalid `input`, or unsupported embed fields: `400`;
2. unknown model or a model without `embedding: true`: `422`;
3. route unavailable or queue rejection: `503`;
4. upstream connection or upstream protocol error: `502`;
5. queue timeout, run timeout, or upstream timeout: `504`.

The OpenAI endpoint uses the existing OpenAI error envelope. The Ollama endpoint uses the established Ollama/OpenAIx error shape.

## Configuration

### Remote aidir provider

A deployment configures its remote aidir provider and mirrors its published model catalog:

```json5
"remote_aidir": {
  "api": "openaix",
  "baseUrl": "https://remote-aidir.example",
  "models": [
    {"id": "remote-chat-model"},
    {
      "id": "remote-embedding-model",
      "embedding": true,
    }
  ]
}
```

No `resources` block is configured on the calling instance: remote aidir is the source of truth for its resources and queue. Smart routing queries the remote OpenAIx queue-state endpoint and does not create a duplicate remote resource reservation.

### Public smart model

Add this model under provider `smart`:

```json5
{
  "id": "embedding-model",
  "alias": "embedding-model",
  "type": "first_available",
  "embedding": true,
  "default_tool_injection": false,
  "items": [
    {
      "provider": "remote_aidir",
      "model": "remote-embedding-model",
      "request_timeout_ms": 1500,
      "fallback_prio": 10
    }
  ]
}
```

Smart candidate validation for `request_kind = "embed"` must require `embedding: true` on the public smart model and the resolved concrete model.

`embedding: true` is an embed-operation capability flag only. Chat routes preserve the existing Ollama-style behavior: aidir does not add a separate chat capability gate, and an embedding-only model selected for chat receives the normal upstream result or upstream error.

## Execution and Routing

1. The endpoint validates the protocol request and resolves the requested public or concrete model for embedding capability.
2. It creates a normal `Task_agent`, marks it as `request_kind = "embed"`, and attaches the resolved route and resource requirements when they exist.
3. The scheduler applies normal queue, priority, timeout, and configured-resource rules.
4. The selected worker sends only an Ollama embed payload to `POST {baseUrl}/api/embed`.
5. The worker returns the upstream embedding data to the endpoint.
6. The endpoint serializes either the Ollama-preserving response or the OpenAI response.

For `api: "openaix"` remote providers, route probing reads the remote queue-state endpoint. A successful probe is availability evidence only; the remote instance remains the authority for scheduling and resource accounting.

Both Ollama-capable agent paths, `call_ollama` and `openaix`, implement `request_kind = "embed"`. Each must expose the same queue-managed behavior and use its resolved provider's embed endpoint; neither may silently route an embed task through chat handling.

Embedding tasks use the same default priority as every other task. An explicitly supplied OpenAIx priority retains its normal scheduling semantics. The initial unmanaged provider has no special per-provider concurrency limiter.

## Remote Aidir Routing

1. The public smart alias remains stable when its concrete remote route changes.
2. Smart routing queries remote queue state for the configured provider/model pair before selecting the candidate.
3. The embedding task is sent to remote aidir through its OpenAIx adapter, which selects the remote embedding route.
4. The remote instance remains the source of truth for its queue and resources.

## Deployment Requirements

1. The remote provider URL, authentication, and model catalog are deployment configuration, not protocol constants.
2. The remote catalog must include the configured concrete embedding model with `embedding: true`.
3. The remote OpenAIx endpoint must expose queue state and the selected embedding protocol.
4. Network trust and authentication must follow the deployment security policy.

## Implementation Plan

1. Add shared embed request validation and a task-builder path that sets `request_kind = "embed"`.
2. Add `POST /api/embed` and its Ollama-compatible response/error adapter.
3. Add `POST /v1/embeddings` and its OpenAI-compatible response/error adapter.
4. Add capability-aware concrete and smart-route resolution using `embedding: true`.
5. Add `request_kind = "embed"` upstream execution paths to both `call_ollama` and `openaix`, bypassing chat context and tool processing.
6. Configure the remote aidir provider, mirror its published model catalog, and route the public smart embedding model through it.
7. Add focused endpoint, worker, routing, timeout, and configuration tests.
8. Perform live validation against the configured remote aidir endpoint with both public protocols.
9. Update `openaix_spec.md` and `README.md` with the supported contracts and examples.
10. Validate the remote `api: "openaix"` adapter and queue-state-aware candidate policy in the target deployment.

## Acceptance Criteria

1. Both `POST /api/embed` and `POST /v1/embeddings` accept the configured public embedding alias and return vectors in their respective protocol formats.
2. Requests use the ordinary aidir queue, task history, priority, and timeout mechanics.
3. Embed requests never invoke chat context construction or tools.
4. Non-embedding models cannot be selected for embedding requests.
5. Calls reach the configured remote embedding model without duplicate resource accounting on the calling instance.
6. Automated tests cover successful single/batched input, validation errors, protocol translation, smart routing, task timeouts, and worker upstream errors.
7. Transitioning the public smart model from unmanaged Ollama to remote aidir does not change client model names or public endpoint contracts.