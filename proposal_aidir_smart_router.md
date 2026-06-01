# Proposal: Smart Router for aidir

## Summary

This proposal adds dynamic, resource-aware routing for OpenAIx and Ollama-compatible requests in aidir.

The design introduces two provider classes:

1. `api: "openaix"` for remote aidir-compatible upstreams that can be probed for queue/resource availability.
2. `api: "smart"` for local virtual providers whose models are aliases that resolve to a real provider/model at request time.

The key idea is simple: route before the task is queued, not after a worker has already failed. The current fallback chain is still useful as a last-resort execution policy, but it is too late and too static to be the main routing mechanism.

## Problem Statement

The current fallback behavior is not sufficient for the routing problem described here.

Current limitations in this repository:

1. Fallback is worker-based and static. In `core/scheduler.py`, rejection handling switches between `task.fallbacks` or `workers.items.<worker>.fallbacks`, not between provider/model candidates selected from current resource state.
2. The active provider for `workers/agent/openaix/app.py` and `workers/agent/call_ollama/app.py` is resolved from worker config, not per request.
3. Resource reservation in `core/scheduler.py` resolves requirements from `worker.provider + payload.model`, so dynamic provider switching is not represented in the current task model.
4. aidir already exposes a queue-state API for `provider/model`, but that information is not yet used when choosing where a new inference request should go.

At the same time, the project already has the most important building block for distributed routing: `GET /api/providers/{provider}/models/{model}/queue-state` and `GET /v1/providers/{provider}/models/{model}/queue-state`.

## Goals

1. Allow a request to target a virtual alias model and let aidir choose the best concrete provider/model at request time.
2. Prefer candidates that can start immediately.
3. Use remote aidir instances as first-class routing targets when they expose OpenAIx queue-state.
4. Keep the user-facing API compatible with existing OpenAIx and Ollama-like clients.
5. Minimize invasive changes to the current codebase.

## Non-Goals for v1

1. Replacing the entire scheduler fallback subsystem.
2. Global cluster scheduling across all task types.
3. Predictive routing based on historical latency or token throughput.
4. Full multi-hop federation protocol between aidir instances.

## Current Architecture Constraints

The proposal should match the code that exists today.

1. `core/endpoints/endpoint_openaix.py` already handles both `/api/chat` and `/v1/chat/completions`, so this is the best interception point for smart inference routing.
2. `Endpoint_openaix._build_task_for_payload()` creates the task before queueing, which is the earliest point where the provider/model route can be materialized.
3. `Task` already has `task.config`, which is a good place to store resolved routing metadata without leaking internal fields into the external request payload.
4. `Core.get_effective_worker_config()` already establishes the idea of config layering, even though scheduler and workers do not yet apply per-task provider overrides.

Because of these constraints, endpoint-level pre-routing is the correct design for v1.

## Proposed Configuration Model

### 1. Remote aidir provider

`api: "openaix"` means the provider speaks the OpenAIx-compatible HTTP API and may expose queue-state.

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

### 2. Smart virtual provider

`api: "smart"` means the provider is local and virtual. It does not serve an upstream itself. Its models are routing aliases.

Suggested provider shape:

```json5
"smart": {
  "api": "smart",
  "models": [
    {
      "id": "smart_type_1",
      "type": "first_available",
      "items": [
        {
          "provider": "remote_aidir1",
          "model": "smart_type_1",
          "probe_provider": "smart",
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
          "probe_provider": "smart",
          "fallback_prio": 3
        }
      ]
    },
    {
      "id": "smart_type_2",
      "type": "first_available",
      "items": []
    }
  ]
}
```

### 3. Candidate item fields

For v1, each `items[]` entry should support these fields:

1. `provider`: concrete provider id from `models.providers`.
2. `model`: concrete model id to use on that provider.
3. `probe_provider`: optional provider id to use for queue-state probing when the remote aidir provider name differs from the local transport provider id.
4. `request_timeout_ms`: optional probe timeout for remote queue-state checks.
5. `fallback_prio`: optional ranking used only when no candidate can run immediately.

Optional future fields can be added later without breaking the base design.

TBD: If the remote queue-state target also needs a different model id from the actual inference model, add `probe_model`; in v1 it can default to `model`.

## Request Routing Flow

