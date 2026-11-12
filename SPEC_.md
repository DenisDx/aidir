# SPEC Overlay

Status: active. Date: 2026-10-29.

This file is the editable overlay over [SPEC.md](SPEC.md). It records the
changes and decisions that have been approved since the base specification was
last synchronized. If this file and `SPEC.md` disagree, this file wins.

The user merges this overlay into `SPEC.md` from time to time, then deletes
`SPEC_.md`. Agents must add new specification changes here, not in `SPEC.md`.
Direct edits to `SPEC.md` are forbidden unless the user explicitly approves a
narrow, one-time correction.

## 1. Documentation and proposal lifecycle

- `SPEC.md` is the base specification and is read-only for agents.
- `SPEC_.md` is the editable overlay. It is not a correction log and does not
  use the `FILENAME_.md` correction template.
- Only the highest-numbered `proposal_updateN.md` is open. Every older
  `proposal_update*` document is closed.
- A proposal may be partially closed. Unfinished items may be moved to the next
  `proposal_updateN.md` only after direct approval by the user.
- A non-update proposal (`proposal_<name>.md`) is independent. Its status is
  recorded inside the file, and closing it does not require a new
  `proposal_update`.
- `proposal_update*` is reserved for small list fixes, bugs and inaccuracies.
  Large new features belong in a separate `proposal_<name>.md`.
- Deferred work and ideas that still need discussion belong in `ROADMAP.md`.
- `DOCUMENTS.md` must stay synchronized with every document added, renamed or
  closed.
- `SPEC_NEW.md` is not a current document name. References to it mean
  `SPEC.md`.

## 2. Configuration: `default_auth_mode`

`config.json5` gets a root key `default_auth_mode`:

```json5
{
  default_auth_mode: "disabled",
}
```

Semantics:

- If the key is absent, the value is `"endpoint"`.
- `"endpoint"` makes peer surfaces require a valid API token by default.
- `"disabled"` keeps peer surfaces public by default.
- The key is parsed as a whitespace-separated list of keywords so that future
  authentication mechanisms can be added without a format change. At present
  the only keywords are `disabled` and `endpoint`; if both are present,
  `endpoint` wins.
- An unknown or invalid value fails closed to `"endpoint"`.
- An explicit `auth_mode` in `peer_tasks` or `peer_resources` overrides this
  default for that surface.
- The provider `auth` block is not a peer authentication setting. It stays the
  credential used by the reading side when it calls a peer, and by an inference
  provider when it calls an upstream.
- The shipped `config.json5` and `config.json5.example` set
  `default_auth_mode: "disabled"` until the reading side is known to carry the
  required tokens. This preserves the behavior of existing multi-instance
  deployments.

## 3. Security decisions

### 3.1 MCP authentication (W1)

`require_auth` keeps its default of `false`, including for the `mcp` API. The
doctor's `auth` check must report every MCP endpoint that publishes its tools
without a token. The report is advisory; it does not change the default.

A separate dashboard security panel is proposed in
[proposal_security_dashboard.md](proposal_security_dashboard.md). It belongs
to the left of the system-health card and lists open security findings such as
MCP without authentication.

### 3.2 WebUI authentication (W3)

W3 is deferred to [ROADMAP.md](ROADMAP.md). The previously accepted
`sha256$<hex>` scheme is withdrawn; no interim password-hash format is currently
planned.

The preliminary direction is browser-side authentication:

- the browser derives a deterministic signing key pair from the password and a
  per-user salt;
- the server stores the login, the salt and the **public** key, never the
  password or a password hash;
- at sign-in the server sends a fresh random challenge; the browser derives the
  private key locally and signs the challenge;
- the server verifies the signature with the stored public key and then creates
  the ordinary session cookie.

The password must never be sent to the server. Signing a static salt alone is
not sufficient because it is replayable; the signed value must include a fresh
server challenge. This is a preliminary design, not a PAKE: a stolen public key
and salt still permit an offline dictionary attack. Login rate limiting remains
required.

