# Proposal: aidir Update Round 1

Date: 2026-10-23
Scope: every confirmed defect and every design gap recorded in
[SPEC_NEW.md](SPEC_NEW.md) §14, except the two items that are explicitly deferred
(the middleware chain and the `before_tools` extension — see §10).
Constraint: this document is analysis + proposals only; no existing files are
modified. Once it is accepted it is frozen like every other proposal, and any new
requirement goes into the next numbered file.

Priority meaning:

| Priority | Meaning |
|---|---|
| **P1** | A correctness or security problem that makes a documented behavior false or a stated guarantee untrue |
| **P2** | A reliability or performance problem that bites under load or after a restart |
| **P3** | A completeness or consistency problem that misleads a reader or a contributor |

---

## 1. Summary

| # | Area | Pri | Item | Location |
|---|------|-----|------|----------|
| U1 | Configuration | P1 | `endpoints[].peer_resources` is read by the code but not declared in the descriptor, so it cannot be set | `core/endpoints/endpoint_openaix.py:48`; `config.descriptor.json5:750-865` |
| U2 | Configuration | P2 | `envids.check_period_minutes` and `envids.store_duration_minutes` are declared and shipped but read by nobody, so the envid contexts in Redis never expire | `config.descriptor.json5:1437-1452`; `core/envid.py:264-275` |
| U3 | Configuration | P2 | `logging.logs` / `logging.log_overrides` is read by the cron job but not declared, so it cannot be set through the editor | `core/cron.py:60`; `config.descriptor.json5:340-507` |
| U4 | Configuration | P2 | `logging.levels.workers` and `logging.alllevels.workers` use the plural while the logger type is `worker`, so the two keys never take effect | `core/logger.py:140,155`; `config.json5.example:69,75` |
| U5 | Configuration | P1 | `reload_config` refreshes only the settings cache; the workers, the endpoints and the resource model are not rebuilt, so the `reload` apply policy is nominal | `core/app.py:385-391`; `core/settings/runtime.py:262-282` |
| U6 | Routing | P1 | The scheduler's smart router cannot probe the OpenAI-family providers, so those candidates are classified `provider_down` and their affinity record is silently forgotten | `core/scheduler.py:893-912` vs `core/endpoints/endpoint_ollama.py:627` |
| U7 | Routing | P2 | The KV affinity store is process-local; a restart loses the mapping and no instance can see another's | `core/kv_affinity.py:251-492` |
| U8 | Routing | P2 | The context fingerprint is not forwarded across instances, so a peer that re-routes breaks the edge's affinity | `core/endpoints/endpoint_ollama.py:376` |
| U9 | Routing | P3 | Only `first_available` is implemented, although the descriptor permits `direct` — the latter produces HTTP 400 at request time | `core/smart_router.py:104-110`; `config.descriptor.json5:1108` |
| U10 | Routing | P3 | There is no per-alias affinity opt-out; `proposal_kv_keeper.md` deferred it explicitly | `core/kv_affinity.py`; `proposal_kv_keeper.md` |
| U11 | Routing | P2 | The 20-message trim in the `openaix` worker breaks conversational prefix reuse, so the executor may never see the hashed prefix | `workers/agent/openaix/app.py:167-181` |
| U12 | Scheduling | P2 | `_select_worker` ignores the provider: a task pinned to a worker whose provider cannot serve the model still reaches that worker | `core/scheduler.py:345-360` |
| U13 | Scheduling | P2 | The deferral loop is a busy poll that re-probes every candidate once per second per blocked task | `core/scheduler.py:73-251` |
| U14 | Scheduling | P3 | The admission lock is a single global `asyncio.Lock`, so every reservation decision serializes process-wide | `core/resources.py:44` |
| U15 | Scheduling | P3 | Equal-priority tasks are ordered by task id, not by arrival time; there is no FIFO within a priority class | `core/queue_manager.py:143-149` |
| U16 | Task model | P2 | `fallbacks`, `on_reject` and `next_retry_at` are not persisted in the task hash, so a task that round-trips through Redis loses its fallback chain | `core/task.py:115-155` |
| U17 | Security | P1 | The MCP endpoint has no authentication at all: the published tools are callable by anyone who can reach the port | `core/endpoints/endpoint_mcp.py:92-149,201-279` |
| U18 | Security | P1 | The four queue-state endpoints are unauthenticated and are `O(queued tasks)` with one `HGETALL` per task | `core/endpoints/endpoint_openaix.py:138-170`; `core/queue_manager.py:312-398` |
| U19 | Security | P1 | The peer resource and peer task endpoints default to `auth_mode: "disabled"`, publishing the task list unauthenticated | `core/endpoints/endpoint_openaix.py:48-57` |
| U20 | Security | P2 | There is no CSRF token anywhere; the whole write surface relies on `samesite=strict` alone | `webui/backend/app.py:805` |
| U21 | Security | P2 | Logout deletes only the cookie's session; a session established with the `Authorization` header stays alive in Redis | `webui/backend/app.py:814-823` |
| U22 | Security | P2 | The WebUI passwords are stored in plaintext and there is no login rate limiting | `webui/backend/app.py:627-634,783-807` |
| U23 | Security | P2 | `web_fetch` has no SSRF guard: a loopback, private or metadata address is reachable | `workers/tool/web_fetch/app.py:360-444` |
| U24 | Security | P3 | `/api/test/agent` accepts an unbounded client-supplied timeout | `webui/backend/app.py:1622-1626` |
| U25 | Security | P3 | The logs directory is world-readable and holds task payloads | `logs/` |
| U26 | Security | P3 | Redis has no password by default | `.env.example:10`; `docker-compose.yml:6-8` |
| U27 | Security | P2 | `models.providers.*.exec_cmd` remains writable through the settings editor, and a write is a command-execution chain | `config.descriptor.json5:982`; `core/local_server_manager.py:77-96` |
| U28 | Security | P3 | The log WebSocket accepts the session token in the query string, where it lands in the access log | `webui/backend/app.py:1235-1248` |
| U29 | Observability | P2 | The audit writer drops events under the default `best_effort` policy and the caller does not see the drop | `core/audit_log.py:683-684,717-729` |
| U30 | Observability | P3 | The audit retention never deletes today's or yesterday's partition, so one huge day can exceed `max_total_bytes` until the next day | `core/audit_log.py:354-356` |
| U31 | Observability | P2 | `cleanup_stale_tasks` is an empty stub: a task stuck in `running` with no live worker is never detected or cancelled | `core/cron.py:237-241` |
| U32 | Observability | P3 | The cron `health_check` only pings Redis; there is no HTTP-level health check of the core endpoints | `core/cron.py:135-146` |
| U33 | Observability | P2 | The upstream error response restores only `content-type`; the rate-limit and `retry-after` headers are dropped | `core/upstream_errors.py:39-42` |
| U34 | Protocol | P2 | The Anthropic stream encoder never emits `signature_delta` and the non-streaming result carries an empty thinking signature | `core/messages_protocol.py:719-720,942-946` |
| U35 | Protocol | P3 | `count_tokens` is a `chars/4` heuristic: the images are not counted and the tool schemas are counted as raw JSON | `core/messages_protocol.py:79-101` |
| U36 | Protocol | P3 | `messages_response_to_internal` accepts a `streaming` keyword that is never used | `core/messages_protocol.py:637` |
| U37 | Tests | P2 | The envid registry, the MCP endpoint and the three context workers have no direct test coverage, although the envid authorization rules are normative | `core/envid.py`; `core/endpoints/endpoint_mcp.py`; `workers/context/*` |
| U38 | Tests | P2 | The seven OpenCode Go release gates are open and the focused test module does not exist | `proposal_opencode_go.md:374-389`; `core/opencode_session.py` |
| U39 | Tests | P3 | Two CLI probes (`test_context_overflow.py`, `test_timeout_semantics_live.py`) match the `test_*.py` discovery glob but contribute zero tests | those files |
| U40 | Tests | P3 | `test_logs.py` performs real log wipe and trim operations against `logs/` at import time | `test_logs.py:11-45` |
| U41 | Documentation | P3 | The README's WebUI route table still lists `/api/config`, `/api/config/raw` and `/api/config/fields`, which were removed | `README.md:646` |
| U42 | Documentation | P3 | `proposal_hooks.md` states that both bundled hooks ship disabled; `hooks/llama_cpp_empty_response_recovery.py` has `ENABLED = True` | `proposal_hooks.md:160-163`; `hooks/llama_cpp_empty_response_recovery.py:5` |
| U43 | Documentation | P3 | The README references `prod.json5.example`, which does not exist in the repository | `README.md:62-63` |
| U44 | Documentation | P3 | The plan documents quote stale test counts (574 and 366; the actual number is 586) | `plan_kv_keeper.md:5`; `plan_aiset_integration.md:433` |
| U45 | Documentation | P3 | Several proposal status lines are stale: `proposal_kv_keeper.md` says "Proposed, not implemented", `proposal_searxng.md` says "Status: proposal", `_proposal_embedding.md` says "Proposed" — all three are shipped | those files |