### 1. Request entry

1. Client sends `/api/chat` or `/v1/chat/completions` with `model`.
2. `endpoint_openaix` checks whether that model id belongs to a provider with `api: "smart"`.
3. If not, current behavior remains unchanged.
4. If yes, the endpoint resolves the alias into one concrete route before creating the final queued task.

### 2. Route materialization

The selected route should be written into the task as follows:

1. `task.payload["model"] = <resolved_concrete_model>`
2. `task.config["route"] = { ... }`

Suggested `task.config["route"]` payload:

```json
{
  "requested_model": "smart_type_1",
  "requested_provider": "smart",
  "resolved_provider": "ollama_local",
  "resolved_model": "qwen3.5:9b",
  "strategy": "first_available",
  "candidate_index": 1
}
```

This keeps the public request compatible while preserving the actual routing decision for logs, diagnostics, and later scheduler/worker logic.

### 3. Worker selection

The endpoint should continue to resolve `task.worker_id`, but it must now resolve it from the selected concrete provider rather than from the original smart alias.

For v1, the simplest rule is:

1. `api: "ollama"` -> existing `openaix` or `call_ollama` worker, depending on the endpoint path already used by this deployment.
2. `api: "openaix"` -> existing `openaix` worker.
3. `api: "smart"` is never executed directly and must be fully resolved before queueing.

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

### Local providers

For local providers on the same aidir instance, do not call HTTP.

Use the same internal logic that already powers queue-state:

1. Resolve resource requirements from `models.providers.<provider>.models[]`.
2. Query queued tasks via the queue manager.
3. Check current resource availability via `core.resources`.
4. Build the same logical result shape as queue-state.

This avoids unnecessary local loopback requests.

### Remote aidir providers

For `api: "openaix"` candidates, call:

```text
GET {baseUrl}/v1/providers/{probe_provider}/models/{probe_model}/queue-state?priority={priority}
```

Use `request_timeout_ms` for this probe only.

The routing probe itself is also the availability check. No separate health request is required in v1.

## Required Runtime Changes

### 1. Endpoint layer

Add a dedicated smart-routing helper used by `core/endpoints/endpoint_openaix.py` before `Task_agent` is finalized.

Recommended implementation shape:

1. New helper class: `core/smart_router.py`
2. Main method: `resolve_route(request_payload, endpoint_id, default_worker_id, request_priority)`
3. Return object: resolved provider, resolved model, resolved worker, route metadata

This keeps smart-routing policy out of the endpoint file while preserving endpoint ownership of request interception.

### 2. Scheduler resource resolution

Update `Scheduler._resolve_resource_requirements()` so it uses, in this order:

1. `task.resource_requirements` if already fixed.
2. `task.config.route.resolved_provider` + `task.config.route.resolved_model` if present.
3. Existing worker-config provider + payload model fallback.

Without this change, the scheduler will reserve the wrong resources for dynamically routed requests.

### 3. Agent worker provider resolution

Update `workers/agent/openaix/app.py` and `workers/agent/call_ollama/app.py` so they resolve provider-specific runtime data from `task.config.route.resolved_provider` when present.

That includes:

1. upstream `baseUrl`
2. auth headers
3. model `contextWindow`
4. any provider-specific request settings added later

Without this change, the worker will still call the provider configured statically in `workers.items.<worker>.provider`.

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

## Model Listing Behavior

`/api/tags` and `/v1/models` should expose smart alias ids exactly like normal model ids.

This is important for compatibility:

1. Existing OpenAI and Ollama-style clients normally select only `model`.
2. Asking clients to also choose `provider` reduces compatibility.
3. A smart alias should therefore look like a normal model from the client perspective.

Internally, aidir can still keep provider-aware diagnostics and queue-state APIs.

## Distributed Safety

If aidir instances can route to other aidir instances, routing loops become possible.

Example risk:

1. instance A routes `smart_type_1` to instance B
2. instance B routes the same alias back to instance A
3. the request bounces until timeout

Recommended v1 protection:

1. Add an internal hop-trace header for inter-aidir inference requests.
2. Include current `instance` id in the trace.
3. Reject or skip candidates that point to an already visited instance.
4. Enforce a small max hop count.

This does not need a full federation protocol. A simple visited-instance trace is enough for v1.