## 4. Task persistence (W20)

A task restored from Redis must preserve its retry and fallback state. The
following fields are part of `Task.to_redis_hash()` and
`Task.from_redis_hash()`:

- `next_retry_at` (float),
- `fallbacks` (JSON list of strings),
- `on_reject` (JSON object).

`fallback_index` and `retry_attempt` were already persisted. After this change,
a restored task continues its fallback chain instead of starting it from the
beginning.

## 5. Closed and deferred items

- W28 (orphan count as a separate health-card row) is closed. The count is
  already exposed in the execution-health data and rendered by the existing
  health card; a separate row is not wanted.
- W3 (WebUI authentication) is deferred. The password-derived key-pair design
  is tracked in `ROADMAP.md`; the interim `sha256$` scheme is withdrawn.
- W4 (`web_fetch` SSRF guard) is in scope but deferred. It is tracked in
  `ROADMAP.md`.
- W5 (settings-editor write of `exec_cmd`) is deferred. It is tracked in
  `ROADMAP.md`.
- W7 (CSRF token) is deferred. It is tracked in `ROADMAP.md`.
- The security analysis in `local_proposal_update_1.md` is historical. Its open
  findings are tracked by the current proposal ledger and `ROADMAP.md`, not by
  editing the closed security analysis in place.

## 6. Session and routing decisions (W9, W14, W15)

### 6.1 Legacy sessions (W9)

The legacy session fallback is removed. A session is valid only under the
instance-scoped key `aidir:session:<instance>:<token>`. Sessions written before
the namespace change are no longer accepted.

### 6.2 Direct smart routing (W14)

`type: "direct"` is implemented. A direct smart model must have exactly one
candidate. The router resolves to that candidate under the normal admission
rules: a candidate that cannot start is queued, not forced. A direct model with
a different candidate count fails at request time with HTTP 400 and
`INVALID_ROUTING_CONFIG`.

### 6.3 KV affinity identity (W15)

KV affinity depends only on the request context. The alias, endpoint, route and
`prompt_cache_key` do not affect the identity. The same canonical context uses
the same affinity record regardless of where the request came from.

The fingerprint hash is `HMAC-SHA256(secret, "kv2\0kind\0prefix")` and the store
is global with one LRU. `max_entries_per_alias` and `honor_prompt_cache_key` are
accepted for configuration compatibility but deprecated and ignored.

## 7. Security, observability and protocol fixes

- The log WebSocket authenticates with the `aidir_token` session cookie only.
  The query-string token is no longer accepted, so tokens cannot leak into
  access logs.
- Logout reads the session token with the same Bearer-first rule as the
  authentication dependency and deletes that session, not only the cookie one.
- `/api/test/agent` clamps a client-supplied `_timeout` to
  `[1, webui.request_timeouts.agent_test_max]`; the default maximum is 300
  seconds.
- `logs/` is created or tightened to mode `0700`. New audit journal files use
  `0600`. The doctor reports a different directory mode and can fix it.
- A Redis bind outside loopback must have a password. The installer generates
  one when the bind is widened, Compose health checks authenticate, and the
  doctor reports an unprotected widened bind.
- Audit `durable` enqueue and receipt waits are bounded. A failure raises
  `TimeoutError` and marks health unhealthy. `best_effort` still drops on queue
  overflow, and `dropped_recent` is visible in the execution-health line.
- Emergency audit retention never deletes today's partition, even when it is
  the only daily partition left. If the emergency limit is still exceeded, the
  retention reports an unhealthy status instead of destroying the active day's
  audit evidence.
- Upstream error responses preserve `retry-after`, `request-id`, `x-request-id`
  and the `x-ratelimit-*` family. Other upstream headers are dropped.
