# Proposal: Smart Router for aidir

## Summary

This proposal adds dynamic, resource-aware routing for OpenAIx and Ollama-compatible requests in aidir.

The design introduces two provider classes:

1. `api: "openaix"` for remote aidir-compatible upstreams that must support queue-state and resource-availability probing.
2. `api: "smart"` for local virtual providers whose models are aliases that resolve to a real provider and model at request time.

The first required step is to fix model identity. External clients pass only `model`, not `provider`, so smart routing must start from a stable global model resolution rule. This proposal uses `alias` for that purpose.

The key idea is simple: route before the task is queued, not after a worker has already failed. The current fallback chain is still useful as a last-resort execution policy, but it is too late and too static to be the main routing mechanism.

## Implemented Baseline

The following foundation is already implemented in the codebase:

1. alias-based external model resolution;
2. preferred-provider selection using `workers.items.<worker>.provider` when several providers expose the same external model name;
3. route persistence in `task.config.route` with `resolved_provider` and `resolved_model`;
4. scheduler resource resolution using the resolved route when present;
5. `openaix` and `call_ollama` workers honoring the resolved provider for upstream URL and context-window lookup;
6. `/api/tags` and `/v1/models` exposing `alias` when present, otherwise `id`.

This proposal therefore focuses only on the remaining smart-routing scope.

## Problem Statement

The current fallback behavior is not sufficient for the routing problem described here.

Current limitations in this repository:

1. Fallback is worker-based and static. In `core/scheduler.py`, rejection handling switches between `task.fallbacks` or `workers.items.<worker>.fallbacks`, not between provider and model candidates selected from current resource state.
2. aidir already exposes a queue-state API for `provider/model`, but that information is not yet used when choosing where a new inference request should go.
3. There is still no `api: "smart"` provider implementation that can resolve one alias model into a ranked list of concrete candidates at request time.
4. There is still no model-only queue-state endpoint for peer aidir probing.

At the same time, the project already has the most important building block for distributed routing: queue-state.

## Goals

1. Allow a request to target a virtual alias model and let aidir choose the best concrete provider and model at request time.
2. Fix ambiguous model identity when several providers expose the same model id.
3. Prefer candidates that can start immediately.
4. Use remote aidir instances as first-class routing targets when they expose OpenAIx queue-state.
5. Keep the user-facing API compatible with existing OpenAIx and Ollama-like clients.
6. Minimize invasive changes to the current codebase.

## Non-Goals for v1

1. Replacing the entire scheduler fallback subsystem.
2. Global cluster scheduling across all task types.
3. Predictive routing based on historical latency or token throughput.
4. Full multi-hop federation protocol between aidir instances.

## Current Architecture Constraints

The proposal should match the code that exists today.

1. The endpoint layer is the best interception point for smart inference routing.
2. Task creation before queueing is the earliest point where the provider and model route can be materialized.
3. `Task` already has `task.config`, which is a good place to store resolved routing metadata without leaking internal fields into the external request payload.
4. `Core.get_effective_worker_config()` already establishes the idea of config layering, even though scheduler and workers do not yet apply per-task provider overrides.

Because of these constraints, endpoint-level pre-routing is the correct design for v1.

## Proposed Configuration Model

### 1. Concrete model entries

Each model may expose an external `alias`.

```json5
"ollama_local": {
  "baseUrl": "${OLLAMA_BASE_URL:-http://127.0.0.1:11434}",
  "api": "ollama",
  "models": [
    {
      "id": "qwen3.5:9b",
      "alias": "smart_chat",
      "name": "some model",
      "contextWindow": 128000,
      "resources": {
        "local_machine": {
          "VRAM": 16000
        }
      }
    }
  ]
}
```

If `alias` is omitted, the model remains externally addressable by `id`.

### 2. Remote aidir provider

`api: "openaix"` means the provider is treated as an aidir-compatible upstream: it speaks the OpenAIx-compatible HTTP API and must expose queue-state.

No extra capability flag such as `supports_queue_state` is needed for this proposal. If a remote system does not support queue-state, it should not be configured as `api: "openaix"` for smart-routing purposes.

Suggested provider shape:

