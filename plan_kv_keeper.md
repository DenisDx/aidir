# Plan: KV Keeper Implementation

## Status

Implemented and verified. Full suite: 663 tests pass. W15 made the
affinity identity context-only and removed the per-alias partition.

Source specification: `proposal_kv_keeper.md`.

This file tracks the implementation plan, current state, and decisions that come
up while the specification is being applied. Update it after every phase.

## Scope

1. Prefix-affinity smart routing: canonical fingerprint, adaptive anchor, HMAC
   hash, bounded in-process store, queue-imbalance diversion, resource-wait
   policy, scheduler/fallback integration.
2. Per-model parallel admission: `maxConcurrentRequests` and
   `sessionResources`, model-aware admission, base-once/session-per-task
   reservation, readiness history, queue-state exposure.
3. Configuration descriptors, shipped configuration, tests, documentation.

## Resolved Decisions

- Affinity enabled by default in code and shipped config.
- Diverted mapping moves to the alternative immediately.
- Queue depth means queued tasks only; `active_count` is diagnostic.
- Session field is `sessionResources` (same shape as `resources`).
- Store is in-process in v1.
- W15: affinity depends only on the request context; alias and client keys do
  not partition the store. `max_entries_per_alias` and `honor_prompt_cache_key`
  are deprecated and ignored.
- Warm resource wait: configurable, default 60 s.
- Cold resource wait: default 0 s (immediate diversion).
- Availability sampling happens on candidate evaluation only.
- Anti-flapping ratio: 0.5 over the 10-minute history window.
- Queue and resource thresholds are global in v1.

## Phases

### Phase 1: Configuration and data model

Status: complete.

- Add `routing.kv_affinity` keys to `config.descriptor.json5`.
- Add `maxConcurrentRequests`, `sessionResources`, and `cache_group` to the model
  entry and `cache_group` to `provider_ref`.
- Add the routing block to `config.json5` and `config.json5.example`.
- Add validation tests for the descriptor.

### Phase 2: KV-affinity core

Status: complete.

- New `core/kv_affinity.py`:
  - canonical request context;
  - adaptive anchor length and ladder quantization;
  - HMAC-SHA256 fingerprint;
  - bounded store with TTL and candidate invalidation;
  - readiness tracker with transition history and wait decision.
- Unit tests for canonicalization, anchor policy, store bounds, and readiness.

### Phase 3: SmartRouter integration

Status: complete.

- Add affinity lookup and metadata to `SmartRouter.resolve_route`.
- Evaluate all candidates when affinity is enabled.
- Select preferred candidate, queue diversion, resource wait, and cold/warm
  timeout behavior.
- Record affinity and readiness on every selection.
- Add optional model queue-state callback.

### Phase 4: Scheduler and fallback integration

Status: complete.

- Pass the store and tracker into the scheduler.
- `_refresh_smart_route_for_dispatch` must honor affinity.
- `_fallback_smart_route_after_execution_failure` must invalidate the failed
  candidate and remember the new one.
- Invalidate on known unloads and provider failures.

### Phase 5: Session resources and admission

Status: complete.

- Add active-count registry and model-aware admission to `core/resources.py`.
- Add base/session reservation and release paths.
- Wire the scheduler dispatch and release paths.
- Extend queue-state with `model_loaded`, `admission`, `active_count`,
  `max_concurrent_requests`, `session_resources`, and `memory_slots`.
- Tests for concurrency, memory caps, warm reuse, and force-unload.

### Phase 6: Verification and documentation

Status: complete.

- Run the full unittest suite.
- Add `test_kv_affinity.py` and session-admission tests.
- Update `TEST.md`, `README.md` only if the behavior is user-visible.
- Update `memory/context.md` and `memory/history.md`.
- Update this plan with final state and any deviations.

Completed: `test_kv_affinity`, `test_kv_affinity_routing` and
`test_session_resources` added; `README.md`, `TEST.md` and `DOCUMENTS.MD`
updated. The full unittest discovery run passes (663 tests).

## Important Notes Discovered During Work

- `core/endpoints/endpoint_openaix.py` inherits `_smart_router` from
  `endpoint_ollama.py`; the factory lives in the Ollama endpoint.
- The scheduler re-resolves every queued smart route before dispatch, so
  affinity must be passed through that path or it will be silently undone.
- The current worker trims >20 messages to system + last 18
  (`workers/agent/openaix/app.py`), which breaks conversational prefix reuse.
  This is a documented limitation, not part of this implementation.
- The current tree has no explicit per-model concurrency cap; the field is
  introduced by this work.
- `maxConcurrentRequests == 1` must keep the legacy full-footprint reservation
  path. Session accounting applies only above 1, while active counting is still
  needed to report `concurrency` instead of a memory shortage.
- A loaded model's own base footprint must not count as a blocker; only the
  incremental session cost matters.
- The canonical context is an append-stable line stream, not one JSON envelope:
  a growing `messages` array must not rewrite the closing brackets of the prefix
  that was hashed earlier.
- The anchor ladder had to be denser than the first proposal (256-byte steps up
  to 4 KiB) or a 90% anchor of a 4 KiB context rounded down to 50%.
- `SmartRouter` evaluates all candidates only when affinity is enabled; with the
  feature off it keeps the old first-immediate early return.
- A resource-timeout diversion only moves to an alternative that can run now.
  If none can, the preferred candidate is kept.
- `maxConcurrentRequests == 1` keeps the legacy reservation path but still counts
  active requests, so "busy" is reported as `concurrency`, not a memory
  shortage.
- `Resources.reserve_model_start` reserves the base once and sessions
  incrementally; `release_model_session` returns the base to soft tracking on
  the last release. A failed llama.cpp start does not retain the base.
- Unload events invalidate affinity through a `Resources` candidate-invalidator
  callback wired by `Core`.
- The scheduler refresh path uses the same store and readiness tracker, so it
  cannot silently undo affinity. Execution fallback forgets the failed candidate
  first.