- Anthropic signed thinking blocks keep their signature through conversion. The
  stream encoder emits `signature_delta` before closing the thinking block; a
  rewritten turn with no signature warns and omits the signature instead of
  inventing an empty one.
- `logging.logs` and its legacy alias `logging.log_overrides` are declared in
  the descriptor as open maps of bounded per-file maintenance settings.
- `messages_response_to_internal` no longer accepts an unused `streaming`
  keyword.

## 8. Test LLM endpoint selection

`POST /api/test/llm` does not require an endpoint whose `api` is exactly
`ollama`. It uses the first chat-capable endpoint (`ollama` or `openaix`) that
has a port, and prefers an endpoint whose `worker` matches the `worker` field in
the request. The request is always sent to that endpoint's `/api/chat`. This
lets the Test LLM dialog exercise the shipped `openaix` endpoint, including
`call_llama_cpp` and other workers.

## 9. Round 4 completion (2026-10-24)

- The scheduler router now probes `openai`, `opencode-go` and `anthropic`
  candidates through their model list, so those routes are no longer classified
  `provider_down` before dispatch.
- KV affinity keeps the in-process cache as the hot path and mirrors records to
  Redis with their TTL. A local miss reads the mirror. The full fingerprint is
  carried in route metadata and forwarded as `X-Aidir-KV-Fingerprint`.
- The unconditional 20-message trim in the `openaix` worker is removed. The
  executor receives the payload the router fingerprinted; provider context
  limits remain authoritative.
- `_select_worker` prefers the worker named by the resolved provider route over
  an incompatible pinned worker. Admission uses per-resource locks acquired in
  sorted order. Equal-priority tasks are FIFO through a priority-major,
  arrival-minor queue score.
- Envid contexts are pruned by cron using `envids.store_duration_minutes`.
  Stale running tasks are failed with `STALE_TASK` after their run timeout plus
  a 60-second margin. The cron health check probes `/health` on configured
  OpenAIx and MCP endpoints.
- Remote smart probes are cached for one second and concurrent probes for the
  same provider/model/priority share one in-flight call.
- The `openaix` and MCP peer surfaces accept a termination request; the
  dashboard forwards a remote task cancel to the provider that holds it.
- `count_tokens` adds 1600 tokens per image block and is documented as an
  estimate.
- The retry policy is recorded in `spec_timeouts.md`: every attempt gets the
  full run budget, inter-attempt waits are not charged to it, and no separate
  retry-wait timeout exists.
- The two CLI probes no longer match test discovery, the README no longer lists
  removed `/api/config*` routes or a missing `prod.json5.example`, and the plan
  documents no longer quote a fixed test count.
- Direct coverage was added for the envid registry, the MCP endpoint and the
  three context workers.
- Concurrent local candidate probes now share one in-flight evaluation, and
  remote probes keep the one-second cache.
- The Round 4 ledger is closed in `proposal_update4.md`. The transferred items
  are tracked in `ROADMAP.md`: OpenCode Go release validation, a human browser
  acceptance, and P3 example configuration/installation tooling.

## 10. Multi-GPU resources (2026-10-29)

A resource may declare the hardware devices it includes with an optional
`devices` array of non-negative indices. An absent or empty array keeps the
previous single-value behavior.

Command parsing for availability and sensor probes extends as follows:

- The command may print one reading per line, either as `value` or as
  `index,value`.
- When `devices` is configured and the output is indexed, readings for devices
  that are not listed are ignored before their values are parsed. Every listed
  device must appear exactly once.
- Indexed and unindexed lines must not be mixed.
- With unindexed output, one value for one configured device remains unscoped;
  N values for N configured devices are mapped in configured order.
- A missing listed device, a duplicate device, or a value count mismatch is a
  probe error and fails closed.

For availability, `availability.aggregate` selects how per-device free capacity
is combined: `min` (the default and fail-closed choice), `max`, or `sum`. A
multi-device resource should use `sum` only when the provider actually splits a
model across all listed devices; heterogeneous cards should be separate
resources.