```json5
"remote_aidir1": {
  "baseUrl": "${AIDIR_REMOTE_1_BASE_URL:-http://192.168.1.100:21434}",
  "api": "openaix",
  "auth": {
    "type": "bearer",
    "token": "${AIDIR_REMOTE_1_TOKEN:-}"
  },
  "models": [
    {"id": "smart_type_1"},
    {"id": "smart_type_2"},
    {"id": "gpt-4o-mini", "name": "gpt-4o-mini"}
  ]
}
```

### 3. Smart virtual provider

`api: "smart"` means the provider is local and virtual. It does not serve an upstream itself. Its models are routing aliases.

Suggested provider shape:

```json5
"smart": {
  "api": "smart",
  "models": [
    {
      "id": "smart_type_1",
      "alias": "smart_type_1",
      "type": "first_available",
      "items": [
        {
          "provider": "remote_aidir1",
          "model": "smart_type_1",
          "request_timeout_ms": 1500,
          "fallback_prio": 2
        },
        {
          "provider": "ollama_local",
          "model": "qwen3.5:9b",
          "fallback_prio": 1
        },
        {
          "provider": "remote_aidir1",
          "model": "smart_type_2",
          "fallback_prio": 3
        }
      ]
    }
  ]
}
```

### 4. Candidate item fields

For v1, each `items[]` entry should support these fields:

1. `provider`: concrete provider id from `models.providers`.
2. `model`: concrete model id to use on that provider.
3. `request_timeout_ms`: optional probe timeout for remote queue-state checks.
4. `fallback_prio`: optional ranking used only when no candidate can run immediately.

Optional future fields can be added later without breaking the base design.

## Request Routing Flow

### 1. Request entry

1. Client sends `/api/chat` or `/v1/chat/completions` with `model`.
2. The corresponding inference endpoint resolves that value using the alias resolution order described above.
3. If the resolved model belongs to a normal provider, execution may continue directly.
4. If the resolved model belongs to a provider with `api: "smart"`, the endpoint resolves the alias into one concrete route before creating the final queued task.

### 2. Route materialization

The selected route should be written into the task as follows:

1. `task.payload["model"] = <resolved_concrete_model>`
2. `task.config["route"] = { ... }`

Suggested `task.config["route"]` payload:

```json
{
  "requested_model": "smart_type_1",
  "requested_alias": "smart_type_1",
  "requested_provider": "smart",
  "resolved_provider": "ollama_local",
  "resolved_model": "qwen3.5:9b",
  "strategy": "first_available",
  "candidate_index": 1
}
```

This keeps the public request compatible while preserving the actual routing decision for logs, diagnostics, and later scheduler and worker logic.

### 3. Worker selection

The endpoint should continue to resolve `task.worker_id`, but it must now resolve it from the selected concrete provider rather than from the original smart alias.

For v1, the simplest rule is:

1. `api: "ollama"` -> existing `openaix` or `call_ollama` worker, depending on the endpoint path already used by this deployment.
2. `api: "openaix"` -> existing `openaix` worker.
3. `api: "smart"` is never executed directly and must be fully resolved before queueing.

v1 smart-routing should support only concrete provider APIs that are already reachable through existing inference worker paths. It does not need to support every future provider API type from day one.

In practice this means:

1. Smart aliases may resolve directly to `api: "ollama"` providers when they are the final inference executors.
2. Smart aliases may also resolve to `api: "openaix"` providers when the remote aidir instance is the next routing or execution hop.
3. Adding new provider API families later is an extension point, not a v1 requirement.

## Routing Algorithm: `first_available`

The initial strategy should be exactly one routing type: `first_available`.

Algorithm:

1. Iterate `items[]` in configured order.
2. Probe each candidate.
3. If a candidate responds and `can_run_now == true`, select it immediately and stop scanning.
4. If a candidate responds but `can_run_now == false`, keep it as a busy fallback candidate.
5. If a candidate does not respond within `request_timeout_ms`, skip it.
6. After all candidates are checked:
7. If at least one immediate candidate was found, use the first one found in list order.
8. Otherwise, choose the responsive busy candidate with the smallest `fallback_prio`.
9. If several busy candidates have the same `fallback_prio`, keep original list order.
10. If no candidate responded successfully, fail routing before queueing the task.

This matches the behavior requested in the problem statement.

