# Proposal: Queue-Managed Embeddings for OpenAIx

## Status

Proposed. This document supersedes the scope decision in `proposal_ollama_embed.md`: the planned first implementation includes both Ollama `POST /api/embed` and OpenAI `POST /v1/embeddings`.

## Objective

Expose embedding models through the existing OpenAIx endpoint while retaining normal aidir routing, task persistence, timeouts, and smart-model selection.

The initial test route is:

```text
client -> local aidir OpenAIx -> smart model qwen3-embedding:4b
       -> ollama_remote_unmanaged -> http://192.168.1.40:11434
  -> qwen3-embedding:4b
```

The remote Ollama instance is initially unmanaged. Its resources must not be represented in the local scheduler. After aidir is deployed on `192.168.1.40`, the same public smart model will instead call that remote aidir instance through its OpenAIx protocol and use its queue-state extension during route selection.

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
  "model": "qwen3-embedding:4b",
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
  "model": "qwen3-embedding:4b",
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
  "model": "qwen3-embedding:4b",
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

### Initial unmanaged remote provider

Add a distinct provider so its unmanaged status is explicit:

```json5
"ollama_embedding_remote": {
  "api": "ollama",
  "baseUrl": "http://192.168.1.40:11434",
  "models": [
    {
      "id": "qwen3-embedding:4b",
      "embedding": true,
      "estimated_vram_mb": 5000
    }
  ]
}
```

`estimated_vram_mb` is documentation and operational planning metadata only. No `resources` block is configured for this provider or model: local aidir neither reserves, unloads, nor reports the VRAM of `192.168.1.40`. A scheduler `resources` entry for an unmanaged remote machine would make the route unavailable because no local resource controller exists for it. Local queueing therefore uses the normal task priority and timeout behavior only; it cannot determine remote GPU availability.

### Public smart model

Add this model under provider `smart`:

```json5
{
  "id": "qwen3-embedding:4b",
  "alias": "qwen3-embedding:4b",
  "type": "first_available",
  "embedding": true,
  "default_tool_injection": false,
  "items": [
    {
      "provider": "ollama_embedding_remote",
      "model": "qwen3-embedding:4b",
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

For an unmanaged remote provider, route probing may verify HTTP/model reachability but must not treat a successful probe as proof that GPU capacity is available.

Both Ollama-capable agent paths, `call_ollama` and `openaix`, implement `request_kind = "embed"`. Each must expose the same queue-managed behavior and use its resolved provider's embed endpoint; neither may silently route an embed task through chat handling.

Embedding tasks use the same default priority as every other task. An explicitly supplied OpenAIx priority retains its normal scheduling semantics. The initial unmanaged provider has no special per-provider concurrency limiter.

## Migration to Remote Aidir

After aidir is deployed on `192.168.1.40` and the embedding model is configured there with scheduler-managed resources:

1. replace `ollama_embedding_remote` in the local configuration with an `api: "openaix"` provider whose `baseUrl` points to the remote aidir endpoint;
2. configure the remote aidir model as `embedding: true` with its actual resource requirements;
3. retain the public smart model id and alias `qwen3-embedding:4b` so clients require no change;
4. query remote OpenAIx queue state for the resolved provider/model before selecting the candidate;
5. use the returned `can_run_now`, queue totals, and priority counts as smart-routing availability evidence, without duplicating remote resource accounting locally;
6. send the eventual embedding request through remote `POST /api/embed` or remote `POST /v1/embeddings`, selected by the provider adapter.

The local instance must not mirror or reserve the remote instance's VRAM. The remote aidir remains the source of truth for its resources and queue.

## Verified Initial Model

`GET http://192.168.1.40:11434/api/tags` has been verified to list `qwen3-embedding:4b`.

The remote embedding endpoint has been live-verified on 2026-09-30: `POST /api/embed` with `qwen3-embedding:4b` returned HTTP `200`, including two ordered vectors of dimension `2560` for a two-string input.

## Trust Boundary

During the initial deployment, hosts on the local network are trusted. No additional application-level authentication is required for the local aidir-to-remote-Ollama call. The later remote-Aidir provider follows the same trust model unless the deployment topology changes.

## Implementation Plan

1. Add shared embed request validation and a task-builder path that sets `request_kind = "embed"`.
2. Add `POST /api/embed` and its Ollama-compatible response/error adapter.
3. Add `POST /v1/embeddings` and its OpenAI-compatible response/error adapter.
4. Add capability-aware concrete and smart-route resolution using `embedding: true`.
5. Add `request_kind = "embed"` upstream execution paths to both `call_ollama` and `openaix`, bypassing chat context and tool processing.
6. Add the initial unmanaged remote provider, its non-operational 5 GB estimate, and the public smart model in `config.json5` and `config.json5.example`.
7. Add focused endpoint, worker, routing, timeout, and configuration tests.
8. Perform live validation against `192.168.1.40:11434` with both public protocols.
9. Update `openaix_spec.md` and `README.md` with the supported contracts and examples.
10. Add the remote `api: "openaix"` adapter and queue-state-aware candidate policy when the remote aidir deployment is ready.

## Acceptance Criteria

1. Both `POST /api/embed` and `POST /v1/embeddings` accept `qwen3-embedding:4b` and return vectors in their respective protocol formats.
2. Requests use the ordinary aidir queue, task history, priority, and timeout mechanics.
3. Embed requests never invoke chat context construction or tools.
4. Non-embedding models cannot be selected for embedding requests.
5. Initial calls reach `qwen3-embedding:4b` at `192.168.1.40:11434` without local remote-VRAM accounting.
6. Automated tests cover successful single/batched input, validation errors, protocol translation, smart routing, task timeouts, and worker upstream errors.
7. Transitioning the public smart model from unmanaged Ollama to remote aidir does not change client model names or public endpoint contracts.