Sensor thresholds are evaluated per device, and the sensor alerts when any
device crosses its threshold. The public sensor and telemetry snapshots keep a
`devices` list with per-device values, status, and alert state. The top-level
sensor value remains the maximum reading for an `above` threshold and the
minimum reading for a `below` threshold. Resource snapshots include the
configured `devices` list, and peer telemetry forwards per-device readings for
whitelisted sensors.

## 11. Model concurrency without resource requirements (2026-11-03)

`maxConcurrentRequests` is enforced for every model, including a model whose
resolved resource requirement map is empty. When a request names a model, the
scheduler applies the model admission gate before dispatch even if the model
declares no `resources` or `sessionResources`:

- A model with `maxConcurrentRequests <= 1` holds one single-request admission
  slot from dispatch until the task releases its resources.
- A model with `maxConcurrentRequests > 1` counts one active session per
  in-flight request and admits at most the configured maximum.
- A request above the limit is deferred with the model-concurrency deferral
  reason and retried when an active request finishes.

The model admission counter is therefore a property of the model, not of the
resource map.

## 12. Monitoring compatibility routes (2026-11-03)

The OpenAIx endpoint adds two unauthenticated monitoring routes that previously
returned 404:

- `GET /stats` returns a JSON snapshot: `object`, `instance`, `uptime_seconds`,
  task counts (`total`, `active`, `by_status`), worker counts (`total`,
  `enabled`), and resource count.
- `GET /metrics` returns the same counters in Prometheus text exposition format
  (`text/plain; version=0.0.4`), with `aidir_up`, `aidir_uptime_seconds`,
  `aidir_tasks_total{status=...}`, `aidir_tasks_active`, `aidir_workers_total`,
  `aidir_workers_enabled`, and `aidir_resources_total`.

Neither route exposes task bodies, tokens, or configuration values.

## 13. Health, model state and queue-state extensions (2026-11-03)

The OpenAIx `/health`, `/v1/models`, `/v1/models/{model}` and queue-state routes
now share one `schema_version: 1` extension model.

- `/health` is a last-known snapshot: resources (with availability, telemetry,
  loaded models and host), configured models that declare resources,
  telemetry grouped by executor, external provider statuses, queue counters and
  runtime flags. It never probes an executor or peer.
- Resource peer responses include `loaded_models`, so another aidir can see the
  models a peer holds without asking again. Local resources also report the
  four case fans (fan1-fan4) in the shipped configuration.
- `/v1/models` and `/v1/models/{model}` keep the OpenAI model shape and put every
  aidir field under one `aidir` key. The block reports `real_id`, provider,
  resource totals/free/used-by-model/needed and `state` with `can_run_now`,
  `resource_ready`, `model_loaded`, admission and queue counters. Smart models
  add `aidir.smart` with `context_identification`, `fallback_route` and
  candidate states.
- Model ids containing `/` are accepted both literally and percent-encoded.
  Unknown models remain `404 INVALID_MODEL`.
- Queue-state accepts a smart model and reports the aggregate queue and
  fallback route when no candidate is schedulable instead of returning 500.
  Queue failures return `503`; the route never returns 500.

## 14. Health and model-state corrections (2026-11-03)

- A model already loaded on a resource reports `resource_ready: true` and
  `resource_ready_after_unload: true`; `can_run_now` is then limited only by
  the model's concurrency. The previous base-memory admission result could
  report `base_memory` for a loaded model because its soft-consumer fingerprint
  did not match.
- OpenAI `/v1/*` model lookups return `404 model_not_found` with
  `type: "invalid_request_error"`; Ollama `/api/*` keeps `INVALID_MODEL`.
- The model extension adds `resolved_provider` and `resolved_model`. For a smart
  model they name the concrete fallback backend; `aidir.smart.fallback_route`
  remains available.
- `/health.models` includes smart models whose candidates declare resources,
  with merged candidate resources and the smart state.
