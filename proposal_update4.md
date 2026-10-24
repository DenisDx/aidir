# Proposal: aidir Update Round 4

Date: 2026-10-23. Status snapshot: 2026-10-24.

Scope: the complete Round 4 ledger. It collects every item that survived the two
previous rounds ([proposal_update1.md](proposal_update1.md),
[proposal_update_3.md](proposal_update_3.md)) and every item that the older
documents still carried. The status snapshot in §1.1 lists the three items
that remain.

Constraint: this document is analysis + proposals only. It is the latest
`proposal_update` and therefore stays open for status changes until a
higher-numbered proposal supersedes it. Older `proposal_update*` documents are
closed.

Item numbering uses the `W` prefix so that it does not collide with the `U`
items of `proposal_update1.md` or the `V` items of `proposal_update_3.md`. Every
item names its source, so the ledger can be checked against the older documents.

Priority meaning:

| Priority | Meaning |
|---|---|
| **P1** | A correctness or security problem that makes a documented behavior false |
| **P2** | A reliability problem that bites under load or after a restart |
| **P3** | A completeness or consistency problem that misleads a reader or an operator |

---

## 1. Summary

| # | Area | Pri | Item | Source |
|---|---|---|---|---|
| W1 | Security | P2 | Closed by decision: `require_auth` stays `false` for every API; the doctor reports every open surface and the dashboard panel is a separate proposal | U17, U18 (the auth half) |
| W2 | Security | P2 | Closed: root `default_auth_mode` is implemented, and an explicit peer `auth_mode` overrides it | U19 |
| W3 | Security | P2 | Deferred to ROADMAP: browser-derived credentials and login rate limiting | U22 |
| W4 | Security | P2 | Deferred to ROADMAP: `web_fetch` has no SSRF guard | U23 |
| W5 | Security | P2 | Deferred to ROADMAP: `exec_cmd` is writable through the settings editor | U27 |
| W6 | Security | P3 | Closed: the log WebSocket authenticates only with the session cookie | U28 |
| W7 | Security | P2 | Deferred to ROADMAP: there is no CSRF token | U20 |
| W8 | Security | P2 | Closed: logout deletes the Bearer token session too | U21 |
| W9 | Security | P2 | Closed: the legacy session namespace is no longer accepted | V9 |
| W10 | Routing | P2 | Closed: the scheduler passes `probe_openai_model_availability`, so OpenAI-family candidates are probed in the pre-dispatch refresh. |
| W11 | Routing | P2 | Closed: the in-process store is the hot path and records are mirrored to Redis with their TTL | U7, V11 |
| W12 | Routing | P2 | Closed: route metadata carries the full fingerprint, the OpenAIx worker sends `X-Aidir-KV-Fingerprint`, and the receiver records it. The `openaix` history trim was removed so both sides see the same payload. |
| W13 | Routing | P3 | Closed: the unconditional 20-message trim is removed, so the executor receives the payload the router fingerprinted. Model context limits remain the provider's responsibility. |
| W14 | Routing | P3 | Closed: both `first_available` and the single-candidate `direct` strategy are implemented | U9 |
| W15 | Routing | P3 | Closed: affinity depends only on the context, so there is no per-alias partition | U10 |
| W16 | Scheduling | P2 | Closed: remote probes are cached by W45, concurrent local candidate probes share one in-flight evaluation, and the deferral loop still waits one second when it dispatches nothing. |
| W17 | Scheduling | P2 | Closed: the worker named by the resolved route wins over an incompatible pinned worker. |
| W18 | Scheduling | P3 | Closed: admission acquires one lock per touched resource in sorted order; an empty requirement set keeps the global lock. |
| W19 | Scheduling | P3 | Closed: the queue score is priority-major and arrival-minor, so equal priorities are FIFO. |
| W20 | Task model | P2 | Closed: `fallbacks`, `next_retry_at` and `on_reject` survive a Redis round trip | U16, V20 |
| W21 | Task model | P2 | Closed: the cron job `prune_envid_contexts` scans `{ns}:ctx:*`, compares `saved_at`, and deletes contexts older than `envids.store_duration_minutes`. |
| W22 | Observability | P2 | Closed: durable waits are bounded and recent drops are shown in the health panel | U29 |
| W23 | Observability | P2 | Closed: a running task older than its run timeout plus a 60-second safety margin is failed with `STALE_TASK`. |
| W24 | Observability | P3 | Closed: allowlisted rate-limit and request-id headers are restored | U33 |
| W25 | Protocol | P2 | Closed: thinking signatures survive conversion and are emitted as `signature_delta` | U34 |
| W26 | Protocol | P3 | Closed: the estimate adds 1600 tokens per image block and the protocol document says that it is an estimate. |
| W27 | Interface | P2 | Closed: the dashboard forwards a remote cancel to the provider that holds the task | V13 |
| W28 | Interface | P3 | Closed: the orphan count is already rendered by the existing health card | V15 |
| W29 | Interface | P3 | Closed by decision: deleting another instance's data is the operator's action; the check reports only. |
| W30 | Tests | P2 | Closed: `test_context_components.py` covers the registry, MCP and all three context workers | U37 |
| W31 | Tests | P2 | Open: the OpenCode Go release gates include focused managed-session/redaction tests and a live smoke test that needs an external deployment | U38 |
| W32 | Tests | P3 | Closed: renamed to `probe_context_overflow.py` and `probe_timeout_semantics_live.py`; `TEST.md` lists both probes. |
| W33 | Documentation | P3 | Closed: the removed routes are gone from the table. |
| W34 | Documentation | P3 | Closed by policy: older non-update proposals keep their own status and are not edited in place | U42 |
| W35 | Documentation | P3 | Closed: the README uses only the shipped `config.json5.example` and an explicit filename example. |
| W36 | Documentation | P3 | Closed: the plans no longer quote a fixed suite count. |
| W37 | Documentation | P3 | Closed by policy: proposal status ownership stays with each non-update proposal | U45 |
| W38 | Configuration | P3 | Closed: both per-file log override maps are declared | U3 |
| W39 | Security | P3 | Closed: `_timeout` is clamped by `webui.request_timeouts.agent_test_max` | U24 |
| W40 | Security | P3 | Closed: log directories are `0700`, new journals are `0600`, and the doctor checks the mode | U25 |
| W41 | Security | P3 | Closed: a non-loopback bind gets a generated password, and the doctor warns when unprotected | U26 |
| W42 | Observability | P3 | Closed: emergency retention may remove today when it is the sole partition | U30 |
| W43 | Observability | P3 | Closed: the cron health check also probes `/health` on every configured `openaix` and `mcp` endpoint. |
| W44 | Protocol | P3 | Closed: the unused keyword is removed | U36 |
| W45 | Routing | P3 | Closed: remote probes are cached for one second and concurrent probes for the same provider/model/priority share one in-flight call. |
| W46 | Protocol | P2 | Closed by decision: each attempt gets the full run budget and inter-attempt waits are not charged to it | `spec_timeouts.md` TBD |
| W47 | Acceptance | P2 | Open: the generated editor still needs a human browser acceptance through nginx | `plan_aiset_integration.md`, `TEST.md` 7.1 |
| W48 | Tooling | P3 | Open (P3): several selectable `<name>.json5.example` sets and installation scripts for a full configuration set | `TODO.md` |
| W49 | Documentation | P3 | Closed by policy: the historical analysis is not rewritten; later rounds record their resolutions in their own ledgers | `_proposal_update_1.md` F1–F13 |