## Candidate Probing Rules

### Providers with directly visible resources

For any provider whose effective model resources are already visible to the current aidir instance, do not call HTTP.

This includes not only providers running on the same host, but also remote non-aidir providers when this aidir instance already monitors the resources they consume.

Use the same internal logic that already powers queue-state:

1. Resolve the concrete model by alias and id using the same global model resolution rules.
2. Resolve resource requirements from `models.providers.<provider>.models[]`.
3. Query queued tasks via the queue manager.
4. Check current resource availability via `core.resources`.
5. Build the same logical result shape as queue-state.

This avoids unnecessary probe HTTP calls for providers whose schedulability can already be evaluated locally.

### Remote aidir providers

Use this path only for candidates whose current schedulability cannot already be decided from locally visible resources.

For `api: "openaix"` candidates, call a model-only queue-state endpoint:

```text
GET {baseUrl}/v1/models/{model}/queue-state?priority={priority}
GET {baseUrl}/api/models/{model}/queue-state?priority={priority}
```

As with the existing inference endpoints, v1 should support both the `/v1/...` and `/api/...` variants for compatibility.

Use `request_timeout_ms` for this probe only.

The routing probe itself is also the availability check. No separate health request is required in v1.

This implies one protocol change in aidir: queue-state probing should support model-only lookup using the same alias resolution semantics as normal inference requests.

The existing exact `provider/model` queue-state endpoints can remain as diagnostic endpoints, but smart-routing should not depend on peer aidir instances knowing the internal provider id.

## Required Runtime Changes

### 1. Smart route resolver

Add a dedicated smart-routing helper used before `Task_agent` is finalized when the resolved external model belongs to a provider with `api: "smart"`.

Recommended implementation shape:

1. New helper class: `core/smart_router.py`
2. Main method: `resolve_route(request_payload, endpoint_id, default_worker_id, request_priority)`
3. Return object: resolved provider, resolved model, resolved worker, route metadata

This keeps smart-routing policy out of the endpoint files while preserving endpoint ownership of request interception.

### 2. Model-only queue-state endpoint

Add a model-only queue-state endpoint that uses the same alias resolution semantics as inference.

This is required so peer aidir instances can be probed without knowing each other's internal provider ids.

For v1, expose both of these equivalent routes:

1. `/v1/models/{model}/queue-state`
2. `/api/models/{model}/queue-state`

### 3. Remote probing and auth

Extend the execution path so smart routing can probe remote `api: "openaix"` providers for queue-state and then call them for inference using provider auth config.

## Provider Authentication

Smart routing to remote aidir instances is not complete unless both the routing probe and the real inference call can authenticate.

Recommended config location:

```json5
models.providers.<provider>.auth
```

Recommended format: reuse the same shape already used by `external_mcp` where possible.

This auth block should be applied to:

1. queue-state probe calls
2. actual `/api/chat` inference calls

Auth precedence for v1 should be:

1. If `models.providers.<provider>.auth` is configured, use it for both the queue-state probe and the real inference call.
2. Otherwise, if the incoming client request carries a Bearer token, forward that same token to both the queue-state probe and the real inference call.
3. Otherwise, call the remote provider without auth.

v1 should not introduce separate probe-auth and inference-auth config blocks. If a remote provider needs a different token than the caller supplied, configure it explicitly in `models.providers.<provider>.auth`.

## Model Listing Behavior

`/api/tags` and `/v1/models` should expose smart alias ids exactly like normal model ids.

This is important for compatibility:

1. Existing OpenAI and Ollama-style clients normally select only `model`.
2. Asking clients to also choose `provider` reduces compatibility.
3. A smart alias should therefore look like a normal model from the client perspective.

The public model list should prefer `alias` as the externally visible identifier. If `alias` is not set, expose `id`.

Internally, aidir can still keep provider-aware diagnostics and exact queue-state APIs.

## Distributed Safety

If aidir instances can route to other aidir instances, routing loops become possible.

Example risk:

1. instance A routes `smart_type_1` to instance B
2. instance B routes the same alias back to instance A
3. the request bounces until timeout

Recommended v1 protection:

1. Generate a stable internal route id for the first smart-routed request.
2. Forward that route id in an internal header such as `X-Aidir-Route-Id`.
3. Forward the visited instance chain in an internal header such as `X-Aidir-Visited-Instances`.
4. Append current `instance` id from config to that chain before forwarding to another aidir instance.
5. If an instance receives an inference request whose visited-instance chain already contains its own `instance` id, reject it as a routing loop.
6. Enforce a small max hop count even if the visited-instance chain is malformed or incomplete.

This does not need a full federation protocol. A simple visited-instance trace plus a stable route id is enough for v1.

This is preferable to checking active tasks by `task.id`:

1. `task.id` is local runtime state unless explicitly propagated across instances.
2. Active-task lookup can miss loops if the previous hop already finished or was evicted from memory.
3. Loop detection should depend on the routing path itself, not on whether another instance still has a matching live task object.

## Observability

Each smart-routed task should log:

1. requested alias model
2. selected provider and model
3. strategy type
4. candidate probe results
5. probe latency per remote candidate
6. final selection reason: `immediate`, `busy_fallback`, or `routing_failed`

This data should also be preserved in task metadata where practical.

## Suggested Implementation Plan

### Phase 1: Smart provider support

1. Add `core/smart_router.py`.
2. Add config parsing helpers for `api: "smart"` providers.
3. Integrate smart-route resolution into inference endpoints after normal alias resolution.

### Phase 2: Remote probing

1. Add a model-only queue-state endpoint that uses alias resolution.
2. Add provider auth support for inference and queue-state probes.
3. Reuse that endpoint for remote aidir probing.

### Phase 3: Distributed robustness

1. Optionally add a short-lived remote probe cache if it stays a small local optimization.
2. Add in-flight probe deduplication in a later iteration if burst traffic proves it necessary.
3. Add loop-protection trace for inter-aidir calls.

### Phase 4: Extended strategies

After `first_available` is stable, add more routing strategies only if still needed.

## Test Plan

### Unit tests

1. Alias model resolves to the first candidate with `can_run_now == true`.
2. Busy candidates are ranked by `fallback_prio`.
3. Timed-out candidates are skipped.
4. Equal `fallback_prio` preserves original order.

### Integration tests

1. `/api/chat` with a smart alias reaches the correct local provider.
2. `/v1/chat/completions` with a smart alias reaches the correct local provider.
3. Model-only queue-state endpoint resolves the same alias to the same provider and model pair as inference does.
4. Remote queue-state timeout skips that candidate and chooses another one.
5. Remote queue-state busy response still allows fallback selection.
6. Authenticated remote aidir works for both queue-state and inference.

### Regression tests

1. Existing non-smart provider routing remains unchanged.
2. Existing exact `provider/model` queue-state endpoint behavior remains unchanged.
3. Existing worker fallback behavior still works after routing has already selected a concrete provider.

## Recommended Final Shape for v1

The minimal coherent v1 is:

1. `api: "smart"` provider with alias models.
2. `first_available` strategy only.
3. Shared alias-based model resolution for inference and queue-state.
4. Remote aidir probing via a model-only queue-state endpoint.
5. Smart route resolution that materializes one concrete provider and model before queueing.

This is enough to solve the actual routing problem without rewriting the scheduler or introducing a large new subsystem.

## TBD and Improvement Notes

Queue-state warm/cold signals policy: optional fields such as `is_loaded` or `would_require_unload` are useful, but they are not required for v1 correctness. Add them in v1 only if they can be implemented as a small extension of the existing queue-state calculation with no significant new coordination or provider-specific complexity. Otherwise, defer them to v2.

Additional routing strategies policy: strategies such as `least_queued` or `weighted_preferred` are useful, but they are not required for the first release. Add them in v1 only if they remain a small, low-risk extension on top of `first_available`. If they require materially more ranking logic, observability, or tuning surface, keep v1 limited to `first_available` and defer extra strategies to v2.

Remote probe cache policy for v1: implement it only if the cache can remain a simple short-lived local optimization with no complex invalidation or coordination logic. If it starts to require notable complexity, defer it to v2.

In-flight probe deduplication policy: it is not required for v1 correctness and should normally be deferred to v2. Add it only after real burst-load evidence shows that repeated concurrent probe calls are a practical problem worth the extra coordination complexity.