- `/health.tasks` is the active task list (created, queued, running). It is
  always present and is an empty array when no task is active.

## 15. Managed llama.cpp provider probe (2026-11-03)

A `llama-cpp` provider remains the local-server type even when the server
speaks the OpenAI wire protocol: `api: "llama-cpp"` owns `exec_cmd`,
`LocalServerManager`, the `call_llama_cpp` worker and per-model resource
lifecycle. Setting `api: "openai"` would remove the local start/stop path.

Strata requires an API key and does not echo the configured model alias in
`/v1/models`; it lists its internal id (`qwen3.8-flash-next-ista-iq3_s`). For a
managed llama.cpp provider that declares `exec_cmd`, a successful
`GET /v1/models` is therefore treated as availability even when the listed id
differs from the configured id. Authentication is taken from the provider
`auth` block and applies both to probes and to the worker request.

## 16. Resource admission, occupancy and response normalization (2026-11-03)

- Smart-route dispatch now resolves resource admission and warm-model reuse
  with the concrete `resolved_model` (for example `Qwen3.8-Flash-Next`) instead
  of the client-visible alias (`local-only`). This is what kept queued tasks
  reporting `insufficient resources` while the model was already loaded.
- A persistent `llama.cpp` base reservation is retained on every required
  resource, even when that resource declares no `alive_time` or `keep_alive`.
  Such consumers remain visible to `get_active_soft_consumers()` and
  `model_is_loaded()`.
- `/health.resources[].used` is now the total occupied amount; `active_used`
  and `soft_used` expose the split. `Resource.snapshot()` also gained
  `total_used`.
- OpenAIx converts Ollama responses to OpenAI without leaking Ollama-only
  timing/counter fields (`total_duration`, `load_duration`,
  `prompt_eval_count`, `prompt_eval_duration`, `eval_count`, `eval_duration`);
  the OpenAI `usage` block is always synthesized when the counters exist.
- The OpenCode Go `deepseek-v4-flash` upstream id was renamed to
  `deepseek-v4.1-flash`; the configured model id follows the upstream while the
  external alias `opencode-deepseek-v4-flash` is kept.

## 17. Health task limits and client labels (2026-11-03)

- `/health.tasks[]` now carries `queue_timeout` and `run_timeout` in seconds,
  `wait_ms` (created to start), `run_ms` (start to now while running),
  `queue_remaining_ms`, and `run_remaining_ms`.
- Each task also carries `client` (`internal`, `anonymous`, or the matched API
  user login), `client_host`, `external`, and `envid`, so callers can separate
  their own tasks from other clients' tasks. The token itself is never exposed.
- `/health.runtime.timeouts` reports the configured default queue, run, and
  restart-wait limits. `by_priority` remains keyed by strings.

## 18. Explicit client labels (2026-11-03)

A task's `/health.tasks[].client` label can be set with the `X-Aidir-Client`
request header or a `client` string field in the request body. The header wins.
The label is trimmed to 64 printable characters and is never sent upstream; the
body field is removed from the task payload. Precedence: header, body, matched
API user login, `anonymous`; internal tasks use `internal`.
`/health.tasks[].client_source` reports `header`, `body`, `api_user`,
`anonymous`, or `internal`.

## 19. Running tasks count as loaded models (2026-11-03)

`model_loaded` and `loaded_models` now include active task consumers, not only
base reservations and idle soft consumers. While a task runs, its consumer id
is mapped back to the live task route, so `/v1/models/{model}` reports
`state.model_loaded: true` and `/health.resources[].loaded_models` lists the
model with `state: "running"`.

## 20. Safe route switch before unload (2026-11-03)

- A smart route whose affinity-preferred candidate is blocked by resources
  (`session_memory`, `base_memory`, `waiting_unload`) now checks for an
  alternative candidate that can start immediately before waiting or unloading.
  When one exists, the route is diverted immediately
  (`affinity_resource_diverted`). A cold model with a ready alternative is also
  diverted instead of waiting.
