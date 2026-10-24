# aidir — Scheduling, resources and smart routing

> Satellite document of [SPEC.md](../SPEC.md). See
> [§6.4 Scheduler](../SPEC.md#64-scheduler) –
> [§6.6 SmartRouter and KV affinity](../SPEC.md#66-smartrouter-and-kv-affinity)
> there for the architecture context.
>
> This document describes the task lifecycle, the queue semantics, the scheduler
> algorithm, the resource model, the smart router and the KV affinity mechanism in
> detail.

---

## 1. The task lifecycle

### 1.1 States and transitions

```
                    ┌──────────── on_task_added / add_task ─────────────┐
                    ▼                                                     │
              [created] ──────────────────▶ [queued] ──mark_running──▶ [running]
                                                 │                        │
                                                 │                        ├─ mark_completed ─▶ [completed]
                                                 │                        ├─ mark_failed    ─▶ [failed]
                                                 │                        └─ mark_canceled  ─▶ [canceled]
                                                 └─ mark_canceled ─────────▶ [canceled]
```

`created` is the state a task has between its construction and its enqueue; it is
never observed in Redis.

Terminal states: `completed`, `failed`, `canceled`. All three push a `None` sentinel
into the task's chunk queue and set its done event.

### 1.2 Who performs each transition

| Transition | Performed by | Notes |
|---|---|---|
| → `queued` | `QueueManager.add_task` | Sets `status`, `updated_at`; a ZSET add plus a HASH write in one transaction |
| → `running` | `QueueManager.mark_running` | Sets `started_at`, `worker_id`; clears `error` and `upstream_response` |
| → `completed` | `QueueManager.mark_completed` | Stores `result` and `upstream_response`; writes the terminal audit record |
| → `failed` | `QueueManager.mark_failed` | Stores `error` and `error_code` |
| → `canceled` | `QueueManager.mark_canceled` | Used by the graceful shutdown, the client disconnect path, and the hook cancel |

### 1.3 The failure states a client can observe

| `error.code` | HTTP | Meaning |
|---|---|---|
| `QUEUE_TIMEOUT` | 504 | The task waited longer than `tasks.queue_timeout` before its first run |
| `TIMEOUT` | 504 | The task ran longer than `tasks.run_timeout` |
| `UPSTREAM_TIMEOUT` | 504 | One upstream HTTP call exceeded its timeout |
| `UPSTREAM_UNREACHABLE` | 502 | The upstream could not be reached |
| `UPSTREAM_ERROR` | 502 (or the upstream's own status) | The upstream returned an error |
| `UPSTREAM_INVALID_JSON` | 502 | The upstream returned a body that is not JSON |
| `EXCEPTION` | 502 | The worker raised |
| `HOOK_CANCELED` | 502 | A hook canceled the task |
| `SERVICE_RESTARTED` | — | The process restarted while the task was running |
| `TASK_PARENT_CHAIN_TOO_DEEP` | — | The parent chain exceeded `tasks.task_parent_chain_max_depth` |
| `RESTART_IN_PROGRESS` | 503 | The service is draining for a restart |

### 1.4 Persistence

`Task.to_redis_hash()` writes every field as a string. `from_redis_hash` reverses it
and dispatches on `type`; an unknown type raises `ValueError`, which is what makes
the scheduler quarantine a corrupt member.

**Not persisted:** `fallbacks`, `on_reject`, `next_retry_at`. A task that round-trips
through Redis loses its fallback chain.

**Not persisted either:** the in-memory `asyncio.Event` and `asyncio.Queue` — they
are recreated when the task is deserialized.

---

## 2. The queue

### 2.1 Redis structures

| Key | Type | Content |
|---|---|---|
| `{ns}:queue:agent` | ZSET | member = task id, score = priority |
| `{ns}:queue:tool` | ZSET | the same |
| `{ns}:task:{task_id}` | HASH | the full task |
| `{ns}:queue:quarantine` | HASH | task id → `{task_type, reason}` |

### 2.2 Priority

| Constant | Value | Meaning |
|---|---|---|
| `PRIORITY_URGENT` | 0 | Highest |
| `PRIORITY_NORMAL` | 5 | The default |
| `PRIORITY_IDLE` | 10 | Lowest |

**Lower is more urgent**, so the ZSET score *is* the priority and `ZPOPMIN` pops the
most urgent task.

Within one priority class, Redis orders members lexicographically — that is, by task
id (a uuid4). There is **no FIFO guarantee**.

### 2.3 Type separation

Two independent ZSETs exist. The scheduler drains the `agent` queue completely
before it touches the `tool` queue in each pass, so **task type outranks priority
across types**: under sustained agent load, a tool task can starve.

### 2.4 Enqueue and dequeue

`add_task(task, is_retry=False)`:

```python
task.status = STATUS_QUEUED
task.updated_at = now
pipe = redis.pipeline(transaction=True)
pipe.zadd(self._q(task.type), {task.id: task.priority})
pipe.hset(self._tk(task.id), mapping=task.to_redis_hash())
await pipe.execute()
```

The operation is atomic. The in-memory `self._tasks[task.id] = task` assignment
happens after the transaction, so the in-memory object is the authority for the live
endpoints while the HASH is the durable record.

`pop_next(task_type)` uses `ZPOPMIN count=1` — an atomic pop.

### 2.5 Startup recovery

`QueueManager.recover_startup_tasks()` runs once, right after the queue is created.

| Situation | Action |
|---|---|
| A ZSET member whose HASH is missing or unparseable | Removed from the ZSET, moved to `{ns}:queue:quarantine` with a reason |
| A ZSET member whose status is not `queued` | Same |
| A HASH with `status=queued` that is in no ZSET | Re-added to its ZSET |
| A HASH with `status=running` | **Not replayed.** Marked `failed` with `{"code": "SERVICE_RESTARTED"}` and removed from every ZSET |

The result is logged as `{"recovered": n, "quarantined": n, "restarted": n}`.

### 2.6 Deletion and retention

`delete_task(task_id)`:

- an **external** task (created by an endpoint) keeps its Redis HASH; only the
  in-memory entry is dropped and the task is removed from the ZSETs;
- an **internal** task is deleted from Redis entirely.

**No TTL is ever set on a task hash.** External hashes are reaped by the cron job
`cleanup_expired_tasks`, which scans `{ns}:task:*` and removes those with
`external == "1"`, a terminal status, and `finished_at` older than
`tasks.external_task_live`. Queued and running tasks are never removed by that job.

### 2.7 Queue-state aggregation

Two functions compute the counters the queue-state endpoints expose:

`get_resource_queue_state(requirements, priority)` scans both ZSETs and keeps the
tasks whose serialized `resource_requirements` match **exactly** (JSON with sorted
keys). `get_model_queue_state(provider_id, model_id, priority)` filters by
`route_provider_id` and `model_id` instead.

Both return:

```python
{"queued_count_total": int,
 "queued_count_below_priority": int,   # the tasks with a HIGHER priority number
 "priority_counts": [ {"priority": p, "count": n}, ... ]}
```

Both are `O(queued tasks)` with one `HGETALL` per task — the main scalability hot
spot of the queue.

---

## 3. The scheduler

`core/scheduler.py`. One asyncio task, `Scheduler.run` (`core/scheduler.py:73-251`).

### 3.1 The loop

```python
while self._running:
    dispatched = False
    for task_type in ("agent", "tool"):
        deferred = []
        task_id = await queue.pop_next(task_type)
        while task_id is not None:
            task = queue.get_task(task_id)
            ... the gates ...
            background = asyncio.create_task(self._run_task(task, worker))
            self._active_runs.add(background)
            background.add_done_callback(self._on_run_done)
            dispatched = True
            task_id = await queue.pop_next(task_type)
        for d in deferred:
            await queue.add_task(d)
    if not dispatched:
        await asyncio.wait_for(self._wake.wait(), timeout=1.0)
        self._wake.clear()
```

The loop drains the **whole** queue for a type before sleeping, so a blocked task
never starves a later task of the same type. The deferred tasks are re-queued at the
end of the pass.

The `self._wake` event lets another component interrupt the sleep when something
changes (a new task, a cancellation).

### 3.2 The dispatch gates

The gates are evaluated in this order. A failure at any gate **defers** the task.

| # | Gate | Line | On failure |
|---|---|---|---|
| 1 | The task is still in the in-memory registry | `scheduler.py:85-89` | Warn and skip |
| 2 | The queue timeout has not expired | `scheduler.py:91-93` | `mark_failed(QUEUE_TIMEOUT)` |
| 3 | `next_retry_at` is in the past | `scheduler.py:96-99` | Defer |
| 4 | A worker exists for the task type | `scheduler.py:101-108` | Defer, warn "No worker for task …" |
| 5 | The smart route is refreshed | `scheduler.py:110` | The existing route is kept |
| 6 | The resource requirements are resolved | `scheduler.py:112` | — |
| 7 | The measured availability is refreshed | `scheduler.py:115-120` | Defer +1 s, "resource availability probe failed" |
| 8 | A remote aidir reports it can run the model now | `scheduler.py:121-130` | Defer +1 s |
| 9 | Admission (the parallel / session path) | `scheduler.py:131-178` | Defer +1 s |
| 10 | Legacy admission (reuse / force-unload / reserve) | `scheduler.py:179-223` | Defer +1 s or +5 s |

**Deferral** means `task.next_retry_at = time.time() + N` followed by
`queue.add_task(task)`. `N` is 1 second normally and 5 seconds when a force-unload
did not free enough space.

### 3.3 Worker selection

`Scheduler._select_worker` (`core/scheduler.py:345-360`):

1. If `task.worker_id` is set and that worker exists and is enabled → use it.
2. Otherwise the **first** enabled worker whose `task_type` matches → use it.
3. Otherwise `None` → the task is re-queued with a warning.

Step 2 is dictionary-order dependent, so it is not deterministic across different
worker load orders.

### 3.4 Provider and model resolution

| Item | Source |
|---|---|
| The provider | `task.config["route"]["resolved_provider"]`, else the worker's configured `provider` |
| The model | `task.payload["model"]` (after the route refresh may have overwritten it with the concrete model) |
| The worker | `route.resolved_worker` wins; otherwise the provider `api` decides: `llama-cpp` → `call_llama_cpp`, `openai` → `call_openai`, `anthropic` → `call_anthropic`, `opencode-go` → `call_opencode_go`, `openaix` → `openaix`. The candidate is used only if the worker id exists in `workers.items` |

### 3.5 Resource requirement resolution

`Scheduler._resolve_resource_requirements` (`core/scheduler.py:786-818`):

1. An explicit `task.resource_requirements` wins.
2. Otherwise the **sum** of the worker's `resources` block and the resolved provider
   model's `resources` block.

The result is cached back onto the task.

### 3.6 The two admission branches

#### Parallel models (`maxConcurrentRequests > 1`)

1. `resources.evaluate_model_admission(reqs, model_id, provider_id)`.
2. If the admission is `waiting_unload` → `force_unload_for(needed, keep_model_id,
   keep_provider_id)`, then re-evaluate.
3. Still not admissible → defer +1 s.
4. `resources.reserve_model_start(...)` — atomic: it re-evaluates the admission under
   the admission lock, reserves the base if absent, and reserves one session.
5. The reservation was refused (the resources changed between the evaluation and the
   reservation) → defer +1 s.

#### Legacy models (`maxConcurrentRequests <= 1`)

1. `resources.check_available(local_reqs)`.
2. If not available:
   - `check_available_for_reuse(...)` → the warm model is reused, no unload;
   - else `check_available_after_unload(...)` → `force_unload_for(...)`, then
     **re-check**; still short → defer +5 s ("resource unload did not free enough
     space");
   - else defer +1 s ("insufficient resources").
3. `resources.reserve_if_available_or_reuse(local_reqs, consumer_id=f"{task.id}:{worker.id}",
   model_id, provider_id)` — atomic under the admission lock. A refusal → defer +1 s.

The consumer id is `{task_id}:{worker_id}`.

### 3.7 Task execution

`Scheduler._run_task` (`core/scheduler.py:432-556`):

1. `mark_running`.
2. Set `task.route_provider_id`.
3. Reserve, if not already pre-reserved.
4. `mark_model_started`, when there is no session admission.
5. Hooks:
   - `before_llm_request` (agent tasks only),
   - `before_consumer_delivery` before and after the delivery,
   - `llm_response_complete` on success and on failure.
6. `await asyncio.wait_for(worker.execute(task, emit_chunk=...), timeout=task.run_timeout or None)`.
7. On success: store the result and the upstream response, reset `retry_attempt`,
   `fallback_index` and `next_retry_at`, `mark_completed`.
8. On failure: store the error, try the smart-route fallback, detect a local-server
   start failure, apply the reject policy.
9. `finally`: release the session (or the plain reservation), then `mark_model_finished`.

### 3.8 The retry and fallback policy

`Scheduler._handle_reject` (`core/scheduler.py:1152-1211`).

The **reason** is derived from the error code:

| Error codes | Reason |
|---|---|
| `UPSTREAM_UNREACHABLE`, `CONNECT_ERROR`, `CONNECTION_ERROR`, `UNAVAILABLE` | `unavailable` |
| `UPSTREAM_BUSY`, `RESOURCE_BUSY`, `BUSY` | `busy` |
| anything else | `error` |

The **policy** is `workers.items.<id>.on_reject.<reason>` merged over
`task.on_reject`, with `retry_count`, `retry_period` and `fallbacks` falling back to
the worker level and then to the task level.

| Action | Behavior |
|---|---|
| `retry` | Re-queue while `retry_attempt < retry_count`; the default is `cancel` |
| `fallback` | Switch `task.worker_id` to the next entry in `fallbacks` and re-queue; when the chain is exhausted, fail |
| `fallback-retry` | The same, but the chain restarts while `retry_attempt < retry_count` |
| `cancel` | Fail now |

**A re-queue failure never masks the original error.** The failure is logged and
`False` is returned, so the client sees the real upstream error rather than an
aidir-internal exception.

### 3.9 The smart-route fallback

`Scheduler._fallback_smart_route_after_execution_failure`
(`core/scheduler.py:558-673`):

1. Only on `error.code == "UPSTREAM_ERROR"`, and only when the route's `selection` is
   `smart_route` and the requested provider's `api` is `smart`.
2. The failed candidate is forgotten in the affinity store and its readiness history
   is cleared.
3. The route is re-resolved with `excluded_candidates = {(failed_provider,
   failed_model)}`.
4. If a different candidate is found, the task's route, payload model, resource
   requirements, provider, model and worker are rewritten and the task is re-queued.
5. If the re-queue fails, the **previous state is restored exactly**, so the
   original `UPSTREAM_ERROR` survives.

### 3.10 The pre-dispatch route refresh

`Scheduler._refresh_smart_route_for_dispatch` (`core/scheduler.py:710-784`) re-resolves
a queued smart route before dispatch, so a busy-fallback selection can move to a
newly available candidate. When the resolution changes, the route, the payload model,
the resource requirements and the worker id are updated.

A `SmartRouteError` here is swallowed and the existing route is kept.

**Known gap:** the scheduler's router is not given the OpenAI-family probe callback,
so an `openai` / `opencode-go` / `anthropic` candidate is classified
`provider_down` in this path. The route survives, but the candidate's affinity
record is forgotten and its readiness history is polluted with `provider_down`
transitions.

### 3.11 Cancellation

| Operation | Effect |
|---|---|
| `Scheduler.stop()` | Clears `_running`, wakes the loop |
| `wait_for_active_tasks(timeout)` | Waits on the idle event; logs the remaining labels on timeout |
| `cancel_active_tasks(timeout=5.0)` | Cancels every task in `_active_runs`, gathers them, logs BEGIN/SUCCESS/TIMEOUT |
| `cancel_task(task_id, timeout=5.0)` | Cancels one task, found by the `task:<id>:<worker>` name prefix |
| `asyncio.CancelledError` inside `_run_task` | `mark_canceled(task)` and the exception is re-raised |

### 3.12 Diagnostics

The scheduler emits, among others:

- "Scheduler started" / "Scheduler run loop exited";
- "Scheduler stop requested: _running=… active_tasks=…";
- one BEGIN/SUCCESS/TIMEOUT line per cancellation;
- one deferral warning per reason, each naming the reason;
- "smart route refreshed before dispatch: {old} -> {new}";
- "smart execution fallback: {failed} -> {next}";
- the queue and run timeout messages, which name the parameter and its source:

```
Task queue timeout exceeded after {elapsed:.2f}s (limit={limit}s, parameter=task.queue_timeout, source=tasks.queue_timeout / TASK_QUEUE_TIMEOUT_SECONDS)
Task run timeout exceeded after {elapsed:.2f}s (limit={limit}s, parameter=task.run_timeout, source=request.timeout | tasks.run_timeout / TASK_RUN_TIMEOUT_SECONDS)
```

---

## 4. The resource model

### 4.1 One resource

`core/resource.py`.

| Field | Meaning |
|---|---|
| `id` | The unique key |
| `type` | `cuda`, `aidir`, `rocm`, `metal`, `cpu` — informational only |
| `limits` | `{metric: amount}`, the hard capacity |
| `units` | `{metric: unit}` |
| `used` | The hard reservations currently held |
| `consumers` | `consumer_id → {metric: amount}` |
| `_soft_used` | The models still resident after their task ended |
| `alive_time` | Seconds a model stays resident after release; **the memory still counts** |
| `keep_alive`, `keep_alive_period` | The keep-warm window and the ping interval |
| `provider` | The provider id used for the unload / keep-alive call |
| `monitoring` | The sensors and the poll interval |
| `availability` | The command or peer probe |
| `telemetry` | The sensor ids safe to publish |
| `use` | The runtime enable/disable flag |

### 4.2 Hard and soft reservations

**Hard** — an in-flight task's reservation, keyed by `consumer_id`:

| Method | Effect |
|---|---|
| `reserve_blind` | Increments `used` and `consumers`; if a matching **warm soft consumer** exists for the same model, it is cleared first, converting the soft occupancy into a hard reservation without double counting |
| `reserve_active` | Increments without touching the soft consumers — used for the per-request *session* reservation on top of an already-loaded base |
| `release` | Decrements, then converts the freed amount into a soft consumer when `alive_time > 0` or `keep_alive > 0` and the model id is known |

**Soft** — a model still resident in VRAM:

```python
{"consumer_id", "resources", "released_at", "expires_at",
 "model_id", "provider_id", "persistent"}
```

A soft entry is active while it is `persistent`, or `now < expires_at`, or
`now - released_at < alive_time`. `persistent=True` means "never expires" and is
used for the aidir-owned llama.cpp servers.

The sum of the active soft amounts is what makes an idle model still block
admission.

### 4.3 Capacity accounting

Two tiers:

| Source | How the free capacity is computed | On a failed probe |
|---|---|---|
| `estimated` (default) | `limit - used - soft_used` | n/a |
| `command` | The measured value from the availability command | **Fail closed** — the resource is unavailable |
| `peer` | The measured value from the peer aidir | **Fail closed** |

**The generation trick.** Each observation increments
`_availability_generation`. Each reservation records the generation at which it was
made. The observed free capacity subtracts only the reservations made **after** the
observation. A stale measurement is therefore not double-discounted by old
reservations, and new reservations are still discounted correctly.

**Fail closed.** `Resource.is_available` returns `False` when the metric has no
observed value but the resource has a command probe or an active peer source.

**Warm reuse.** `Resource.is_available_for_reuse` answers "can the same warm model
serve this request without an unload?" — a matching soft consumer must exist, its
resources must cover the requirement, and (with a command or peer source) the probe
must be `ok`. Otherwise the check computes
`used + (soft - matched) + need <= limit`, i.e. the model's own footprint is not
counted as a blocker.

### 4.4 Admission

`Resources.evaluate_model_admission(reqs, model_id, provider_id)` returns:

```python
{"admission": "ready" | "concurrency" | "waiting_unload" | "base_memory" | "session_memory",
 "admissible_now": bool,
 "admissible_after_unload": bool,
 "model_loaded": bool,
 "active_count": int,
 "max_concurrent_requests": int,
 "session_resources": {...},
 "free": {...}, "needed": {...},
 "resource_ready": bool, "resource_ready_after_unload": bool}
```

Algorithm:

1. The requirements are filtered through `local_admission_requirements`: a resource
   whose availability source is `peer` is **not** reserved locally — the peer aidir
   owns it.
2. `active = model_active_count(provider, model)`.
3. If `active >= max_concurrent_requests` → `admission = "concurrency"`, not
   admissible now.
4. `model_loaded` = the model has a `base:<provider>:<model>` consumer, or a matching
   soft consumer on every required resource.
5. `maxConcurrentRequests <= 1`: `needed = reqs` when the model is not loaded;
   `ready = loaded or check_available(reqs)`;
   `after = ready or check_available_after_unload(reqs)`.
6. `maxConcurrentRequests > 1`: `needed = session` when the model is loaded, else
   `merge(reqs, session)`; the "after unload" test excludes the model's own base.

### 4.5 Base and session reservations

`Resources.reserve_model_start(reqs, session, model_id, provider_id)` performs the
atomic reservation under a global `asyncio.Lock`:

1. Re-evaluate the admission; if it is not admissible, return the decision unchanged.
2. Reserve the base under `base:<provider>:<model>` if it is absent.
3. Reserve the session under the task's consumer id.
4. `mark_model_started`.

`Resources.release_model_session(...)` releases the session and decrements the active
count. **Only on the last release** does the base return to soft tracking — with
`persistent=True` for the llama.cpp providers when the base should be retained.

`Resources.memory_slots(model_id, provider_id)` computes the effective cap:

```
min( maxConcurrentRequests - active ,
     floor(free_metric / session_metric) for every metric )
```

It returns `None` when `maxConcurrentRequests <= 1` or no session requirements are
configured.

### 4.6 Configuration validation

`Resources.validate_model_parallel_config` raises at boot when
`maxConcurrentRequests > 1` and the model declares local `resources` but a metric
has no positive `sessionResources` entry. `Core.start` turns that into a
`RuntimeError`, so the process refuses to start rather than mis-accounting memory.

### 4.7 Unload

| Path | Trigger | Behavior |
|---|---|---|
| `force_unload_for(reqs, full_config, keep_model_id, keep_provider_id)` | The scheduler needs VRAM | For each required resource with `alive_time > 0`: iterate the active soft consumers, skip the model being kept, call the provider's unload API; on success, invalidate the affinity candidate and delete the Redis activity record; clear the soft entry. Returns `False` if any unload failed |
| `force_unload_all(full_config)` | The service shuts down | Unload every tracked idle model |
| `force_release(rid)` | The WebUI "release resource", or the `release_resource` sensor reaction | **Refuse** when there are active consumers; otherwise unload the idle models and report which |
| `clear_soft_consumers_for_providers(provider_ids)` | A managed server stopped | Remove the soft entries and invalidate the affinity |

The provider unload API:

| Provider `api` | The call |
|---|---|
| `llama-cpp` | `LocalServerManager.stop(provider_id)` |
| `ollama` | `POST {baseUrl}/api/generate {"model": …, "keep_alive": 0}`, 10 s timeout |
| anything else | No call; only the bookkeeping is cleared |

Every unload path calls the **candidate invalidator**, which is wired to
`KvAffinityStore.forget_candidate` and `ModelReadinessTracker.clear_candidate`.

### 4.8 Idle occupancy across a restart

`Resources.restore_activity_consumers(full_config)` scans
`{ns}:resource:*:activity:*` and rebuilds the soft consumers for the non-llama.cpp
providers, skipping the records whose retention window has expired.
`Resources.restore_owned_llama_cpp_consumers(full_config, manager)` re-creates the
persistent soft consumers for the models whose llama.cpp PID record is still valid.

The activity records carry `released_at`, `last_used_at`, `last_ping_at`,
`keep_alive`, `keep_alive_period` and `provider`. The keep-alive window is measured
from `last_used_at`, and a keep-alive **ping does not move it** — a ping cannot keep
a model warm forever.

---

## 5. The smart router

`core/smart_router.py`.

### 5.1 The strategies

Both **`first_available`** and **`direct`** are implemented. `direct` requires
exactly one candidate and resolves to it under the normal admission rules. A
different candidate count raises `SmartRouteError(code="INVALID_ROUTING_CONFIG",
status_code=400)`.

### 5.2 The two factories

| Factory | Where | Used for |
|---|---|---|
| `Endpoint_ollama._smart_router` | `core/endpoints/endpoint_ollama.py:570-642` | The endpoint's own resolution |
| `Scheduler._make_smart_router` | `core/scheduler.py:853-912` | The pre-dispatch refresh and the post-failure fallback |

Both are given the **same** affinity store and readiness tracker, so the two
decisions cannot disagree.

### 5.3 Candidate evaluation

For each candidate in `items[]` the router computes `provider`, `model`, `index`,
`fallback_prio` (from the item, defaulting to the index) and `cache_group` (the
item, then the model config, then the model id).

The probe branches, in order:

1. **Capability filter** — a candidate that the caller is not allowed to use (an
   embedding request against a chat-only model) is marked
   `routing_eligible: False`, `admission: "resource_disabled"`.
2. **OpenAI-family providers** (`openai`, `opencode-go`, `anthropic`) — a
   `GET {baseUrl}/v1/models` must list the model. The result is
   `can_run_now: True`, `admission: "ready"`, `model_loaded: True`.
3. **The local branch** — when the model declares `resources`:
   - the queue depth comes from `get_model_queue_state`, falling back to
     `get_local_queue_state`;
   - `blocked_by_same_or_higher = queued_count_total - queued_count_below_priority`;
   - `resource_ready`, `resource_ready_after_unload` and `resource_reusable` come
     from the resource registry;
   - Ollama and llama.cpp additionally probe the provider over HTTP; a failed probe
     → `routing_eligible: False`, `admission: "provider_down"`;
   - when `evaluate_model_admission` is available, its decision is carried through
     verbatim, and `can_run_now = admissible_now and blocked_by_same_or_higher == 0`.
4. **A non-OpenAIx remote without local requirements** — `routing_eligible: False`,
   `admission: "provider_down"`.
5. **A remote OpenAIx** — `probe_remote_model_queue_state` asks the peer for its
   queue state; the peer supplies `can_run_now`, `admission`, `model_loaded`,
   `active_count`, `max_concurrent_requests`, `session_resources`, `free` and
   `needed`.

The probe timeout comes from the item's `request_timeout_ms` (default 1500 ms).

### 5.4 The selection

**Without affinity** the router returns the **first** candidate with `can_run_now`.
If none can run now, it picks the `busy_fallback` candidate with the smallest
`(fallback_prio, index)`. If there is no eligible candidate at all, it raises
`ROUTING_FAILED` (HTTP 503).

**With affinity** (the default) *every* candidate is evaluated first, and then
`_select_with_affinity` applies the affinity policy. Only when the affinity yields
nothing does the router fall back to the `can_run_now` order and then to the busy
fallback.

The alternative ranking `_best_alternative` sorts by
`(0 if can_run_now else 1, queue_depth, fallback_prio, index)`.

### 5.5 The route object

`task.config["route"]` is the audit trail of the decision:

```jsonc
{
  "requested_model": "...",          // what the client asked for
  "requested_alias": "...",
  "requested_provider": "smart",
  "resolved_provider": "ollama_local",
  "resolved_model": "...",
  "selection": "smart_route",         // or "alias" / "model_id"
  "strategy": "first_available",
  "selection_reason": "affinity" | "affinity_wait_queue" | "affinity_wait_resource"
                    | "affinity_wait_no_alternative" | "affinity_queue_diverted"
                    | "affinity_resource_timeout" | "immediate" | "busy_fallback",
  "candidate_index": 0,
  "fallback_prio": 10,
  "candidate_probes": [ { "index", "provider", "model", "probe_ok", "can_run_now",
                          "probe_source", "probe_latency_ms", "fallback_prio",
                          "admission", "model_loaded", "queue_depth",
                          "active_count", "max_concurrent_requests",
                          "queued_count_total", "queued_count_below_priority",
                          "probe_error" } ],
  "kv_affinity": { ... },
  "resolved_protocol": "openai" | "ollama" | "anthropic-messages",
  "resolved_worker": "openaix" | "call_llama_cpp" | ...
}
```

`task.config` also carries `kv_client_key`, `incoming_bearer_token`, `route_trace`
and `request_kind`.

---

## 6. KV affinity

`core/kv_affinity.py`.

### 6.1 The canonical context

`canonical_context(payload, request_kind)` builds an **append-stable line
stream**:

1. The version tag `kv1` and the request kind.
2. The prompt-shaping keys, in a fixed order: `system`, `tools`, `tool_choice`,
   `response_format`, `format`, `think`, `raw`, `context_builder`, `template`.
3. One `message:`-prefixed line per message.
4. The `prompt` / `input` values.
5. The remaining keys, sorted.

The excluded keys (`_NON_PROMPT_FIELDS`) are the ones that do not affect the
prefix: `model`, `stream`, `worker`, `priority`, every `*_timeout` variant,
`keep_alive`, `log`, `envid`, `save_llm_request`, `opencode_session`,
`request_kind`, `prompt_cache_key`, `options` and every sampling parameter
(`temperature`, `top_p`, `seed`, `max_tokens`, `stop`, …). Keys starting with `_`
are skipped entirely — that is how the internal `_aidir_kv_key` hint stays out of
the hashed content.

Serialization: `json.dumps(..., ensure_ascii=False, sort_keys=True,
separators=(",", ":"), default=str)`.

**Why the line stream matters.** Because each message is its own line, a growing
conversation **appends** bytes rather than rewriting the closing brackets of an
already-hashed prefix. That is exactly what keeps an older anchor valid.

### 6.2 The anchor policy

`AnchorPolicy.anchor_bytes(total_bytes)`:

| Context size | Anchor fraction |
|---|---|
| ≤ `short_context_bytes` (10 KiB) | `short_fraction` (0.90) |
| ≤ `medium_context_bytes` (20 KiB) | `medium_fraction` (0.50) |
| ≤ `long_context_bytes` (100 KiB) | Linear interpolation from 0.50 to 0.10 |
| > `long_context_bytes` | `long_fraction` (0.10) |

The result is clamped to `[min_anchor_bytes=512, max_anchor_bytes=32768]`, capped at
the context length, and quantized **down** onto the ladder.

The default ladder is dense: 256 → 32768 in 256/512-byte steps. A sparser ladder
rounded a 4 KiB context's 90 % anchor down to 50 %, which was too lossy.

### 6.3 The hash

```
message = b"kv2\x00" + request_kind + b"\x00" + prefix
digest  = HMAC-SHA256(secret, message)   if a secret is configured
        = SHA256(message)                otherwise
return  = digest[:16].hex()              # 128 bits
```

The secret comes from `routing.kv_affinity._secret` or `AIDIR_KV_AFFINITY_SECRET`.
Without a secret the store degrades to plain SHA-256 and is documented as
internal-only.

The alias, endpoint and `prompt_cache_key` are **not** part of the identity.
Affinity depends only on the request context, so the same context arriving
through a different alias or client uses the same record. The
`honor_prompt_cache_key` and `max_entries_per_alias` keys are deprecated and
ignored.

**The router never stores prompt text.** It stores a truncated HMAC and a byte
length.

### 6.4 The store

`KvAffinityStore`:

| Property | Default |
|---|---|
| `ttl_seconds` | 3600 |
| `max_entries` | 20000 |

The keys are `(anchor_bytes, hash)` in one global store; the LRU is global.
`max_entries_per_alias` is accepted for compatibility and ignored.

**Lookup** (`lookup(fingerprint)`): for every stored anchor length `a` that is
≤ `min(stored_len, current_anchor)`, the router recomputes the hash of the **first
`a` bytes of the current canonical context** and compares it with the stored hash.
The **longest** match wins; ties break on `last_used`. This handles both directions:
a grown context matches an older short anchor, and a shrunk context skips the
longer stored anchors.

**Remember** (`remember(fingerprint, provider, model, ...)`): upsert at the current
quantized anchor; refresh `last_used`, `expires_at`, `cache_group`, `provider`,
`model` and `last_reason`. The TTL is **not** extended beyond `ttl_seconds`.

**Invalidation**:

| Method | Effect |
|---|---|
| `forget_candidate(provider, model)` | Removes every record pointing at that executor |
| `forget_prefix(fingerprint)` | Removes exactly one record |

`stats()` exposes only `{enabled, records, ttl_seconds}` — never prompt content.

### 6.5 The readiness tracker

`ModelReadinessTracker` keeps, per `(provider, model)`:

- `blocked_since` — the start of the current contiguous resource-blocked run;
- `transitions` — `{ts, state, admission, model_loaded, free, needed}` entries.

The state is:

| Admission | State |
|---|---|
| `session_memory`, `base_memory`, `waiting_unload` | `blocked` |
| `ready` | `ready` |
| anything else | `other` |

`blocked_since` is set on the first blocked sample and reset to `None` on any
non-blocked sample. Changing **between** the three blocked reasons does **not**
reset it — only the transition into a blocked run and out of it matter.

A transition is appended only when the admission reason or `model_loaded` changed.

### 6.6 The affinity policy

`_select_with_affinity` (`core/smart_router.py:302-355`). The preferred candidate
must:

- still exist among the evaluated candidates,
- have `probe_ok`,
- be `routing_eligible`,
- and have the **same `cache_group`** as the affinity record.

A mismatch in the cache group forgets the record.

| The preferred candidate's admission | Behavior | `selection_reason` |
|---|---|---|
| `ready` | Keep it, unless the queue-imbalance test fires | `affinity` |
| `concurrency` | Keep waiting on it, unless the queue-imbalance test fires | `affinity_wait_queue` |
| `session_memory` / `base_memory` / `waiting_unload` | Keep waiting until the resource-wait timer expires | `affinity_wait_resource` |
| `provider_down` / `resource_disabled` | Treated as a miss; the record is forgotten | — |

`prefer_affinity_over_immediate` (default `true`) decides whether the router may
wait on a preferred candidate that cannot start immediately. When it is `false`, such
a candidate is skipped entirely.

### 6.7 The queue-imbalance diversion

`_queue_overloaded` (`core/smart_router.py:368-394`) diverts **only when both**
conditions hold:

```
Q_pref - Q_min > max_queue_diff_len          # default 8
Q_pref     >= Q_min * max_queue_diff_part    # default 3.0
```

`Q_min` is the minimum queue depth among the other `probe_ok` and
`routing_eligible` candidates. If there are none, no diversion happens. A
`max_queue_diff_part <= 1` is coerced back to 3.0.

On a diversion, the mapping is **re-remembered** for the new candidate immediately:
the current request is about to build its cache on the new executor, so the record
must follow it.

### 6.8 The resource wait

`ModelReadinessTracker.should_divert`:

```
wait_limit = resource_wait_timeout_seconds (60)      when model_loaded is not False
           = cold_model_wait_timeout_seconds (0)      when model_loaded is False
```

Divert when `blocked_for >= wait_limit`, **or** when
`resource_blocked_ratio >= resource_blocked_ratio_threshold` (0.5) *and* the
observed span is at least `wait_limit`. The ratio rule is the anti-flap guard; a
threshold ≤ 0 disables it.

On expiry the router looks for an alternative that **can run now**. If there is
none, it keeps the preferred candidate with the reason `affinity_wait_no_alternative`.

A **cold** model is not worth waiting for, because its cache is already gone. That
is why `cold_model_wait_timeout_seconds` defaults to 0.

### 6.9 Where the readiness samples come from

There is **no background prober**. Every evaluated candidate is sampled on every
routing decision. The scheduler's ~1 Hz deferral loop provides the sampling
density. A task that sits in a busy fallback and is not re-resolved contributes no
history.

### 6.10 When affinity is dropped

| Trigger | Mechanism |
|---|---|
| A force-unload of a soft consumer | The candidate invalidator → `forget_candidate` + `clear_candidate` |
| `force_unload_all` | The same |
| `clear_soft_consumers_for_providers` | The same |
| An upstream execution failure (`UPSTREAM_ERROR`) | `forget_candidate` + `clear_candidate` |
| A probe failure at routing time | `forget_candidate` when the preferred candidate is not `probe_ok`, is not eligible, or the cache group changed |
| A TTL expiry or an LRU eviction | The store's own pruning |

**A queue timeout does not drop the affinity.** The executor did not lose its cache.

---

## 7. The resource monitor

`core/resource_monitor.py`.

### 7.1 The loop

`run()` polls, then waits on a stop event with a timeout equal to the time until the
earliest next poll. `poll_resource` refreshes the availability first, then polls
each sensor.

The poll interval is `monitoring.poll_interval` (default 10). When `monitoring` is
absent but the resource has a command probe or a peer provider, the default is
**also 10 s**. Otherwise the resource's monitoring is disabled. A non-positive or
non-numeric interval is a configuration error and disables the monitoring.

### 7.2 Sensors

```json5
{
  "id": "gpu_temperature",
  "label": "GPU temperature",
  "unit": "C",
  "command": "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits",
  "pattern": "Package id 0:\\s+\\+([0-9.]+)",   // optional
  "match_group": 1,                             // optional
  "command_timeout": 5,                         // default
  "threshold": { "operator": "above", "value": 85,
                 "reactions": ["notify", "release_resource"], "cooldown": 300 }
}
```

Without a `pattern` the command must write **exactly one finite number** to
standard output. With a `pattern`, the first match is used and `match_group`
selects the capture group (numbered or named).

The command runs through `asyncio.create_subprocess_shell` with a timeout; a
non-zero exit code becomes an error carrying up to 240 characters of stderr.

### 7.3 Thresholds and reactions

`above` is strictly greater, `below` is strictly less. A normal reading **re-arms**
the sensor. A reaction runs once on crossing the threshold, and then no more often
than `cooldown` seconds (default 300).

| Reaction | Effect |
|---|---|
| `notify` | A warning in the system log and a visible alert |
| `release_resource` | `Resources.force_release(resource_id)` — unloads **only** idle models, and reports the blocking consumers rather than interrupting them |

Configuration errors (an unknown reaction, a missing command) are logged **once**
per (resource, sensor, message) so the log is not flooded every poll.

### 7.4 Availability commands

```json5
"availability": {
  "command": "nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits",
  "metric": "VRAM",
  "unit": "MiB",
  "command_timeout": 5
}
```

- The command must write one finite numeric free-capacity value.
- `metric` must exist in `limits`.
- When both `availability.unit` and `units.<metric>` are set, they must match. An
  omitted unit is treated as an empty string and leaves the consistency to the
  administrator.
- A failure records the error and **keeps the previous reading**, but flips the
  status to `error` — and the resource becomes unavailable.

`refresh_required_availability(reqs)` refreshes every required resource and returns
`False` on the first failure. This is the fail-closed pre-dispatch check.

### 7.5 Peer resources

`PeerResourceClient` probes only providers with `api: "openaix"`. It tries
`{baseUrl}/v1/resources/{id}` and then `{baseUrl}/api/resources/{id}`.

| Response | Interpretation | Cached? |
|---|---|---|
| 401 / 403 | Not a peer | Yes (negative) |
| A non-JSON body or an incompatible schema | Not a peer | Yes (negative) |
| 404 on both URLs | Not a peer | Yes (negative) |
| A timeout or a transport error | Transient | **No** — retried later |

The schema check requires `id: str`, `limits: dict` and `availability: dict` with
`availability.available: dict`.

`_refresh_peer_availability` maps the peer's metrics onto the local metric names,
drops the unknown ones, and treats a **unit mismatch as an error**. On success it
records the observation and the peer telemetry, which is filtered against
`resources[].telemetry.sensors`.

The peer's resource id may differ from the local one through
`availability.peer_resource_id`.

---

## 8. The local server manager

`core/local_server_manager.py`. Manages the `llama-server` processes aidir owns.

### 8.1 The ownership record

`{root}/logs/llama_cpp_servers.json` holds one record per provider:
`{pid, start_ticks, model_id}`. Writes are atomic (write to `.tmp`, then
`Path.replace`). A missing or corrupt file is treated as an empty record set.

The **start ticks** are field 19 of `/proc/<pid>/stat`. The recorded and the actual
ticks must match for the process to be considered owned. This is what prevents
acting on a recycled PID.

### 8.2 `ensure_running(provider_id, model_id)`

1. A provider without a `baseUrl` → `UPSTREAM_UNREACHABLE`.
2. `GET {baseUrl}/health` returns 2xx → **return immediately**. The server is
   external; aidir never adopts or stops it.
3. No `exec_cmd` → `UPSTREAM_UNREACHABLE` ("not healthy").
4. Under a per-provider `asyncio.Lock`, re-check the health.
5. If the persisted record still identifies a live owned process → wait for its
   readiness instead of starting a second one.
6. Otherwise remove the stale record, split `exec_cmd` with `shlex.split` and expand
   `~` and `$VAR`. A parse error → `INVALID_EXEC_CMD`.
7. Start with `asyncio.create_subprocess_exec(..., start_new_session=True)`. An
   `OSError` → `LLAMA_CPP_START_FAILED`. `start_new_session=True` makes the child a
   process-group leader, so the whole group can be signalled.
8. Store the record, then wait for the readiness. If it fails, stop the server and
   re-raise.

### 8.3 Waiting for readiness

`_wait_ready` polls `GET {baseUrl}/health` every 0.5 s. The timeout is
`provider.startup_timeout` (default **120 s**). It fails fast when the process has
exited (the exit code is included) or when the PID no longer matches the recorded
ticks. On timeout → `UPSTREAM_UNREACHABLE` ("was not ready within {timeout}s").

### 8.4 Stopping

`stop(provider_id)` sends `SIGTERM` to the process group, polls for up to 10 s,
then sends `SIGKILL`. A `ProcessLookupError` removes the record and returns `False`.
**An unowned server is never touched.**

### 8.5 Adoption across a restart

`adopt_existing()` runs during `Core.start`: for every owned record, a healthy
provider is **kept** and an unhealthy one is stopped. The kept providers' VRAM is
then re-registered as persistent soft consumers.

### 8.6 The logs

`logs/local_llama_cpp.log` for the provider `llama_local`, otherwise
`logs/llama_cpp_<safe_id>.log`.

### 8.7 The `keep_alive` semantics

For a `llama-cpp` provider, `keep_alive` means **restart-on-failure** during the
active window; it does **not** send API pings. For an Ollama provider, `keep_alive`
is a real ping window measured from the last use.

The two providers share one configuration key with two different meanings. This is a
known inconsistency.

---

## 9. Known limitations of this subsystem

| # | Limitation |
|---|---|
| 1 | The affinity identity is context-only, so there is no per-alias opt-out; the deprecated keys are ignored |
| 2 | The scheduler's router cannot probe the OpenAI-family providers, which spuriously forgets their affinity records |
| 3 | The affinity store is process-local; there is no Redis mirror |
| 4 | The 20-message trim in the `openaix` worker breaks conversational prefix reuse |
| 5 | The 20-message trim applies only to `openaix` — the transports that override `execute()` do not apply it |
| 6 | The readiness tracker samples only on candidate evaluation |
| 7 | The affinity wait competes with the task's own `queue_timeout`: a waiting task can hit `QUEUE_TIMEOUT` first |
| 8 | The admission lock is a single global `asyncio.Lock` |
| 9 | The deferral loop is a busy poll that re-probes every candidate per second |
| 10 | `_select_worker` ignores the provider |
| 11 | Equal-priority tasks are ordered by task id, not by arrival time |
| 12 | `force_unload_for` returns `False` if any unload failed, but the successfully unloaded models are already cleared from the ledger |
| 13 | The peer capability cache is per-process and only negative-definitive |
| 14 | The availability commands are trusted shell |
| 15 | The `estimated` capacity path is recomputed on every call |
| 16 | `upstream_error_response` restores only `content-type`; the other upstream headers (rate limits, `retry-after`) are dropped |
| 17 | The request-body limit is enforced per endpoint and the middleware buffers the whole body in memory |