## Observability

Each smart-routed task should log:

1. requested alias model
2. selected provider/model
3. strategy type
4. candidate probe results
5. probe latency per remote candidate
6. final selection reason: `immediate`, `busy_fallback`, or `routing_failed`

This data should also be preserved in task metadata where practical.

## Suggested Implementation Plan

### Phase 1: Core routing support

1. Add `core/smart_router.py`.
2. Add config parsing helpers for `api: "smart"` providers and alias models.
3. Integrate route resolution into `endpoint_openaix`.
4. Persist route metadata into `task.config.route`.

### Phase 2: Execution correctness

1. Update scheduler resource resolution.
2. Update `openaix` and `call_ollama` workers to use resolved provider overrides.
3. Add provider auth support for inference and queue-state probes.

### Phase 3: Distributed robustness

1. Add short-lived remote probe cache.
2. Add in-flight probe deduplication.
3. Add loop-protection trace for inter-aidir calls.

### Phase 4: Extended strategies

After `first_available` is stable, add more routing strategies only if still needed.

## Test Plan

### Unit tests

1. Alias model resolves to the first candidate with `can_run_now == true`.
2. Busy candidates are ranked by `fallback_prio`.
3. Timed-out candidates are skipped.
4. Equal `fallback_prio` preserves original order.
5. Scheduler resource resolution uses `task.config.route` when present.
6. Worker provider override uses `task.config.route.resolved_provider`.

### Integration tests

1. `/api/chat` with a smart alias reaches the correct local provider.
2. `/v1/chat/completions` with a smart alias reaches the correct local provider.
3. Remote queue-state timeout skips that candidate and chooses another one.
4. Remote queue-state busy response still allows fallback selection.
5. Authenticated remote aidir works for both queue-state and inference.

### Regression tests

1. Existing non-smart provider routing remains unchanged.
2. Existing queue-state endpoint behavior remains unchanged.
3. Existing worker fallback behavior still works after routing has already selected a concrete provider.

## Recommended Final Shape for v1

The minimal coherent v1 is:

1. `api: "smart"` provider with alias models.
2. `first_available` strategy only.
3. Endpoint-level route resolution in `endpoint_openaix`.
4. Resolved route persisted in `task.config.route`.
5. Scheduler and relevant agent workers updated to honor that resolved route.
6. Remote aidir probing via existing queue-state endpoint.

This is enough to solve the actual routing problem without rewriting the scheduler or introducing a large new subsystem.

## TBD and Improvement Notes

TBD: The current OpenAIx and Ollama-compatible chat requests are primarily model-driven, not provider-driven. Decide whether smart routing should be selected only by alias model id, or whether a new optional OpenAIx-only `provider` request field is also needed for debugging/admin use.

TBD: `GET /api/tags` and `GET /v1/models` currently expose model ids without provider namespace. Decide whether smart alias ids must be globally unique across all providers.

TBD: `api: "openaix"` should not automatically mean "this is definitely another aidir instance" unless we explicitly require queue-state support from every OpenAIx upstream. Decide whether to add an explicit capability flag such as `supports_queue_state: true`.

TBD: Align routing priority semantics. The queue-state endpoint defaults to `priority=5`, while `Task.priority` currently defaults to `10`. Decide which value smart-routing should use when the client does not provide an explicit priority.

TBD: Decide whether the remote queue-state probe should use the same bearer token as the real inference call, or whether probe auth and inference auth must be configurable separately.

TBD: Decide whether v1 smart-routing supports only providers reachable through the existing Ollama/OpenAIx worker path, or whether it must already support every future provider API type.

TBD: Add a short-lived cache for remote queue-state probes, otherwise one incoming request can trigger several outbound probe requests even when traffic is bursty.

TBD: Add in-flight probe deduplication so concurrent requests for the same smart alias can reuse the same remote queue-state call.

TBD: Consider extending queue-state with optional warm/cold signals such as `is_loaded` or `would_require_unload`, because `can_run_now` alone may be too coarse once several candidates are all technically runnable.

TBD: Add loop protection for inter-aidir routing, otherwise two instances can route the same alias back to each other.

TBD: Consider adding a later strategy such as `least_queued` or `weighted_preferred`, but do not add more routing types until `first_available` is proven in production.