- `force_unload_for` refuses to stop a provider while the resource has active
  consumers, so running tasks always finish first.
- A successful managed `llama-cpp` unload stops the whole provider and clears
  its persistent soft reservations from every resource, not only the resource
  that triggered the unload.
- `_compute_soft_used` includes persistent consumers even on a resource without
  `alive_time`/`keep_alive`, so a switch can see the real occupancy.

## 21. Parallel concurrency for llama_local_ii (2026-11-03)

`llama_local_ii/Qwen3.8-27b-Q6_K_XL` runs with `maxConcurrentRequests: 2`,
matching the `--parallel 2` llama-server command. Each in-flight request declares
1024 MiB of session VRAM on `local_machine` and `local_gpu1`; the model base
stays at 30000 MiB per resource. The scheduler admits at most two active
requests, the third is deferred with the model-concurrency reason. The limits
are `apply: reload`, so SIGHUP applies them without stopping running tasks.

## 22. Remote refusal grace window (2026-11-03)

- Added `routing.remote_refusal_grace_seconds` (default 60, `0` disables).
- A remote `capacity` refusal (for example `admission=concurrency` with empty
  `free`/`needed`) is now retried for that window before the task fails with
  `REMOTE_CANNOT_RUN`. This covers a remote that is only briefly busy.
- `unreachable` keeps its existing behavior: the task waits, bounded by its queue
  timeout.
- The grace start is per task and is cleared on re-route, completion or failure.
  `Scheduler.apply_config` reloads the value on SIGHUP.

## 23. session_memory on a loaded parallel model (2026-11-05)

A loaded model does not by itself prove that another session fits. For
`maxConcurrentRequests > 1`, admission also checks `sessionResources` against
the measured free VRAM. If the free amount is below the configured session
cost, the admission is `session_memory` and the task waits; the model will not
release its own base, so a session value larger than the permanent headroom
makes the task wait until its queue timeout.

The Q8 and Q6 `Qwen3.8-27B` llama.cpp models preallocate both `--parallel 2`
slots at startup. Their `sessionResources` changed from 1024 MiB to 256 MiB per
GPU to match the actual headroom. `/v1/models` now keeps `model_loaded: true`
but reports `resource_ready: true`, `can_run_now: false` and
`admission: "session_memory"` for this case instead of overriding it to `ready`.

## 24. Q8 27B single-slot runtime (2026-11-05)

`local_Qwen3.8-27B-UD-Q8_K_XL` now starts llama-server with `--parallel 1` and
declares `maxConcurrentRequests: 1`, removing its per-session VRAM reservation.
The change was made after a CUDA OOM during a very large prompt with two slots.
The exec_cmd change takes effect when the provider or the service restarts.

## 25. Ambiguous model ids and sticky routing (2026-11-05)

- At startup aidir scans `models.providers` and logs one warning when several
  provider models expose the same external id or alias, naming the providers.
- When a request matches several provider/model pairs, `_resolve_model_route`
  prefers a candidate whose concrete model is already loaded or has a running
  task. A loaded or executing match outranks the normal alias/id rank.
- Consequently an ambiguous external id sticks to the candidate that was loaded
  first. This is intentional and documented; unique aliases are the way to make
  the choice explicit.
- This prevents the shared-port loop where a request for a plain id unloaded the
  Strata server that another alias had just loaded.

## 26. Configurable internal tool injection (2026-11-05)

- Added root `default_tool_injection` (bool, absent/false by default). It is the
  global fallback for requests that carry no `tools` field.
- Model-level `default_tool_injection` now applies to any route, smart or
  concrete (`llama-cpp` included), not only smart models.
- Added worker-level `workers.items.<worker_id>.default_tool_injection`. A
  worker-level `false` is a hard off and wins over the model flag.