### 1.1 Status snapshot (2026-10-24)

The Round 4 implementation and documentation pass is complete except for the
three items below. Of the 49 ledger items, 42 are closed, 4 are deferred to
[ROADMAP.md](ROADMAP.md) (W3, W4, W5, W7), and 3 remain open: W31 (external
OpenCode Go release gates including a live smoke), W47 (human browser
acceptance), and W48 (P3 example configurations and installation scripts).
No other item is waiting for code or documentation changes.

### 1.2 Decisions of 2026-10-24

| Item | Decision | State |
|---|---|---|
| W1 | `require_auth` keeps its default `false`, including the MCP API. The doctor `auth` check must report an MCP endpoint without a token. A dashboard security panel is a separate proposal. | Doctor coverage already exists; panel proposed in [proposal_security_dashboard.md](proposal_security_dashboard.md). |
| W2 | Add root `default_auth_mode`. Its default is `endpoint`; the shipped configurations set `disabled` until peers carry tokens. An explicit peer `auth_mode` overrides it. | Implemented in this round. |
| W9 | Remove the legacy session fallback now; only the instance-scoped key is valid. | Implemented in this round. |
| W14 | Implement `direct` as a smart model with exactly one candidate, subject to normal admission. | Implemented in this round. |
| W15 | Affinity depends only on the request context. Alias and client keys no longer affect the identity. | Implemented in this round; per-alias opt-out is obsolete. |
| W3 | Replace the plaintext password login with a browser-side password-derived key pair. The server stores only a public key, salt and KDF parameters. The password is never sent to the server. | Deferred to [ROADMAP.md](ROADMAP.md); the interim `sha256$` scheme is withdrawn. |
| W4 | `web_fetch` SSRF protection is in scope but deferred. | Moved to [ROADMAP.md](ROADMAP.md). |
| W5 | `exec_cmd` through the settings editor is deferred; it changes the settings contract. | Moved to [ROADMAP.md](ROADMAP.md). |
| W7 | CSRF protection is deferred; it needs a design that covers the settings editor, WebSockets and non-cookie clients. | Moved to [ROADMAP.md](ROADMAP.md). |
| W20 | `fallback_index` and `retry_attempt` were already persisted. The missing fields are `fallbacks`, `next_retry_at` and `on_reject`. | Corrected and implemented in this round. |
| W28 | The orphan count is already in the execution-health payload and the existing card renders it. A separate health-card row is not wanted. | Closed. |
| W34, W37, W49 | The new lifecycle rules supersede the old freeze rule: older `proposal_update*` documents are closed, while non-update proposals keep their own status. The historical security analysis is not edited in place. | Resolved by policy; no content edits to closed documents. |