---

## 2. Configuration correctness

### U1. `endpoints[].peer_resources` is not declared in the descriptor (P1)

**Evidence**
- `core/endpoints/endpoint_openaix.py:48-52` reads `endpoint_cfg.get("peer_resources")`
  and derives `peer_resources.auth_mode` from it.
- `README.md:399-404` documents `peer_resources.peer_resource_id`,
  `peer_resources.request_timeout_ms` and the `auth_mode` switch as a supported
  configuration surface.
- `config.descriptor.json5:750-865` declares the endpoint children `id`, `api`,
  `port`, `bindAddress`, `request_timeout`, `worker`, `logging`, `middlewares`,
  `tools` and `peer_tasks` — and `peer_tasks` alone of the two peer blocks.
- The library's validator reports an undeclared object field as
  `undeclared_field`, and the settings runtime refuses to load a document that
  carries one (`core/settings/runtime.py:207-218`).

**Impact**
Adding `peer_resources` to an endpoint in `config.json5` is a **startup error**:
the document becomes `invalid_on_disk`, the effective tree is empty and the process
stops. The documented peer-resource configuration is therefore unreachable, and the
code path that reads it can only ever see the default `auth_mode: "disabled"`.

**Proposed modification**
1. Add `peer_resources` to the endpoint children in `config.descriptor.json5`, with
   the same shape as `peer_tasks`: `auth_mode` (`"disabled"` | `"endpoint"`),
   `peer_resource_id` (string) and `request_timeout_ms` (int, 1..60000).
2. Extend the `peer_resources` read in `endpoint_openaix.py` to honour the
   per-endpoint `peer_resource_id` / `request_timeout_ms` defaults, instead of
   reading them only from the resource configuration.
3. Add the declaration to `config.types.json5` if the two peer blocks should share
   one typedef, so the two cannot drift apart again.

**Validation**
- `test_config_descriptor.py`: an endpoint with `peer_resources` loads without
  issues, and the descriptor covers the new keys.
- A new test: the endpoint reports `peer_resources.auth_mode = "endpoint"` when the
  configuration says so, and the peer resource routes then require a token.

---

### U2. The envid retention settings are declared but never read (P2)

