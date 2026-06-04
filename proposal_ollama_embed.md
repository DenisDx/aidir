# Proposal: Transparent Ollama `/api/embed` Support in OpenAIx

## Summary

This proposal defines the implementation task for adding transparent Ollama-compatible `POST /api/embed` support to aidir/OpenAIx.

The new endpoint must:

1. enter the normal aidir task queue;
2. use the same timeout, worker, and resource-management path as other inference requests;
3. support smart routing before queueing;
4. work both for direct Ollama providers and for remote `api: "openaix"` providers;
5. reject models that do not declare embedding support in config.

The goal is not a local shortcut proxy. The goal is full first-class queue-managed embedding execution.

## Design Decision

For v1, embeddings should reuse the existing `Task_agent` path instead of introducing a new task type.

Reason:

1. embeddings are still an inference request against a model;
2. queueing, timeout handling, Redis persistence, external task lifecycle, and resource scheduling already work for `Task_agent`;
3. the main missing part is operation-specific endpoint/worker behavior, not a different scheduler contract.

The request kind must therefore be carried explicitly in task metadata.

Required representation:

1. `task.config["request_kind"] = "embed"` for embed requests;
2. `task.config["request_kind"] = "chat"` for chat requests created by the same endpoint stack.

This keeps the queue/resource model stable while allowing endpoint and worker code to branch safely.

## Goals

1. Add Ollama-compatible `POST /api/embed` to the OpenAIx endpoint service.
2. Ensure embedding requests are queued and scheduled like chat requests.
3. Ensure resource accounting is based on the resolved model, exactly as it already is for chat.
4. Ensure smart routing can resolve embedding requests to the best concrete provider/model.
5. Ensure model config can explicitly declare whether embeddings are supported.
6. Keep changes minimal and aligned with the current architecture.

## Non-Goals for v1

1. Do not introduce a separate embedding scheduler.
2. Do not introduce embedding-specific resource metrics in v1.
3. Do not add streaming for embeddings.
4. Do not redesign the worker/task type hierarchy.
5. Do not implement full OpenAI `POST /v1/embeddings` unless explicitly included as an additional scope item.

## Public API Contract

### 1. Endpoint

Add:

1. `POST /api/embed` on the OpenAIx/Ollama-compatible HTTP app.

This endpoint must be available anywhere the current OpenAIx service already exposes `/api/chat`.

### 2. Request format

The endpoint should accept the Ollama-compatible embed payload shape and forward the relevant fields upstream.

Required/primary fields:

1. `model: string`
2. `input: string | array`

Supported OpenAIx extension fields:

1. `worker: string`
2. `envid: string`
3. `log: object`
4. `priority: integer` if already supported by current routing logic

Explicit v1 rules:

1. `stream` is not supported on `/api/embed`.
2. `tools` must be ignored or rejected for `/api/embed`.
3. `context_builder` must not be executed for `/api/embed`.
4. Chat-only generation fields such as `temperature`, `top_p`, `num_predict`, `tool_choice`, and `response_format` must not affect embedding execution.

### 3. Response format

The endpoint must return an Ollama-compatible non-stream embedding response.

The exact upstream response should be preserved as much as possible.

Expected minimum shape:

```json
{
  "model": "...",
  "embeddings": [[0.1, 0.2, 0.3]],
  "total_duration": 123,
  "load_duration": 45,
  "prompt_eval_count": 7
}
```

If the upstream returns additional Ollama-compatible fields, proxy them unchanged.

### 4. Error behavior

The endpoint must use the same queueing and timeout semantics as chat endpoints.

Minimum expected mapping:

1. invalid JSON or missing required fields -> `400`
2. unknown model or model without embedding support -> `422`
3. queue rejection -> `503`
4. upstream unavailable/error -> `502`
5. queue/run timeout -> `504`

## Configuration Model

### 1. Model capability flag

Each concrete model entry in `models.providers.<provider>.models[]` must support a boolean flag:

```json5
{
  id: "nomic-embed-text",
  embedding: true,
  resources: {
    local_machine: {
      VRAM: 2800
    }
  }
}
```

Rules:

1. `embedding: true` means the model may serve `/api/embed`.
2. missing or false means the model must not be selected for embed requests.
3. chat support remains implicit as it is today unless a future capability system is introduced.

### 2. Smart virtual models

Smart models under `api: "smart"` must also declare embedding support when they are valid targets for `/api/embed`.

Example:

```json5
{
  id: "smart_embed_default",
  alias: "smart_embed_default",
  embedding: true,
  type: "first_available",
  items: [
    { provider: "ollama_local", model: "nomic-embed-text", request_timeout_ms: 1500, fallback_prio: 10 },
    { provider: "ollama_remote", model: "nomic-embed-text", request_timeout_ms: 1500, fallback_prio: 20 }
  ]
}
```

Rules:

1. smart aliases intended for embed requests must set `embedding: true`;
2. every concrete candidate in `items[]` must resolve to a model with `embedding: true`;
3. if a candidate resolves to a non-embedding model, routing must reject it as invalid configuration.

### 3. Resource accounting

For v1, embedding requests use the same model-level `resources` block already used by chat requests.

This means:

1. no new resource schema is required in v1;
2. scheduler and queue-state continue to reason about the resolved model's configured resources;
3. chat and embed requests targeting the same model contend for the same capacity.

This is acceptable for v1 and keeps the implementation small.

Future extension:

If needed later, per-operation resource overrides can be introduced as an optional model field such as `operation_resources.embed`.

## Routing and Scheduling Rules

### 1. Route resolution

Embed requests must use the same route resolution phase as chat requests, before queueing.

The difference is candidate filtering.

For `request_kind = "embed"`:

1. normal model resolution must reject any concrete model whose `embedding` flag is not true;
2. smart routing must evaluate only candidates whose resolved models support embeddings;
3. the selected route must be stored in `task.config["route"]` exactly as chat does today.

### 2. Queue-state interaction

The existing queue-state model can remain resource-based and operation-agnostic.

No new queue-state endpoint is required for v1.

Reason:

1. queue-state currently reports whether the resolved model resources can run now;
2. embed and chat requests should both consume the same model resources in v1;
3. therefore the existing queue-state semantics are already sufficient for smart routing decisions.

### 3. Worker selection

Worker resolution rules remain the same as chat:

1. `api: "ollama"` concrete route -> existing worker path for Ollama inference;
2. `api: "openaix"` concrete route -> existing `openaix` worker;
3. `api: "smart"` must be fully resolved before queueing.

The worker must branch by `task.config["request_kind"]`, not by task type.

## Required Code Changes

### 1. Endpoint layer

#### `core/endpoints/endpoint_ollama.py`

Add:

1. route registration for `POST /api/embed`;
2. request parsing and validation for embed payloads;
3. task creation with `task.config["request_kind"] = "embed"`;
4. sync wait path equivalent to chat, but without streaming support;
5. embed-specific success/error response mapping.

Required behavior:

1. set `task.queue_timeout` and `task.run_timeout` exactly like chat;
2. resolve worker/provider/model route exactly like chat;
3. reject unsupported streaming;
4. reject missing `input`;
5. reject models without `embedding: true`.

#### `core/endpoints/endpoint_openaix.py`

Add:

1. `POST /api/embed` on the OpenAIx app;
2. reuse the same embed task builder as the Ollama-compatible layer;
3. ensure smart routing and worker resolution are identical to chat except for embedding-capability checks.

Optional follow-up, not required for this proposal unless explicitly enabled:

1. add `POST /v1/embeddings` and map it to the same internal execution path.

### 2. Task creation helpers

#### `core/endpoints/endpoint_ollama.py`

Current task creation is chat-oriented but structurally reusable.

Required changes:

1. allow task creation helpers to receive `request_kind`;
2. write `request_kind` into `task.config`;
3. keep `Task_agent` as the created task class.

Do not create a second queue or special task subclass in v1.

### 3. Model capability resolution

#### `core/endpoints/endpoint_ollama.py`
#### `core/endpoints/endpoint_openaix.py`
#### `core/scheduler.py`

Add helper logic to detect whether a resolved model supports embeddings.

Required helper behavior:

1. for a concrete provider/model pair, read `models.providers.<provider>.models[]...embedding`;
2. for a smart model alias, require `embedding: true` on the smart model itself when used for embed routing;
3. when evaluating smart candidates, require `embedding: true` on the resolved concrete model;
4. produce a clear routing error when no embedding-capable candidate exists.

### 4. Worker execution path

#### `workers/agent/openaix/app.py`

This worker currently always targets upstream `POST /api/chat` and applies chat-only logic.

Required refactor:

1. branch on `request_kind`;
2. for `chat` keep current behavior;
3. for `embed` call upstream `POST /api/embed`;
4. for `embed`, skip:
   1. context-builder chain;
   2. internal tools injection and tool loop;
   3. stream handling;
   4. chat-generation normalization.

Embed execution path should:

1. build a small embed payload from the incoming request;
2. preserve `model`, `input`, and allowed metadata fields;
3. use the same per-call upstream timeout resolution as other requests;
4. log request/response similarly to current LLM call logging where applicable;
5. return upstream embedding JSON in `WorkerResult.data`.

### 5. Optional worker path review

#### `workers/agent/call_ollama/app.py`

If this worker can be selected for Ollama inference routes in this deployment, it must either:

1. gain the same `request_kind = "embed"` support; or
2. be excluded from embed routing so that embeddings always go through `openaix`.

The implementation must choose one behavior explicitly. Do not leave it ambiguous.

Recommended v1 choice:

1. route embeddings through `openaix` only, because it already contains the richer provider-aware logic.

If this recommendation is used, document it and enforce worker selection accordingly.

## Validation Rules

### 1. Request validation

Embed endpoints must validate:

1. JSON body is an object;
2. `model` is present when required by current worker selection rules;
3. `input` exists and is string or array;
4. `stream=true` is rejected;
5. embed requests do not attempt to use chat-only tool flow.

### 2. Config validation

At minimum, runtime validation must detect:

1. smart embed alias points to a concrete model without `embedding: true`;
2. request selects a concrete model without `embedding: true`;
3. route resolves to a worker/provider combination that does not support embed execution.

## Tests

Add focused tests. This work is not complete without them.

### 1. Endpoint tests

Add tests for:

1. `POST /api/embed` success path;
2. missing `input` -> `400`;
3. `stream=true` -> `400`;
4. model without embedding support -> `422`;
5. queue timeout / run timeout mapping -> `504`;
6. queue rejection -> `503`.

### 2. Routing tests

Add tests for:

1. smart alias with `embedding: true` resolves to first valid embedding-capable candidate;
2. smart alias skips candidates without `embedding: true`;
3. smart alias fails when no candidate supports embeddings;
4. queue-state-based first-available logic still applies to embed requests.

### 3. Worker tests

Add tests for:

1. embed requests call upstream `/api/embed` rather than `/api/chat`;
2. embed requests do not invoke context-builder chain;
3. embed requests do not enter tool loop;
4. upstream embed response is returned unchanged in worker result;
5. upstream timeout and upstream connection failures map correctly.

### 4. Config/resource tests

Add tests for:

1. resolved model resources are still attached to embed tasks;
2. embed task enters normal queue and scheduler path;
3. embed and chat requests for the same model contend for the same resources in v1.

## Documentation Updates Required by Implementation

Implementation is not complete until the following docs are updated:

1. `openaix_spec.md`:
   1. add `/api/embed` endpoint contract;
   2. document `embedding: true` model capability requirement;
   3. document that embed requests are non-streaming and skip context/tool flow.
2. `README.md`:
   1. add `/api/embed` to endpoint list;
   2. add one curl example;
   3. explain the `embedding` model flag.
3. `config.json5.example`:
   1. show at least one model with `embedding: true`;
   2. optionally show one smart embedding alias example.

## Recommended Implementation Order

1. Add model capability helpers for `embedding` support.
2. Add embed task creation path in endpoint helpers.
3. Add `POST /api/embed` endpoint handlers.
4. Add worker `request_kind = "embed"` upstream execution branch.
5. Add smart-routing candidate filtering for embedding capability.
6. Add focused tests.
7. Update docs/examples.

This order minimizes the risk of mixing protocol work with scheduler behavior changes.

## Acceptance Criteria

The task is complete only when all points below are true:

1. `POST /api/embed` is exposed by OpenAIx and returns Ollama-compatible embedding JSON.
2. The request is stored and executed as a normal queued external task.
3. Queue timeout, run timeout, and per-call upstream timeout work the same way as for chat.
4. Resource arbitration uses the resolved model resources before execution.
5. Smart routing works for embedding aliases and ignores non-embedding candidates.
6. A model without `embedding: true` cannot be used for embed requests.
7. Remote `api: "openaix"` providers can serve embed requests through the same routed path.
8. Focused automated tests cover endpoint, worker, routing, timeout, and resource behavior.
9. `openaix_spec.md`, `README.md`, and config examples are updated.

## Explicit v1 Simplifications

The following simplifications are intentional and acceptable:

1. keep `Task_agent` instead of adding `Task_embed`;
2. use one new metadata field, `task.config["request_kind"]`;
3. reuse model-level `resources` for both chat and embeddings;
4. support only non-stream embed responses;
5. do not add new queue-state endpoints.

These choices keep the change set focused while still delivering full transparent embed support through the existing aidir execution model.