**Deferred, unchanged:** the middleware chain and the `before_tools` extension.
They are recorded in [SPEC.md](SPEC.md) §14.4 and need their own proposals.

**Not ours:** the items of [aiset/proposal_improvement2.md](aiset/proposal_improvement2.md).
`aiset` is a separate project with its own owner; nothing here blocks aidir.

---

## 2. What the previous rounds closed

This is the counterpart of the table above: the items of the two earlier
proposals that are **no longer** open. It is recorded here so that the ledger can
be read in one place.

| Source | Closed by | What changed |
|---|---|---|
| U1 | `config.descriptor.json5` | `endpoints[].peer_resources` is declared and read |
| U4 | the shipped configuration | the log thresholds use the singular `worker`; a startup diagnostic names the unknown subsystems |
| U5 | `core/app.py` | the reload re-initializes the workers, updates the resource registry in place and reports the structural sections; the SIGHUP handler schedules the coroutine instead of dropping it |
| U40 | `test_logs.py` | the wipe and the trim run inside a test, against the log directory the logger actually uses |

**Closed after the Round 4 decisions:**

| Source | Closed by | What changed |
|---|---|---|
| U17 | W1 | the MCP endpoint can require a token; the default stays off and the doctor reports every open surface |
| U18 | W1 | the queue-state aggregation is cached for a second, and the route authentication is left to `require_auth` |
| U19 | W2 | the peer surfaces default to `endpoint`, the root `default_auth_mode` is configurable, and an explicit peer mode wins |

Every other U item is open and is listed in §1 with its W number.

**The V items are all closed.** They are the two production investigations of the
previous round:

| Source | Closed by | What changed |
|---|---|---|
| V1 | `core/settings/runtime.py` | the `.env` reaches the process environment again |
| V2 | `core/local_server_manager.py` | the variables of `exec_cmd` expand again |
| V3 | `core/settings/runtime.py` | the secrets and the fallbacks are populated again |
| V4 | `config.descriptor.json5` | a provider may declare `env` for its managed server |
| V5 | `core/local_server_manager.py` | the output of the managed server is read through a pipe |
| V6 | `core/local_server_manager.py` | a failed startup reports the exit code and the output tail |
| V7 | `core/local_server_manager.py` | the output of the managed server reaches the aidir log |
| V8 | `core/local_server_manager.py` | the managed server gets `stdin=DEVNULL` |
| V9 | `core/scheduler.py` | a remote that cannot run the model is excluded and the route is re-resolved |
| V10 | `core/scheduler.py` | the affinity no longer pins a task to a candidate that cannot run |
| V11 | `core/smart_router.py` | a remote candidate is measured over the network; the OpenAI-family half is closed by W10 |
| V12 | the log thresholds and the health card | the deferral reasons reach the operator through the status and the health card, not only through the log |
| V13 | the task summary | the waiting reason, the provider and the model travel with the task; the remote-aware cancel is closed by W27 |
| V14 | `core/queue_manager.py` | `terminate_task` reaches a task that only exists as a Redis hash |
| V15 | `core/cron.py` | the cron cycle fails the orphans; the health row is closed by the W28 decision |
| V16 | `core/queue_manager.py` | a quarantined task is marked failed |
| V17 | `webui/backend/app.py` | the sessions are namespaced by the instance; the legacy fallback is removed by W9 |
| V18 | `core/doctor.py`, `core/settings/runtime.py` | the settings runtime is isolated from the process environment, and the doctor resolves the inspected root's own `.env` |
| V19 | `core/app.py` | a planned restart is an event, not a traceback |
| V20 | `core/local_server_manager.py`, the health card | the startup errors of the providers are in the health card |
| V21 | `core/scheduler.py` | a remote that stops answering never fails the task |
| V22 | `core/endpoints/endpoint_openaix.py` | a request cannot remove the timeout of its own task |
| V23 | `core/queue_manager.py` | a removed task does not keep `queued` |
| V24 | `core/cron.py` | the cron cycle reaps the orphaned tasks |
| V25 | `core/app.py` | the planned restart keeps the exit code the supervisor needs |
| V26 | `webui/backend/app.py` | the live log socket no longer holds the graceful shutdown back |
| V27 | `core/logger.py` | the logger honours `AIDIR_LOG_DIR`; the suite writes into a temporary directory |
| V28 | `webui/frontend/app.js`, `webui/backend/app.py` | the restart banner survives the whole restart |

---

## 3. The items in detail

### 3.1 Security

| # | Problem | Where | Note |
|---|---|---|---|
| W1 | The MCP endpoint, the queue-depth routes and the model lists can require a token, but the default stays off | `core/endpoints/endpoint_mcp.py`, `core/endpoints/endpoint_openaix.py` | The MCP endpoint had **no** authentication at all, so requiring one closes a hole rather than changing a contract. The inference traffic of the OpenAIx endpoint has always accepted an anonymous request, and flipping that default would break every deployment without a token. Decision: keep `require_auth` at `false` for every API, including `mcp`. The doctor check `auth` reports every open surface with the exact configuration to add. The dashboard security panel is a separate proposal. |
| W2 | The peer task list and the peer resource inventory need a configurable default authentication mode | `core/endpoints/endpoint_openaix.py`, `webui/backend/app.py` | The reading side — the dashboard's peer fetch and the scheduler's peer probe — already sends the provider's `auth` headers. The configuration of the two `api: "openaix"` providers on the investigated machine carries **no** credentials, so those peers are currently unreadable. Decision: the root `default_auth_mode` key controls the default for every peer surface. It defaults to `endpoint`; the shipped configurations set `disabled` until the reading side carries tokens. An explicit peer `auth_mode` overrides it. A peer that answers `401` keeps the existing local-estimation fallback. |
| W3 | The WebUI passwords are plaintext and there is no login rate limiting | `webui/backend/app.py` | Deferred. The planned replacement derives a signing key pair in the browser from the password and a per-user salt; the server stores only the public key. The browser signs a fresh challenge, so the password never leaves the client. Rate limiting remains required. See [ROADMAP.md](ROADMAP.md) for the threat model, migration and recovery questions. |
| W4 | `web_fetch` has no SSRF guard | `workers/tool/web_fetch/app.py` | Deferred. Reject a loopback, private or link-local target, and re-check after every redirect. See [ROADMAP.md](ROADMAP.md). |
| W5 | `exec_cmd` is writable through the settings editor | `config.descriptor.json5` | Deferred. Either a dedicated permission, or move it out of the editable document into the environment. See [ROADMAP.md](ROADMAP.md). |
| W6 | The log WebSocket accepts the token in the query string | `webui/backend/app.py` | Closed: only the session cookie is accepted; the query parameter is gone. |
| W7 | There is no CSRF token | `webui/backend/app.py` | Deferred. A double-submit token on every state-changing request. The only protection today is `samesite=strict`. See [ROADMAP.md](ROADMAP.md). |
| W8 | Logout deletes only the cookie's session | `webui/backend/app.py` | Closed: the Bearer token is resolved with the same helper as authentication and its session is deleted. |
| W9 | The old session namespace is still accepted | `webui/backend/app.py` | Closed: the legacy fallback is removed; only the instance-scoped key is accepted. |
| W39 | `/api/test/agent` accepts an unbounded client-supplied timeout | `webui/backend/app.py` | Closed: `_timeout` is constrained to `[1, webui.request_timeouts.agent_test_max]`. |
| W40 | The log directory is world-readable | `logs/` | Closed: log directories are created or tightened to `0700`, new journals use `0600`, and the doctor reports and fixes the mode. |
| W41 | Redis has no password by default | `.env.example`, `docker-compose.yml` | Closed: the installer generates a password for a non-loopback bind, the Compose health check authenticates, and the doctor warns about an unprotected widened bind. |