**Evidence**
- `config.descriptor.json5:1437-1452` declares `envids.check_period_minutes` and
  `envids.store_duration_minutes` ("Minutes an environment context is kept after
  its last use").
- `config.json5.example:716-717` ships both with the defaults `600` and `60000`.
- `core/envid.py:264-275` (`save_context`) writes `{ns}:ctx:<id>` with a plain
  `SET` — **no TTL**. No cron job prunes the key; the only `expire` call in the
  project is for the OpenCode sessions (`core/opencode_session.py:99`).

**Impact**
The saved conversation context of every envid lives in Redis forever. The
configuration advertises a retention that does not exist.

**Proposed modification**
1. Implement the retention: a cron job that scans `{ns}:ctx:*` and removes the
   contexts whose `saved_at` is older than `store_duration_minutes`, and whose
   envid was not used within the window. Honor `check_period_minutes` as the
   interval of that job.
2. Alternatively, set a TTL on the context key when it is written, using
   `store_duration_minutes`. The explicit job is preferable: it keeps the key
   visible in Redis and gives the operator a log line.
3. Log the removal, the same way the log-retention job logs its result.

**Validation**
- A new test: a context older than the retention window is removed by the cycle;
  a fresh one is kept.

---

### U3. `logging.logs` / `logging.log_overrides` is read but not declared (P2)

**Evidence**
- `core/cron.py:47-80` (`_log_override_cfg`) reads `logging.logs`, falling back to
  `logging.log_overrides`, and uses `wipe_period`, `wipe_max_age`, `trim_period`
  and `max_log_size` per file name or stem.
- `config.descriptor.json5:340-507` declares the `logging` section and does not
  declare either key.

**Impact**
The per-file log overrides work when they are present in the file, but the
settings editor refuses to set them: an undeclared key is a hard error. The
feature is therefore invisible and unusable through the supported path.

**Proposed modification**
1. Declare `logging.logs` in the descriptor as an open map
   (`additional_children`) of `{ <file-stem>: { wipe_period, wipe_max_age,
   trim_period, max_log_size } }`, with the numbers bounded.
2. Keep `logging.log_overrides` as a declared legacy alias, or delete the read in
   `core/cron.py` — but do not leave a read path for an undeclared key.

**Validation**
- `test_config_descriptor.py`: the new key is covered, and a configuration with it
  loads without issues.

---

### U4. The `logging.levels` keys use the plural while the logger type is singular (P2)

**Evidence**
- `core/logger.py:140` resolves the per-file threshold through
  `logging.levels.<type>`, and `core/logger.py:155` resolves the `all.log`
  threshold through `logging.alllevels.<type>`. The type is the logger's own
  argument: `system`, `core`, `worker`, `webui`, `audit`, `http`, `hooks`.
- `config.json5.example:68-78` ships `"levels": { "workers": "warning", "http":
  "warning", "webui": "warning" }` and `"alllevels": { "core": 3, "workers": 3,
  "http": 3, "webui": 3 }`.

**Impact**
`logging.levels.workers` and `logging.alllevels.workers` never match the type
`worker`. `worker.log` falls back to the global `logging.level`, so an operator
who believes the worker log is at `warning` is wrong. The same mistake is easy to
repeat for every other subsystem.

**Proposed modification**
1. Rename the shipped keys to the singular form (`worker`, not `workers`) in
   `config.json5.example` and in the live `config.json5`.
2. Add a startup diagnostic that lists the `logging.levels` / `logging.alllevels`
   keys that do not correspond to a known logger type, so the next plural typo is
   reported instead of silently ignored.
3. Consider making the descriptor enumerate the legal subsystem names instead of
   accepting any key (`key_enum`), so the editor cannot offer a name that will
   never match.

**Validation**
- A new test: a configured `logging.levels.worker` actually raises the worker log
  threshold, and an unknown subsystem key is reported at startup.

---

## 3. Configuration application

### U5. A reload does not reload the runtime (P1)

**Evidence**
- `core/app.py:385-391` (`Core.reload_config`) calls `self.config.load()`, which
  is `runtime.refresh()`: it re-reads the documents and rebuilds the effective
  tree. Nothing else is rebuilt.
- The workers, the endpoints and the resource model were constructed in
  `Core.start` (`core/app.py:218-256`) and keep the values they captured:
  `Endpoint_ollama.__init__` stores `self._cfg = endpoint_cfg`
  (`core/endpoints/endpoint_ollama.py:57`), and the scheduler keeps its
  `workers_cfg` and `full_config`.
- The descriptor marks `endpoints[].worker`, `models.providers.*.models[]`,
  `webui.request_timeouts.*`, `users.items[]`, `tasks.*` and `envids.*` as
  `apply: "reload"` (`config.descriptor.json5`).

**Impact**
A `reload`-policy change to a worker, a provider or a user is reported as
`reload_required` and the WebUI offers a reload, but the running process keeps the
old value. The operator sees a successful reload and an unchanged behavior. This
is the most misleading defect in the set, because the apply policy promises
something the runtime does not deliver.

**Proposed modification**
1. Split the reconfiguration into explicit steps and call them from
   `Core.reload_config`:
   - re-resolve the worker configurations and re-apply `initialize()` to the
     workers whose section changed;
   - re-read the endpoint sections (`self._cfg`) and refresh the model-resolution
     caches;
   - rebuild the resource registry from the new `resources[]` while preserving the
     current reservations;
   - refresh the WebUI's auth users and request timeouts.
2. Keep the endpoint **listeners** (the ports, the bind addresses, the `api`) on
   `restart`: a listener cannot be rebound in place. The descriptor already marks
   them `restart`, so this is a matter of keeping the two consistent.
3. Log one line per reconfigured area, so the operator can see what the reload
   actually did.
4. If a step cannot be applied in place, say so in the log and in the WebUI
   response rather than silently keeping the old value.

**Validation**
- A new test: change a `reload`-policy key, call `reload_config`, and assert that
  the new value is visible to the code that reads it.
- A regression test that the `restart`-policy keys are still reported as
  `restart_required`.

---

## 4. Routing, affinity and scheduling

### U6. The scheduler's router cannot probe the OpenAI-family providers (P1)

**Evidence**
- `core/endpoints/endpoint_ollama.py:626-627` passes both
  `probe_ollama_model_availability` and `probe_openai_model_availability` to the
  `SmartRouter`.
- `core/scheduler.py:904` passes **only** the Ollama probe.
- Without the OpenAI probe, a candidate whose provider `api` is `openai`,
  `opencode-go` or `anthropic` falls into the branch at
  `core/smart_router.py:706-718`: `routing_eligible: False`, `admission:
  "provider_down"`.
- The router forgets the affinity record of a preferred candidate that is not
  `probe_ok` or not eligible (`core/smart_router.py:203-210`), and records a
  `provider_down` readiness transition (`core/kv_affinity.py:503-554`).

**Impact**
The pre-dispatch refresh (and the post-failure fallback) misclassifies every
OpenAI-family candidate as down. The route itself survives — the
`SmartRouteError` is swallowed at `core/scheduler.py:751-752` — but the affinity
record of that candidate is **silently deleted** and its readiness history is
polluted. A conversation that was pinned to an OpenAI-family executor loses its
pin on the very next scheduling pass. This is the most significant integration gap
found in the whole subsystem.

**Proposed modification**
1. Give the scheduler's router the same OpenAI-family probe the endpoint has. The
   probe is a `GET {baseUrl}/v1/models`; the scheduler already holds the full
   configuration, so it can build it without a new dependency.
2. Make the router distinguish "the provider is down" from "this router cannot
   probe this provider": a candidate that cannot be probed must not invalidate an
   affinity record and must not produce a `provider_down` transition.
3. Add a `probe_source` value that names the reason a candidate was not eligible,
   so the readiness history can distinguish an outage from an unprobed provider.

**Validation**
- A new test: the scheduler's router marks an `openai` candidate as eligible when
  the provider lists the model, and the affinity record of a preferred
  OpenAI-family candidate survives a pre-dispatch refresh.

---

### U7. The affinity store is process-local (P2)

**Evidence**
- `core/kv_affinity.py:251-292` — `KvAffinityStore` keeps `_records` and `_order`
  in the process memory, with a TTL, a per-alias LRU and a global LRU.
- `proposal_kv_keeper.md:344-355` records the deliberate decision not to mirror it
  in Redis.

**Impact**
A restart loses every mapping. Two aidir instances on the same machine cannot
share a mapping, so a conversation that hops between them loses its KV cache
every time. The store is also the only place where the "same prefix → same
executor" knowledge lives, so it is a single point of loss.

**Proposed modification**
1. Mirror the store in Redis under `{ns}:kv-affinity:{alias}` with a per-entry
   TTL and a Redis-side sorted-set index for the LRU eviction, keeping the
   in-process cache as the hot path.
2. Keep the local store authoritative for the *current* process, and treat the
   Redis copy as best-effort: a Redis failure must degrade to "no affinity", not
   to an error.
3. Preserve the property that the store never holds prompt text: the Redis value
   is the truncated HMAC and the byte length, exactly as in memory.

**Validation**
- A new test: a record written by one store instance is found by another through
   the Redis mirror, and the mirror is dropped cleanly when Redis is unavailable.

---

### U8. The fingerprint is not forwarded across instances (P2)

**Evidence**
- `core/endpoints/endpoint_ollama.py:376` reads the `X-Aidir-KV-Key` header into
  `task.config["kv_client_key"]` — the **client** key only.
- `proposal_kv_keeper.md:761-788` describes forwarding the computed fingerprint
  across the hop, so the receiving instance can look the prefix up.
- The route-trace headers (`X-Aidir-Route-Id`, `X-Aidir-Visited-Instances`) are
  forwarded, but the fingerprint is not.

**Impact**
When a request crosses to another aidir instance, the receiver recomputes the
fingerprint from its own view of the payload. Any difference in the canonical
context — a different envid, a different tool set, a differently rendered system
prompt — produces a different hash, so the receiver cannot find the mapping.

**Proposed modification**
1. Forward the computed fingerprint in a header (`X-Aidir-KV-Fingerprint`) next to
   the route-trace headers, alongside the alias, the request kind and the anchor
   length.
2. On the receiving side, use the forwarded fingerprint when it is present and
   consistent with the alias and the request kind; otherwise recompute it.
3. Never trust a forwarded fingerprint for anything but a lookup: it must not be
   able to write a mapping for an arbitrary executor.

**Validation**
- A new test: a request that crosses two instances keeps its affinity mapping at
   the receiver.

---

### U9. Only `first_available` is implemented while `direct` is declared (P3)

**Evidence**
- `core/smart_router.py:104-110` raises `SmartRouteError(code =
  "UNSUPPORTED_ROUTING_STRATEGY", status_code=400)` for any strategy other than
  `first_available`.
- `config.descriptor.json5:1108` declares the model type enum as
  `["first_available", "direct"]`.

**Impact**
A configuration that names `direct` loads cleanly, passes the descriptor, and then
fails with HTTP 400 on every request. The failure is at request time, not at
startup, so it is discovered in production.

**Proposed modification**
1. Either implement `direct` — a smart model whose `items[]` has exactly one
   candidate and whose selection is that candidate, subject to the same admission
   rules — or remove `direct` from the descriptor enum.
2. If it is implemented, it must still honor the admission rules: a direct model
   that does not fit is queued, not forced.

**Validation**
- A new test: a model with `type: "direct"` resolves to its single candidate, and
  the admission rules still apply.

---

### U10. There is no per-alias affinity opt-out (P3)

**Evidence**
- `proposal_kv_keeper.md:864-866` explicitly defers
  `models.providers.<smart>.models[].kv_affinity: false`.
- `routing.kv_affinity.enabled` is a single global switch
  (`config.descriptor.json5:110-261`).

**Impact**
An operator cannot exempt one alias from the affinity behavior — for example an
alias whose prompts are short enough that the fingerprint is meaningless, or an
alias whose executor is a stateless API.

**Proposed modification**
1. Add a per-model `kv_affinity` boolean next to the existing model fields
   (`config.descriptor.json5`, the model children block).
2. When it is `false`, the router skips the affinity lookup and the affinity
   update for that model, and the selection falls back to the plain
   `can_run_now` order.

**Validation**
- A new test: a model with `kv_affinity: false` is selected by the plain order
  even when an affinity record exists for its alias.

---

### U11. The 20-message trim breaks conversational prefix reuse (P2)

**Evidence**
- `workers/agent/openaix/app.py:167-181` keeps all `system` messages plus the last
  18 others, so the upstream never sees more than 20 messages.
- The fingerprint is computed on the **incoming** request
  (`core/kv_affinity.py:184-229`), before the trim.
- `plan_kv_keeper.md:111-113` records this as a known limit.
- The trim applies only to `openaix`; the transports that override `execute()`
  (`call_openai`, `call_llama_cpp`, `call_anthropic`, `call_opencode_go`) do not
  apply it, so the behavior differs per worker.

**Impact**
The affinity keeps a conversation on one executor, but the executor receives a
trimmed history. The KV cache the affinity preserves is the cache of a prefix the
executor never received. The affinity therefore costs a routing decision without
delivering the benefit it exists for.

**Proposed modification**
1. Make the trim configurable and move the default into the model configuration
   (`models.providers.*.models[].max_history_messages`), so a provider with a
   large context can keep the whole history.
2. Make the fingerprint use the **trimmed** context, so the hash describes what
   the executor actually receives. This is the important half: the hash and the
   payload must agree.
3. Apply the same trim rule in every transport, so the behavior does not depend
   on which worker the route selected.

**Validation**
- A new test: the fingerprint of a request whose history exceeds the limit equals
  the fingerprint of the trimmed payload that is actually sent.

---

### U12. `_select_worker` ignores the provider (P2)

**Evidence**
- `core/scheduler.py:345-360` selects the first enabled worker whose `task_type`
  matches, or the explicitly pinned `task.worker_id`.
- `core/scheduler.py:972-990` maps the resolved provider `api` to a worker, but
  that mapping is applied to the *route*, not to the worker selection itself.

**Impact**
A task pinned to a worker whose provider cannot serve the requested model is
dispatched anyway and fails at the worker level, with a confusing error. The
failure looks like an upstream error rather than a routing error.

**Proposed modification**
1. In `_select_worker`, verify that the selected worker's provider can serve the
   task's resolved model; if it cannot, fall back to the provider-mapped worker
   and log the substitution.
2. When no worker can serve the model, defer the task with an explicit reason
   rather than dispatching it.

**Validation**
- A new test: a task pinned to a worker whose provider lacks the model is
   dispatched to the provider-mapped worker, or deferred with a named reason.

---

### U13. The deferral loop is a busy poll (P2)

**Evidence**
- `core/scheduler.py:73-251` — a deferred task is re-queued with
  `next_retry_at = now + 1s` (or `+5s`), and the next pass re-probes the
  availability (`core/scheduler.py:115`) and re-resolves the smart route
  (`core/scheduler.py:110`).
- Each pass therefore costs one remote probe per candidate per blocked task per
  second.

**Impact**
Under contention the scheduler generates a steady stream of outbound HTTP probes.
On a busy instance with many blocked tasks this is a measurable load on the
upstream and on the network, and it grows with the number of candidates.

**Proposed modification**
1. Make the deferral interval adaptive: start at 1 s and grow it (with a cap)
   while the blocking condition does not change, and reset it when it does.
2. Share one probe per candidate across all the tasks deferred in the same pass,
   instead of probing per task.
3. Use the readiness history that is already collected to detect a candidate that
   is not going to become available, and lengthen its interval accordingly.

**Validation**
- A new test: the number of probes in a pass does not grow with the number of
   blocked tasks.

---

### U14. The admission lock is a single global lock (P3)

**Evidence**
- `core/resources.py:44` — one `asyncio.Lock` guards every reservation decision.

**Impact**
Every reservation decision serializes process-wide, even when the decisions
concern different resources. The lock is held across the admission evaluation and
the reservation, so a slow evaluation blocks the others.

**Proposed modification**
1. Use a per-resource lock, or a striped lock keyed by the resource id.
2. Keep the "reserve the base and the session atomically" guarantee: the base and
   the session of one model must be reserved under the same lock.

**Validation**
- A new test: the reservations of two different resources do not block each other.

---

### U15. There is no FIFO within a priority class (P3)

**Evidence**
- `core/queue_manager.py:143-149` — `ZPOPMIN` pops the lowest score; within one
   score, Redis orders the members lexicographically, that is, by task id (a
   uuid4).

**Impact**
Two tasks with the same priority are served in an arbitrary order. A user who
submits three requests with the same priority sees them served in a random
sequence.

**Proposed modification**
1. Make the ZSET score a composite value: the priority in the high bits and a
   monotonically increasing sequence number in the low bits. The score is a
   double, so the precision limit has to be respected — a 53-bit mantissa gives
   room for a priority field and a sequence that resets on restart.
2. Alternatively, keep the priority as the score and add a separate FIFO index
   per priority class.

**Validation**
- A new test: three tasks with the same priority are served in their enqueue
   order.

---

### U16. The retry state is not persisted (P2)

**Evidence**
- `core/task.py:115-155` (`to_redis_hash`) writes the id, the type, the status,
   the timestamps, the payload, the result, the error, the counters, the
   timeouts, the resource requirements, the config, the context, the preview — but
   **not** `fallbacks`, **not** `on_reject` and **not** `next_retry_at`.
- `core/queue_manager.py:72-141` restores the queued tasks from the ZSET and the
   HASH.

**Impact**
A task that is re-queued (a deferral, a retry, a fallback) and is then restored
after a restart loses its fallback chain and its retry schedule. It is
re-queued from the start of the chain with no retry counter.

**Proposed modification**
1. Persist `fallbacks`, `fallback_index`, `retry_attempt`, `next_retry_at` and
   `on_reject` in the task hash.
2. On restore, rebuild the fields and keep the retry counter, so the retry budget
   is not reset by a restart.

**Validation**
- A new test: a task that is re-queued with a fallback chain and then restored
   from Redis still carries the chain and the counter.

---

## 5. Security

### U17. The MCP endpoint has no authentication (P1)

**Evidence**
- `core/endpoints/endpoint_mcp.py:92-149` (`_handle_rpc`) and `:201-279`
   (`_handle_tool_call`) never call `_authorize_and_apply_envid`; the class
   extends `BaseEndpoint` directly and therefore does not inherit the auth helper.
- `config.json5.example` publishes the MCP endpoint on `0.0.0.0:20001`.

**Impact**
Anyone who can reach the port can list the tools and **call** them: `web_search`
and `web_fetch` consume a paid API, `http_api` reaches arbitrary configured
connectors, `external_mcp` reaches remote MCP servers. The MCP endpoint is the
most direct attack path in the system.

**Proposed modification**
1. Apply the same token check the other endpoints use: read the `x-api-key` or
   the `Authorization: Bearer` header and validate it against
   `config.users.items[].token`.
2. Add an endpoint option `require_auth` (default `true`) so a trusted-LAN
   deployment can opt out explicitly, rather than by accident.
3. On a rejected call, answer with the JSON-RPC error `-32001` and HTTP 401 —
   the JSON-RPC shape the other MCP errors already use.

**Validation**
- A new test: `tools/call` without a token is rejected when `require_auth` is
   true, and accepted when it is false.

---

### U18. The queue-state endpoints are unauthenticated and O(n) (P1)

**Evidence**
- `core/endpoints/endpoint_openaix.py:138-170` registers the four queue-state
   routes without an auth call.
- `core/queue_manager.py:312-398` computes the counters by scanning both ZSETs
   and issuing one `HGETALL` per queued task.

**Impact**
The queue depth of every provider and model is readable without a token — a
useful reconnaissance surface for an attacker and an information leak in a
multi-tenant deployment. On a busy instance the endpoint is also the most
expensive read in the system, and it is reachable without a token.

**Proposed modification**
1. Apply the token check to the four queue-state routes, with the same
   `require_auth` option as the other endpoints.
2. Reduce the cost: maintain the per-model and per-resource counters
   incrementally, or cache the aggregation for a short interval (a second or
   two) instead of recomputing it on every request.
3. Keep an unauthenticated variant only for a health probe that reveals nothing
   beyond "the endpoint is up".

**Validation**
- A new test: the queue-state routes require a token when `require_auth` is true.

---

### U19. The peer APIs default to `auth_mode: "disabled"` (P1)

**Evidence**
- `core/endpoints/endpoint_openaix.py:48-57` — both peer blocks default to
   `"disabled"` and silently coerce any unknown value to `"disabled"`.
- `proposal_recources.md:166` records the intent to change the default later.
- `README.md:399-416` documents both modes.

**Impact**
`GET /v1/tasks` publishes the active task list — the worker, the model, the
envid and a 300-character request preview — unauthenticated, on every instance
that has an `openaix` endpoint.

**Proposed modification**
1. Change the default to `"endpoint"` for both peer blocks, and keep `"disabled"`
   as an explicit opt-in.
2. Log a warning at startup when a peer block is left in the `"disabled"` mode,
   naming the block and the port, so the exposure is visible in the log and not
   only in the configuration.
3. Make the coercion of an unknown `auth_mode` a startup error rather than a
   silent downgrade to the insecure mode.

**Validation**
- A new test: an endpoint without an explicit `peer_tasks.auth_mode` requires a
   token on `/v1/tasks`.

---

### U20. There is no CSRF token (P2)

**Evidence**
- `webui/backend/app.py:805` sets the session cookie with `httponly=True,
   samesite="strict"`. A repository-wide search finds no CSRF token and no CSRF
   middleware.
- The write surface is wide: `/api/restart`, `/api/tasks/{id}/terminate`,
   `/api/tasks/{id}/extend-timeout`, `/api/resources/{id}/use`,
   `/api/resources/{id}/force-release`, `/api/cron/*`, `/api/settings/*`.

**Impact**
The protection is the cookie attribute alone. Any future relaxation of
`samesite`, or a browser that does not honor it, opens a cross-site request
forgery surface that can restart the service, terminate tasks and write the
configuration.

**Proposed modification**
1. Add a double-submit CSRF token: a random value in a readable cookie and in a
   custom request header, compared on every state-changing request.
2. Exempt the WebSocket handshake, which is protected by the origin check and the
   session cookie.

**Validation**
- A new test: a state-changing request without the CSRF header is rejected.

---

### U21. Logout leaves Bearer-authenticated sessions alive (P2)

**Evidence**
- `webui/backend/app.py:814-823` — the logout route deletes
   `aidir:session:<cookie>` and clears the cookie. It never looks at the
   `Authorization` header.

**Impact**
A client that authenticated with `Authorization: Bearer` keeps a live session in
Redis until its TTL expires (the default is 86400 s). The user believes they
logged out.

**Proposed modification**
1. Resolve the session token from the cookie **or** the header, as
   `_require_session` already does, and delete that session.
2. Consider deleting every session of the same login, which is what a user
   expects from a "log out" on a shared machine.

**Validation**
- A new test: a session established with a Bearer header is gone after the
   logout.

---

### U22. The WebUI passwords are plaintext and there is no login rate limiting (P2)

**Evidence**
- `webui/backend/app.py:627-634` compares the plaintext password with
   `hmac.compare_digest`; the value is read from `webui.auth.users[].password`.
- `webui/backend/app.py:783-807` — a failed login is logged with the attempted
   login and the client IP, but nothing limits the attempts.
- `_proposal_update_1.md` item F4 records the same finding; it was never
   implemented.

**Impact**
The configuration file holds the password in the clear (the file is git-ignored,
but it is also copied into backups and readable by anything that can read the
`.env`). The login is a plain online guessing target.

**Proposed modification**
1. Support a hashed password in the form `sha256$<hex>`; compare the hash and
   keep the plaintext form working for the existing configurations, with a
   startup warning that suggests migrating.
2. Rate-limit the login: after N failures for one login or one IP, delay or
   refuse. A simple per-login counter in Redis with a short TTL is enough.
3. Never log the attempted password (the current log records the login and the
   IP, which is correct — keep it that way).

**Validation**
- A new test: a `sha256$` password is accepted, and the Nth failed attempt is
   refused.

---

### U23. `web_fetch` has no SSRF guard (P2)

**Evidence**
- `workers/tool/web_fetch/app.py:360-444` fetches an arbitrary URL, following
   redirects, with no check on the target address.
- The SearXNG path resolves and fetches the same URL
  (`workers/tool/web_fetch/app.py:407-412`).
- `_proposal_update_1.md` item F5 records the same finding; it was never
   implemented.

**Impact**
The agent can be steered — by a prompt injection, a malicious web page or a
malicious search result — into fetching `http://169.254.169.254/...` (the cloud
metadata service), a loopback service, or a private host. The result is returned
into the agent's context, so the metadata leaks into the model's input and
possibly into the model's output.

**Proposed modification**
1. Resolve the hostname and reject the request when any resolved address is
   loopback, link-local, private or otherwise reserved, unless the target is
   explicitly allowed.
2. Add a per-connector allowlist of destinations, and apply the same guard to the
   `http_api` worker's connectors.
3. Re-check the resolved address after each redirect: a redirect to a private
   address must be rejected too.
4. Make the guard configurable, because a legitimate self-hosted SearXNG or an
   internal API is a normal case.

**Validation**
- A new test: a fetch of `http://127.0.0.1/…` and of `http://169.254.169.254/…`
   is refused; a public address is allowed; a redirect to a private address is
   refused.

---

### U24. `/api/test/agent` accepts an unbounded timeout (P3)

**Evidence**
- `webui/backend/app.py:1622-1626` — the `_timeout` field of the request body
   becomes the HTTP timeout without an upper bound.
- `_proposal_update_1.md` item F9 records the same finding; it was never
   implemented.

**Impact**
A session can hold a backend request open indefinitely. The route is behind the
login, so the exposure is limited, but a hung request holds a worker and a
connection.

**Proposed modification**
1. Clamp `_timeout` to the range `[1, webui.request_timeouts.agent_test_max]`,
   and ignore a value outside it.

**Validation**
- A new test: a timeout of `999999` is clamped.

---

### U25. The logs directory is world-readable (P3)

**Evidence**
- `logs/` and `logs/audit/` are created with the default mode `0755`.
- The logs hold the request and response bodies of every task
   (`core/audit_log.py`).
- `_proposal_update_1.md` item F10 records the same finding; it was never
   implemented.

**Impact**
Every local account on the machine can read the complete task history, including
the prompts and the answers.

**Proposed modification**
1. Create `logs/` and `logs/audit/` with the mode `0700`, and tighten the mode of
   the files inside them.
2. Have `doctor` report the mode as a finding.

**Validation**
- A new test, or a doctor check, that asserts the mode.

---

### U26. Redis has no password by default (P3)

**Evidence**
- `.env.example:10` — `REDIS_PASSWORD=` (empty).
- `docker-compose.yml:6-8` — the `--requirepass` flag is added only when the
   password is non-empty.
- The Redis port is published on `${REDIS_HOST}:${REDIS_PORT}`, default
   `127.0.0.1`.

**Impact**
On a machine where the bind address is widened, Redis is open. Redis holds the
task queue, the WebUI sessions and the envid contexts.

**Proposed modification**
1. Keep the loopback default, but have the installer generate a password when
   the Redis host is not loopback.
2. Have `doctor` report an empty `REDIS_PASSWORD` on a non-loopback bind as a
   finding.

**Validation**
- A doctor check, covered by `test_doctor.py`.

---

### U27. `exec_cmd` remains writable through the settings editor (P2)

**Evidence**
- `config.descriptor.json5:982` declares `models.providers.*.exec_cmd`.
- `core/local_server_manager.py:77-96` runs it through `shlex.split` and
   `asyncio.create_subprocess_exec`, as the aidir service user.
- The whole-configuration write endpoint is gone (U41), and the settings API
   requires a session with the write permission — so the path is now
   authenticated, but not restricted.

**Impact**
A session with the settings write permission can plant a command that the service
executes as its own user. The permission model is the only barrier.

**Proposed modification**
1. Mark `exec_cmd` as `secret: false` but restrict it to a separate permission
   (`settings_edit_exec`), or move it out of the editable document and into the
   environment (`.env`), which is not writable through the UI.
2. At minimum, require an explicit confirmation for a change to `exec_cmd` and
   log it as a security-relevant event.

**Validation**
- A new test: a session without the permission cannot write `exec_cmd`.

---

### U28. The log WebSocket accepts the token in the query string (P3)

**Evidence**
- `webui/backend/app.py:1235-1248` accepts the token as the `?token=` query
   parameter, in addition to the cookie.
- `_proposal_update_1.md` item F6 records the same finding; the cookie path was
   added, the query parameter was kept.

**Impact**
A token in a URL lands in the nginx access log and in any proxy log between the
browser and the server.

**Proposed modification**
1. Accept the token only through the cookie, and reject the query parameter.
2. If a query parameter must stay for a non-browser client, make it a separate
   mechanism with its own short-lived, single-use token.

**Validation**
- A new test: a WebSocket connection with only `?token=` is refused.

---

## 6. Observability and maintenance

### U29. The audit writer drops events under the default policy (P2)

**Evidence**
- `core/audit_log.py:683-684,717-729` — under `best_effort`, a full queue drops
   the event. The drop is counted and the health status is raised, but the
   caller is not told.
- `core/audit_log.py:676-680` — under `durable`, the caller blocks on an event
   with **no timeout**, so a stalled writer hangs the request path.

**Impact**
The audit trail can have holes without anybody noticing, and the durable mode
turns a writer problem into a request hang. Both are the wrong failure direction
for an observability subsystem.

**Proposed modification**
1. Make the drop visible to the caller: return a receipt status the endpoint
   logs, or raise the health status to `error` earlier.
2. Bound the `durable` wait with a timeout, and fall back to a drop with a loud
   log when it expires.
3. Report the drop count in the WebUI health panel, not only in the log.

**Validation**
- A new test: a full queue under `best_effort` increments the drop counter and
   raises the health status; the `durable` wait returns within the timeout.

---

### U30. The audit retention never deletes today's partition (P3)

**Evidence**
- `core/audit_log.py:354-356` — the age rule requires at least
   `max(2, retention_days)` days, the quota rule requires at least 2 days, and
   the emergency rule requires `partition_date < today`.

**Impact**
One very large day can exceed `max_total_bytes` until the following day. The
emergency limit is the only protection, and it also excludes today.

**Proposed modification**
1. Let the emergency rule delete today's partition, but only when the total
   exceeds the **emergency** limit and the partition is the only thing left that
   can be removed. Keep the ordinary rules as they are.
2. Log the emergency removal prominently.

**Validation**
- A new test: a partition older than the emergency condition is removed even when
   it is today's.

---

### U31. `cleanup_stale_tasks` is an empty stub (P2)

**Evidence**
- `core/cron.py:237-241` — the function body is a docstring and a TODO: *"implement
   when task-worker heartbeat mechanism is added"*.

**Impact**
A task that is stuck in `running` because its worker died is never detected. It
holds a resource reservation, it blocks the concurrency slot, and it appears in
the dashboard as active forever. Only a restart clears it, and a restart marks it
`SERVICE_RESTARTED`.

**Proposed modification**
1. Implement a worker heartbeat: each worker writes `{ns}:worker:<id>:heartbeat`
   with a short TTL, and the core refreshes it on every task the worker runs.
2. Implement the job: a task in `running` whose worker has no heartbeat is
   failed with `WORKER_LOST` and its reservation is released.
3. Keep the task's own `run_timeout` as the primary protection; the heartbeat is
   the fast path for a crash without a timeout.

**Validation**
- A new test: a task whose worker's heartbeat has expired is failed and its
   resources are released.

---

### U32. The cron health check only pings Redis (P3)

**Evidence**
- `core/cron.py:135-146` — the job pings Redis and logs the result; the TODO
   says *"add HTTP health checks for core endpoints when port is known"*.

**Impact**
A wedged HTTP listener — a stuck event loop, a full worker pool — is invisible to
the maintenance cycle.

**Proposed modification**
1. Add an HTTP check of the OpenAIx `/health` and the MCP `/health` routes to the
   cycle, using the configured ports.
2. Report the result in the same log line as the Redis ping.

**Validation**
- A new test: the health job probes the configured endpoints.

---

### U33. The upstream error response restores only `content-type` (P2)

**Evidence**
- `core/upstream_errors.py:39-42` — `upstream_error_response` rebuilds the
   response with the original status, body and **content type only**.
- `core/upstream_errors.py:10-32` — `build_upstream_error` captures the status,
   the body and the content type, but not the other headers.

**Impact**
The rate-limit headers (`retry-after`, `x-ratelimit-remaining`), the request id
and any vendor header are dropped. A client that respects `retry-after` cannot
see it.

**Proposed modification**
1. Capture the upstream headers in `build_upstream_error` — a small allowlist:
   `retry-after`, the `x-ratelimit-*` family, `request-id`, `x-request-id`.
2. Restore them in `upstream_error_response`.

**Validation**
- A new test: an upstream 429 with a `retry-after` header reaches the client with
   that header.

---

## 7. Protocol completeness

### U34. The Anthropic thinking signature is never emitted (P2)

**Evidence**
- `core/messages_protocol.py:719-720` — the non-streaming result emits
   `{"type": "thinking", "thinking": …, "signature": ""}`.
- `core/messages_protocol.py:942-946` — the stream encoder never produces a
   `signature_delta`.
- `core/messages_protocol.py:805-808` — the **decoder** does read
   `signature_delta` and stores it, so the information exists on the way in.

**Impact**
A client that requires a real signature for a thinking block does not get one.
The signature is an Anthropic-specific integrity value; an empty one is at best
ignored and at worst rejected.

**Proposed modification**
1. Preserve the signature through the conversion: keep it in the opaque
   `anthropic_blocks` alongside the thinking text, and emit it in the result.
2. Emit a `signature_delta` in the synthesized stream when the block carries a
   signature.
3. When the assistant turn is rewritten and the signature no longer applies, drop
   it with the warning that already exists.

**Validation**
- A new test: a request with a signed thinking block returns a result whose
   thinking block carries the same signature, and a stream emits the
   `signature_delta`.

---

### U35. `count_tokens` is a rough estimate (P3)

**Evidence**
- `core/messages_protocol.py:79-101` — `ceil(total_characters / 4)` over the
   system text, the message text and the serialized tool schemas.
- The images are not counted, and the tool schemas are counted as raw JSON.

**Impact**
The value is systematically wrong for the payloads that matter most: an image
contributes almost nothing to the character count, and a large tool schema
contributes its JSON length rather than its token count.

**Proposed modification**
1. Count the images by their dimensions (the OpenAI convention: a tile-based
   estimate), or at least by their byte size, so an image-heavy request is not
   reported as nearly free.
2. State in the route's documentation that the value is an estimate and that no
   tokenizer is invoked.

**Validation**
- A new test: a request with an image reports a token count that reflects the
   image.

---

### U36. `messages_response_to_internal` accepts an unused keyword (P3)

**Evidence**
- `core/messages_protocol.py:637` — the function takes a `streaming` keyword that
   it never reads.

**Impact**
None beyond the confusion: the signature promises a distinction the body does
not make.

**Proposed modification**
1. Remove the keyword, or implement the distinction it implies.

**Validation**
- None required; the existing tests cover the behavior.

---

## 8. Test coverage

### U37. The envid rules, the MCP endpoint and the context workers are untested (P2)

**Evidence**
- `core/envid.py` — zero references in any `test_*.py`. The `autoassign_envid`
   and the `envids` membership rules are normative in `openaix_spec.md:31-41`.
- `core/endpoints/endpoint_mcp.py` — no dedicated suite; only touched indirectly
   by `test_endpoint_audit.py:186-202`.
- `workers/context/context_builder/app.py`,
   `workers/context/context_add_internal_tools/app.py`,
   `workers/context/context_render_openclaw_style/app.py` — zero direct
   references. The three-stage context chain is exercised only indirectly.
- `workers/tool/external_mcp/app.py` — only stubbed in `test_cron_logging.py`.

**Impact**
Several normative rules have no regression protection. The context chain is the
piece that shapes every prompt the agent sends.

**Proposed modification**
1. Add a test module for the envid registry: the `autoassign_envid` rule, the
   `envids` membership check, the `INVALID_ENVID` error, the parent-chain
   resolution and the cycle detection.
2. Add a test module for the MCP endpoint: the four JSON-RPC methods, the error
   codes, the tool registry and the unknown-tool case.
3. Add a test module for the three context workers: the merge semantics, the tool
   priority order and the rendering.

**Validation**
The new modules are the validation.

---

### U38. The OpenCode Go release gates are open (P2)

**Evidence**
- `proposal_opencode_go.md:374-389` lists seven release gates and states that
   items 1–7 are release gates.
- No test file covers `core/opencode_session.py`,
   `workers/agent/call_opencode_go/app.py` or `workers/agent/call_openai/app.py`.

**Impact**
The session store, the identity digest and the encryption are untested. The
identity isolation is the security-relevant part: two different callers must not
share a session.

**Proposed modification**
1. Write the focused test module the gate asks for: the direct and the smart
   worker selection, `/v1/models` success and failure, the supplied and the
   managed session, the identity isolation, the expiry, the concurrent Redis
   creation, the cleanup, the Redis failure and the redaction.
2. Add a health-probe test: a probe never creates a session binding.

**Validation**
The new module is the validation.

---

### U39. Two CLI probes match the test discovery glob (P3)

**Evidence**
- `test_context_overflow.py` and `test_timeout_semantics_live.py` are CLI
   scripts that match the `test_*.py` pattern and contribute zero test methods.
- `TEST.md:76` claims the suite covers "context overflow"; it does not.
- `test_timeout_semantics_live.py` is the only executable check of the
   `spec_timeouts.md` semantics and is not mentioned anywhere in `TEST.md`.

**Impact**
The discovery output is misleading, and the only live check of the timeout
semantics is undiscoverable from the test protocol.

**Proposed modification**
1. Rename the probes so they do not match the glob — for example
   `probe_context_overflow.py` and `probe_timeout_semantics.py` — or move them
   into a `probes/` directory.
2. Document both in `TEST.md`, with the command that runs them.
3. Correct the "context overflow" claim in `TEST.md`.

**Validation**
- `python -m unittest discover -s . -p 'test_*.py'` lists no zero-test module.

---

### U40. `test_logs.py` mutates the working tree at import time (P3)

**Evidence**
- `test_logs.py:11-45` performs a real wipe and a real trim against `logs/`
   during the import, and prints `WIPE_TEST PASS` / `TRIM_TEST PASS`.

**Impact**
Running the test suite modifies the runtime logs. The print noise also makes the
discovery output harder to read.

**Proposed modification**
1. Move the wipe and the trim into a test method, against a temporary directory
   rather than against `logs/`.
2. Remove the prints.

**Validation**
- The discovery output is clean, and `logs/` is unchanged after a run.

---

## 9. Documentation consistency

### U41. The README lists routes that no longer exist (P3)

**Evidence**
- `README.md:646` lists `/api/config`, `/api/config/raw` and `/api/config/fields`
   under the WebUI backend routes.
- No such route exists in `webui/backend/app.py`; the whole-configuration
   endpoints were removed when the settings subsystem moved to `aiset`, precisely
   because they returned every substituted credential
   (`README.md:258-260`).

**Impact**
A reader follows the README, finds a 404, and cannot tell whether the route is
broken or the documentation is.

**Proposed modification**
1. Remove the three routes from the README's table, and point at the settings
   routes instead.
2. Review the rest of the route table against the code at the same time.

**Validation**
- A test that parses the README's route tables and asserts that every route
   exists in the application. This turns the whole table into a regression
   surface.

---

### U42. `proposal_hooks.md` contradicts the shipped hook (P3)

**Evidence**
- `proposal_hooks.md:160-163` states that both bundled hooks ship disabled.
- `hooks/llama_cpp_empty_response_recovery.py:5` has `ENABLED = True`.

**Impact**
A reader who trusts the proposal will not enable the hook — it is already enabled
— or will believe it is running when it is not.

**Proposed modification**
1. Correct the statement in `proposal_hooks.md`. **Note:** a proposal is frozen
   once it is written; the rule from `memory/context.md` is that a correction goes
   into the next numbered file. This proposal is the next numbered file for the
   project, so the correction may be recorded **here** and applied to the frozen
   document only as a separate, explicitly approved correction.

**Validation**
- A test that reads the `ENABLED` flag of every bundled hook and compares it with
   the documentation.

---

### U43. The README references a template that does not exist (P3)

**Evidence**
- `README.md:62-63` documents `./install.sh prod` as using `prod.json5.example`.
- The repository ships only `config.json5.example` and `.env.example`.
- `install.sh:485` fails with `require_file: ... not found`.

**Impact**
The documented command fails on a fresh clone.

**Proposed modification**
1. Either ship a `prod.json5.example` or remove the example from the README.
2. Note in the README that the template is selected by name and that only the
   shipped ones exist.

**Validation**
- A test that every template the README names exists in the repository.

---

### U44. The plan documents quote stale test counts (P3)

**Evidence**
- `plan_kv_keeper.md:5` — "Full suite: 574 tests pass".
- `plan_aiset_integration.md:433` — "**366 unit tests pass**".
- The actual number is 586.

**Impact**
Small, but a reader uses these numbers to judge whether a document is current.

**Proposed modification**
1. Remove the counts, or replace them with a statement that does not go stale.
   A count in a historical plan document has no value after the next change.

**Validation**
- None required.

---

### U45. Several proposal status lines are stale (P3)

**Evidence**
- `proposal_kv_keeper.md:5` — "Proposed, not implemented", although the feature is
   shipped (`core/kv_affinity.py`, three test suites).
- `proposal_searxng.md:3` — "## Status: proposal", although the provider chain is
   shipped (`workers/tool/web_search/app.py:114-131`,
   `workers/tool/web_fetch/app.py:160-177`).
- `_proposal_embedding.md:5` — "Proposed", although the embeddings are shipped
   (`openaix_spec.md:292-306`, `test_openaix_embeddings.py`).
- `proposal_ollama_embed.md` — no status line at all.
- `proposal_anthropic.md` — no status line; the "Decision" section reads as if
   nothing shipped.

**Impact**
`DOCUMENTS.MD` states that the index describes responsibilities and not the
implementation status, and that a proposal may contain both planned and completed
work — so a reader who consults a proposal for the status gets "not implemented"
for a shipped feature.

**Proposed modification**
1. Add a **Status** line to each of the five documents above, recording the real
   state and the date.
2. Add a status column to the proposal table in `SPEC_NEW.md` §14.4, so the
   authoritative state lives in the specification and the proposal keeps its own
   history.
3. Add the same status line to `proposal_opencode_go.md` (seven gates open) and
   to `proposal_aidir_smart_router.md` (phases 1–2 delivered).

**Validation**
- None required.

---

## 10. Deferred — not in this round

Two items of `SPEC_NEW.md` §14 are **deferred** and are deliberately **not**
part of this proposal:

| Item | Why it is deferred |
|---|---|
| The middleware chain (`middleware.items`, `endpoints[].middlewares`) | The configuration surface promises a chain that the runtime never executes. The decision — implement it, or delete it from the descriptor — is a design decision that deserves its own proposal, not a line in a defect list. It is recorded as a deferred task in `SPEC_NEW.md` §14.4. |
| The `before_tools` / `context_compressor` extension | It is a **new feature**, fully designed in `before_tools_worker.md`, with five open TBDs and no implementation. It is not a defect and it does not belong in a defect round. It is recorded as a deferred task in `SPEC_NEW.md` §14.4. |

---

## 11. Already sound — no action needed

The following were reviewed and are correct as they stand; they are listed so a
future round does not re-investigate them.

- **Constant-time comparison** for the API tokens and the WebUI passwords
  (`core/endpoints/endpoint_ollama.py:339`, `webui/backend/app.py:137-139`).
- **The path traversal guards**: the log file resolution
  (`webui/backend/app.py:176-192`) and the audit file serving
  (`webui/backend/app.py:1016-1020`) both reject an absolute path and `..`.
- **The audit ownership check**: `/api/tasks/viewer/{id}/raw` verifies both the
  `task_id` and the `type`, so a task cannot read another task's event by guessing
  an id (`webui/backend/app.py:992-1003`).
- **The upstream process ownership proof**: the PID plus the start ticks from
  `/proc/<pid>/stat` prevent acting on a recycled PID
  (`core/local_server_manager.py:242-260`).
- **The source-preserving configuration writes**: the comments, the quoting and
  the placeholders survive every write.
- **The secret model of the settings API**: a secret subtree is denied as a whole,
  not redacted field by field.
- **The `aiset` write protocol**: an atomic, revision-checked, lock-protected
  write with a bounded backup chain.
- **The admission atomicity**: the base and the session of one model are reserved
  under the same lock (`core/resources.py:388-427`).
- **The upstream error transparency**: the original status, body and content type
  are returned to the client when the protocols match.
- **The affinity privacy**: the router stores a truncated HMAC and a byte length,
  never prompt text.
- **The hook safety rules**: a failing hook never changes the task outcome, and a
  hook cannot fabricate a successful response.
- **The subprocess launch**: `shlex.split` plus `create_subprocess_exec`, never a
   shell.
- **The front-end escaping**: the task data is inserted as text, never as HTML;
   the assistant's answer is inserted with `textContent`.

---

## 12. Suggested implementation order

The order is by *risk closed per unit of work*, and each step is independently
shippable.

| Round | Items | Why |
|---|---|---|
| **1** | U1, U4, U17, U18, U19 | The configuration surface says one thing and does another, and three unauthenticated surfaces are reachable. Small, local, testable. |
| **2** | U5 | The apply policy currently promises something the runtime does not do. It touches the workers, the endpoints and the resources, so it deserves its own round and its own regression suite. |
| **3** | U6, U12 | The routing correctness: the OpenAI-family probe and the provider-aware worker selection. Both are local to the router and the scheduler. |
| **4** | U16, U31, U33, U34 | The task and the protocol completeness: the persisted retry state, the stale-task cleanup, the upstream headers and the thinking signature. |
| **5** | U22, U23, U25, U26, U27, U29, U30 | The security and the observability hygiene. Each is small; they are grouped because none of them blocks another. |
| **6** | U7, U8, U11 | The distributed affinity: the Redis mirror, the fingerprint forwarding and the trimmed-context hash. They are one feature and must land together. |
| **7** | U2, U3, U13, U14, U15, U20, U21, U24, U28, U32, U35, U36 | The remaining items, in any order. |
| **8** | U37, U38, U39, U40, U41, U42, U43, U44, U45 | The test coverage and the documentation consistency. They can run in parallel with the earlier rounds. |

Two rules that apply to every round:

1. **A round must not change a documented behavior without changing the document
   in the same commit.** The route tables, the descriptor and the apply policies
   are the contract; the code follows them.
2. **A round must not leave a half-implemented promise.** Either the item is done
   and observable, or it is not started.

---

## 13. Testing plan

- Every item above carries a **Validation** clause. Implement it as a test in the
  module that already owns the area, or create a new module when the area has
  none.
- The descriptor items (U1, U2, U3) are gated by `test_config_descriptor.py`,
  which must pass with the new keys and must fail without the declaration.
- The routing items (U6, U9, U10, U11, U12) belong with
  `test_kv_affinity_routing.py` and `test_openaix_tool_args_parser.py`.
- The security items (U17, U18, U19, U20, U21, U23, U28) need a test that
  asserts the **rejection**, not only the success path. A test that only checks
  the happy path passes with the guard removed.
- The observability items (U29, U30, U31) need a test that creates the condition
  (a full queue, an oversized partition, a dead worker) rather than a mock of it.
- The documentation items (U41, U43) are best enforced by a test that parses the
  documents and compares them with the code, so the next drift is caught by the
  suite.
- After each round: `python -m unittest discover -s . -p 'test_*.py' -v`, then
  the smoke checks from `TEST.md` §5, then the manual acceptance matrix from
  `TEST.md` §7 for anything that touches the HTTP surface.