- Resolution order: worker false, model flag, worker true, root flag, false.
- `tools: []` / `tools: null` in the request disables injection for that call;
  a non-empty caller `tools` list remains caller-owned.
- To preserve the pre-2026-11-05 behaviour, set the root key to `true`.

## 27. Termination of running tasks and dashboard terminal visibility (2026-11-07)

- A task removed from the in-memory registry while its execution was still
  running (the streaming client-disconnect path calls `delete_task`) is no
  longer stranded: `Core.delete_task` cancels the live scheduler run first when
  the task is `running`.
- `Core.terminate_task` cancels the live run by task id even when the task is no
  longer in the in-memory registry, then finalizes the persisted hash. A
  terminated streaming task therefore closes its upstream connection instead of
  finishing on the model after it disappears from the dashboard.
- The dashboard task list shows terminal tasks (`completed`, `failed`,
  `canceled`) for 15 seconds after `finished_at` in addition to the active ones,
  so the operator sees the final state for several refresh cycles instead of the
  row disappearing between polls.

## 28. Usage metering, cost, and speed profiles (2026-11-12)

- aidir counts every LLM call per model: the prompt, cached prompt, cache
  write, generation, reasoning, and answer tokens, the tool calls, the duration,
  the time to first token, the failures by class, and the cost. The counters
  live in a local SQLite database (`billing.database`, `logs/usage.sqlite` by
  default) and survive a restart.
- No prompt text, response text, file content, credential, or session value is
  written into the usage database, and no such value becomes a metric label.
  A client identifier (`envid`) is stored only when it is a known envid of the
  registry; the calls without one are counted in an empty bucket.
- The cost of a call is the provider-reported amount when there is one, and the
  computed amount from the configured prices otherwise. The two are kept apart.
  OpenCode Go reports `usage.cost` and `usage.opencode.allowance`; the
  allowance is tracked separately from the charged cost.
- A smart alias shows one aggregated row. Without prices of its own, its cost is
  the summed cost of the concrete models it called. A call is counted for the
  alias and for the concrete model, so the sum over the rows is larger than the
  number of client requests; the external and the internal calls are counted
  separately.
- The reasoning share is never estimated: an unknown share is `0`. The tool-call
  tokens are measured from the generated tool-call text and are an estimate.
  A synchronous remote call without provider timings stores the total duration
  only; no prefill or generation split is invented.
- The hourly rows cover the current calendar day and the previous one, the
  finished days are rolled up into daily rows, and the finished months into
  monthly rows. The rollup runs in the service process and is idempotent.
- The counters are exposed through the `/api/usage/*` routes, the `usage` block
  of `/stats` and `/metrics`, the `aidir.usage` block of `/v1/models`, and the
  `LLM statistic` tab of the WebUI. The tab opens on the last 24 hours.
  The cost and the allowance of `/v1/models` require an authenticated endpoint
  user token.

## 29. Configurable limits and the budget refusal (2026-11-12)

- A limit rule has an id, a window (`1h`, `5h`, `1d`, `1w`, `1M`), a mode
  (`rolling` or `calendar`), optional token and money limits, and an action
  (`warn`, `route`, or `block`). The same rule shape is accepted on the
  instance, on a provider, and on a model, including a smart alias.
- The money metric is automatic: the provider-reported allowance when the
  provider reported one, the computed cost otherwise.
- An over-budget model is refused with HTTP `429` and the code
  `BUDGET_EXCEEDED` in the OpenAI error shape, in every protocol. A smart alias
  drops an over-budget candidate from its candidate list and is refused only
  when no candidate is left.
- The block is sticky: the model stays stopped until the operator resets the
  counters of the rule, either from the `LLM statistic` tab or with
  `POST /api/usage/limits/reset`. The reset clears the token counters, the
  money counter, or both, and the blocked state; the stored aggregates are not
  changed.
- The default action is `warn`, so an installation without limits keeps its
  current behaviour.