### 3.2 Routing and scheduling

| # | Problem | Where | Note |
|---|---|---|---|
| W10 | The scheduler's router cannot probe the OpenAI-family providers | `core/scheduler.py` | Closed: the scheduler passes `probe_openai_model_availability`, so OpenAI-family candidates are probed in the pre-dispatch refresh. |
| W11 | The affinity store is process-local | `core/kv_affinity.py` | Closed: records are mirrored to Redis with their TTL; the in-process store stays the hot path and a local miss falls back to the mirror. |
| W12 | The fingerprint is not forwarded across instances | `core/endpoints/endpoint_ollama.py` | Closed: route metadata carries the full fingerprint, the OpenAIx worker sends `X-Aidir-KV-Fingerprint`, and the receiver records it. The `openaix` history trim was removed so both sides see the same payload. |
| W13 | The 20-message trim breaks prefix reuse | `workers/agent/openaix/app.py` | Closed: the unconditional 20-message trim is removed, so the executor receives the payload the router fingerprinted. Model context limits remain the provider's responsibility. |
| W14 | The descriptor promised `direct`, but only `first_available` was implemented | `core/smart_router.py` | Closed: `direct` requires exactly one candidate and resolves under the normal admission rules. A different count is HTTP 400. |
| W15 | The affinity identity was partitioned by alias, with no opt-out | `core/kv_affinity.py` | Closed by making the identity context-only: alias and client keys no longer partition the store, so a per-alias switch is unnecessary. `max_entries_per_alias` and `honor_prompt_cache_key` are deprecated and ignored. |
| W16 | The deferral loop is a busy poll | `core/scheduler.py` | Closed: remote probes are cached by W45, concurrent local candidate probes share one in-flight evaluation, and the deferral loop still waits one second when it dispatches nothing. |
| W17 | `_select_worker` ignores the provider | `core/scheduler.py` | Closed: the worker named by the resolved route wins over an incompatible pinned worker. |
| W18 | The admission lock is a single global lock | `core/resources.py` | Closed: admission acquires one lock per touched resource in sorted order; an empty requirement set keeps the global lock. |
| W19 | Equal-priority tasks are ordered by task id | `core/queue_manager.py` | Closed: the queue score is priority-major and arrival-minor, so equal priorities are FIFO. |
| W45 | The remote probe has no cache and no in-flight deduplication | `core/smart_router.py` | Closed: remote probes are cached for one second and concurrent probes for the same provider/model/priority share one in-flight call. |

### 3.3 The task model and the observability

| # | Problem | Where | Note |
|---|---|---|---|
| W20 | The retry state is not fully persisted | `core/task.py` | Closed: `fallbacks`, `next_retry_at` and `on_reject` now survive a Redis round trip, together with the already-persisted `fallback_index` and `retry_attempt`. |
| W21 | The envid contexts never expire | `core/envid.py` | Closed: the cron job `prune_envid_contexts` scans `{ns}:ctx:*`, compares `saved_at`, and deletes contexts older than `envids.store_duration_minutes`. |
| W22 | The audit writer drops events | `core/audit_log.py` | Closed: `durable` enqueue and receipt waits are bounded, and `dropped_recent` is shown in the execution-health line. `best_effort` still drops visibly. |
| W23 | `cleanup_stale_tasks` is a stub | `core/cron.py` | Closed: a running task older than its run timeout plus a 60-second safety margin is failed with `STALE_TASK`. |
| W24 | The upstream error response restores only `content-type` | `core/upstream_errors.py` | Closed: `retry-after`, `request-id`, `x-request-id` and `x-ratelimit-*` are captured and restored. |
| W42 | The retention never deletes today's partition | `core/audit_log.py` | Closed: emergency retention may remove today when it is the only partition left, with a warning. The ordinary age and quota rules still require two days. |
| W43 | The health check only pings Redis | `core/cron.py` | Closed: the cron health check also probes `/health` on every configured `openaix` and `mcp` endpoint. |

### 3.4 The protocol

| # | Problem | Where | Note |
|---|---|---|---|
| W25 | The Anthropic thinking signature is never emitted | `core/messages_protocol.py` | Closed: signed thinking blocks survive conversion and the encoder emits `signature_delta` before closing the thinking block. |
| W26 | `count_tokens` is a `chars/4` heuristic | `core/messages_protocol.py` | Closed: the estimate adds 1600 tokens per image block and the protocol document says that it is an estimate. |
| W44 | The unused `streaming` keyword | `core/messages_protocol.py` | Closed: removed. |
| W46 | The retry/fallback wait budget is undefined | `spec_timeouts.md` | Closed by decision: each attempt gets a full run-timeout budget, the wait between attempts is not charged to it, and no separate retry-wait timeout is introduced. |

### 3.5 The operator's view

| # | Problem | Where |
|---|---|---|
| W27 | A task that waits on a remote cannot be canceled where it is | Closed: the dashboard sends the provider id, the WebUI forwards the cancel to the peer `/v1/tasks/{id}/terminate`, and the holder terminates the task. |
| W28 | The orphan count is not a row of the health card | Closed. The count is in the status and the existing card renders it; a separate row is not wanted. |
| W29 | The doctor reports a foreign namespace but does not remove it | Closed by decision: deleting another instance's data is the operator's action; the check reports only. |

### 3.6 The tests and the documentation

| # | Problem | Where |
|---|---|---|
| W30 | The envid registry, the MCP endpoint and the three context workers have no direct coverage | Closed: `test_context_components.py` covers the registry round trip, MCP health/initialize, and all three context workers. |
| W31 | The seven OpenCode Go release gates are open | Open: `proposal_opencode_go.md` — the focused managed-session and redaction tests, the health-probe regression, the error-code normalization, and the redacted fixtures with a live smoke |
| W32 | Two CLI probes match the `test_*.py` discovery glob | Closed: renamed to `probe_context_overflow.py` and `probe_timeout_semantics_live.py`; `TEST.md` lists both probes. |
| W33 | The README's route table lists `/api/config*` | Closed: the removed routes are gone from the table. |
| W34 | `proposal_hooks.md` says both bundled hooks ship disabled | Closed by policy: older non-update proposals keep their own status and are not edited in place. |
| W35 | The README references `prod.json5.example` | Closed: the README uses only the shipped `config.json5.example` and an explicit filename example. |
| W36 | The plan documents quote stale test counts | Closed: the plans no longer quote a fixed suite count. |
| W37 | The proposal status lines are stale | Closed by policy: each non-update proposal owns its status line. |
| W38 | `logging.logs` / `logging.log_overrides` is read but not declared | `config.descriptor.json5` — Closed: both maps are declared with bounded per-file settings. |
| W47 | The browser acceptance of the generated editor has not been done | Open: `plan_aiset_integration.md`, `TEST.md` 7.1 — the step that needs a human, through nginx. |
| W48 | The example configurations and the installation scripts | Open (P3): `TODO.md` — several `<name>.json5.example` configurations selectable at install time, and the installation scripts for a configuration set (aidir + sndbx + ollama). |
| W49 | The security analysis has no recorded resolution | Closed by policy: the historical analysis is not rewritten; later rounds record their resolutions in their own ledgers. |

---

## 4. Outcome

The Round 4 implementation and documentation pass is complete except for the
items below. The closed items and their evidence are recorded in §1 and §3.

| Item | Why it remains open | What closes it |
|---|---|---|
| W31 | The OpenCode Go release gates need focused managed-session and redaction tests plus a live deployment | A focused test round and a live `/v1/models` + Chat Completions + SSE smoke against OpenCode Go |
| W47 | The generated settings editor needs a person to accept it through nginx | A recorded browser acceptance pass in `TEST.md` 7.1 |
| W48 | P3 tooling for several selectable configuration sets and their installation scripts | The example files and the installation scripts requested in `TODO.md` |

Two rules applied to every completed item:

1. **A change to a documented behavior changes the document in the same
   commit.** The route tables, the descriptor and the apply policies are the
   contract; the code follows them.
2. **No half-implemented promise.** Either an item is done and observable, or it
   is recorded as open or deferred above.
