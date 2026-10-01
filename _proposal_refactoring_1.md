# Refactoring Plan 1

Date: 2026-10-01
Status: Execution 1 complete; deferred security work remains out of scope.

## 1. Scope and Baseline

This plan covers only confirmed defects, incomplete requirements from
`_proposal_log_refactoring.md`, serious inefficiencies, and small useful
cleanups. Cosmetic refactoring and speculative redesign are excluded.

The logging implementation is currently uncommitted relative to `HEAD`
(`3508b7d`, which contains only the original logging proposal). The focused
baseline passes 154 tests:

```text
test_audit_log.py
test_endpoint_audit.py
test_request_limits.py
test_cron_logging.py
test_llama_cpp.py
test_openaix_embeddings.py
test_openaix_tool_args_parser.py
test_task_llm_call_count.py
test_webui_live_logs.py
```

Those tests prove the implemented happy paths, but they do not cover the races,
memory bounds, restart recovery, or security boundaries listed below.

## 2. Required Logging Refactoring Follow-up

### L1. Make audit event acceptance and terminal references consistent

**Problem.** In `best_effort` mode, `AuditLog.record_*()` returns an event ID as
soon as the event is queued. `QueueManager._record_terminal_audit()` immediately
queries SQLite, so a terminal record can permanently omit accepted client or LLM
events that the writer has not indexed yet. A queue overflow can also return an
ID for an event that was actually dropped. Late reconciliation currently covers
only client responses.

**Change.** Introduce an explicit writer receipt/barrier contract. Before the
terminal snapshot is queued, wait only for all earlier accepted events for that
task to be either persisted or reported as dropped. Build references from the
confirmed receipts, not from a timing-dependent SQLite query. Preserve
`best_effort` inference behavior: waiting belongs to terminal audit finalization
and must not turn an audit failure into a task failure.

**Acceptance.** A test must block the writer, finish a task, release the writer,
and verify that every persisted event is referenced and every dropped event is
reflected in audit health rather than referenced.

### L2. Guarantee exactly one terminal task record atomically

**Problem.** `record_task_terminal()` performs a non-atomic index lookup before
enqueueing. Two rapid terminal calls can both observe no record and append two
`task` records. SQLite uniqueness by `event_id` does not enforce one terminal
record per `task_id`, and duplicate JSONL records would remain even if indexing
rejected one later.

**Change.** Serialize the terminal existence check and append inside the writer
operation. Use a task-scoped terminal reservation in the writer and re-check the
index immediately before the JSONL append. Rebuild/startup must restore the
reservation from existing journals.

**Acceptance.** Concurrent `completed`/`failed`/`canceled` finalization attempts
for one task produce exactly one JSONL `task` record and one indexed terminal
event.

### L3. Bound upstream stream memory and remove event-loop file I/O

**Problem.** `call_ollama`, `call_llama_cpp`, and `openaix` retain every raw
transport chunk in `list[bytes]` and then allocate another complete copy with
`b"".join(...)`. The llama.cpp path additionally retains all decoded lines and
parsed events. Multi-hour streams can therefore consume several copies of the
response in memory. Body decoding, hashing, attachment extraction, file writes,
and final spool hashing are also synchronous on the asyncio event loop. Client
stream code opens and closes the spool for every chunk.

**Change.** Give each upstream exchange one incremental spool with a bounded
parser buffer, byte count, and SHA-256 state. Keep the spool handle open for the
stream lifetime, close it in `finally`, and finalize it for success, timeout,
cancellation, and failure. Run body conversion and file/index I/O in the writer
thread. Keep only compact diagnostic summaries in worker memory.

**Acceptance.** Stream a body substantially larger than the parser buffer and
verify byte equality, one response event, bounded process-memory growth, event
loop responsiveness, and spool cleanup for normal and canceled execution in all
three workers.

### L4. Apply the declared preservation policy to completed stream spools

**Problem.** `finalize_body_spool()` always creates a file-backed event. The
proposal instead requires terminal stream bodies to be classified using the same
valid-JSON, UTF-8 text, or binary/file policy as non-stream bodies. Current SSE
and NDJSON streams therefore do not follow the documented schema decision.

**Change.** Finalize a spool through the common body classifier. Inline bounded
UTF-8/JSON bodies according to the preservation contract and keep binary or
large bodies file-backed under an explicit, documented size threshold. Add the
threshold to audit configuration if large UTF-8 streams must remain file-backed.

**Acceptance.** Tests cover JSON, UTF-8 SSE/NDJSON, non-UTF-8 data, and the size
threshold without changing the bytes delivered to the client.

### L5. Make retention health shared and stop bounded emergency cleanup

**Problem.** Cron constructs a separate `AuditLog`, so its disk usage,
`retention_status`, and `last_retention_at` never reach the Core-owned instance
reported by `/api/status`. On `ENOSPC`, `force_emergency=True` makes every closed
partition eligible for the whole loop and can delete all historical closed
partitions even after enough space has been reclaimed.

**Change.** Persist the small health/retention snapshot in the SQLite sidecar (or
another process-safe store) and have `/api/status` read it. During forced
emergency cleanup, remove the oldest closed partition, recompute usage/free
space, and stop as soon as the emergency target is satisfied or no closed
partition remains.

**Acceptance.** A cron retention test observes updated API health from another
`AuditLog` instance. An injected `ENOSPC` removes the minimum eligible set and
never the active partition.

### L6. Complete the Task Viewer summary contract

**Problem.** Search is body-free, but it always returns an empty
`request_preview` and omits the promised route/model summary. The existing
`test_viewer_search.py` is a smoke script rather than an asserting contract
test.

**Change.** Persist bounded, safe summary fields separately when a task is
created, return only those fields from search, and add asserting API tests. Do
not decode payload, result, config, or LLM history while building search rows.

**Acceptance.** Search returns route, model, and a bounded preview without
reading heavy Redis fields or exposing credentials/raw bodies.

### L7. Finish legacy cleanup and operator documentation

**Problem.** Legacy raw-call output is retired, but
`config.json5.example`, `test_cron_logging.py`, and
`test_ollama_interference.py` still reference `*_call_raw_log.jsonl`. README has
no concise audit configuration, health, retention, or Task Viewer description.

**Change.** Remove obsolete raw-log overrides and test fixtures. Update the
interference utility to use task-correlated audit events or remove it if it no
longer has a valid use. Document the audit settings and operational checks in
`README.md`.

**Acceptance.** A repository search finds no active legacy raw-call path, the
focused suite still passes, and documented commands match current endpoints and
files.

## 3. Required Project-wide Fixes

### P1. Never persist or return incoming bearer tokens

**Problem.** Ollama/OpenAIx store `incoming_bearer_token` in `task.config`.
`Task.to_redis_hash()` serializes the complete config, and task APIs decode and
return it. Caller credentials can therefore remain in Redis for the external
task retention period and be exposed through Task Viewer.

**Change.** Move the token to a non-persisted runtime-only task field used by
smart-route probes and forwarding. Add defense-in-depth redaction to task
serialization and API projections for all credential-like config keys.

**Acceptance.** Authenticated routing still works, while the token is absent
from Redis, task APIs, operational logs, and audit metadata.

### P2. Recover Redis task queues safely after process restart

**Problem.** `QueueManager` starts with an empty in-memory registry. Scheduler
uses destructive `ZPOPMIN`; when it encounters a persisted queued ID that is not
in memory, it logs a warning and discards it. Thus a service restart can silently
lose queued work and leave stale `running` records. The Dashboard also cannot
show those tasks from the live registry.

**Change.** Add subtype-aware task deserialization and hydrate queued tasks
before Scheduler starts. Define and implement one explicit startup policy for
stale running tasks (normally mark failed/canceled with a restart reason, unless
safe replay is supported). Do not pop an unknown persisted task until it has
been recovered or quarantined.

**Acceptance.** A restart test seeds queued and running Redis records, starts a
fresh Core, executes each recoverable queued task exactly once, and applies the
documented stale-running policy.

### P3. Enforce WebUI permissions and redact configuration

**Problem.** Sessions store `permissions`, but every route checks only whether a
session exists. Any authenticated account can edit configuration, restart the
service, terminate tasks, repair cron, or release resources. `/api/config`
returns resolved configuration values, including secrets. This remains an
unresolved critical finding from `_proposal_update_1.md`.

**Change.** Add centralized read/control/config permission dependencies and use
them on every WebUI route. Return a recursively redacted structured config;
restrict raw config access and dangerous writes to an explicit highest
privilege.

**Acceptance.** Read-only and control-only test users receive the expected 403
responses, `all` retains current behavior, and no configured secret appears in
any config response.

### P4. Propagate parent cancellation to internal tool tasks

**Problem.** `_execute_internal_tool()` cleans up its child only on the child's
own timeout or normal completion. If the parent agent is canceled or reaches its
deadline first, the child tool can continue running and perform side effects
after the parent has already failed.

**Change.** Handle parent `CancelledError` and all abnormal exits in `finally`:
terminate the child through Core, await cancellation completion, then delete its
state according to the normal internal-task policy.

**Acceptance.** Canceling a parent with a blocked tool stops the child coroutine,
releases resources, and prevents a later completed result.

### P5. Fix `call_ollama` cancellation immediately

**Problem.** `workers/agent/call_ollama/app.py` catches
`asyncio.CancelledError` without importing `asyncio`. Real cancellation raises
`NameError`, skips the intended canceled audit finalization, and can be reported
as a generic worker failure.

**Change.** Import `asyncio` and add focused sync/stream cancellation tests.

**Acceptance.** Cancellation records `terminal_status="cancelled"` and
re-raises `CancelledError` unchanged.

### P6. Reject valid non-object JSON consistently

**Problem.** Shared body parsing accepts any JSON value, while handlers
immediately call `.get()`. Requests containing `null`, arrays, strings, numbers,
or booleans therefore reach the global exception handler and return HTTP 500.

**Change.** Add an object-only option to `BaseEndpoint._read_json_body()` (the
default for current task-producing routes), and use one shared protocol-aware
400 rejection path. This also removes repeated parse/type validation.

**Acceptance.** Every public task endpoint returns its protocol-correct 400,
creates no task, and writes one compact rejection for every non-object JSON
type.

### P7. Fix password-enabled Redis and configurable WebUI deployment

**Problem.** Docker enables Redis `requirepass` when configured, but its health
check and `install.sh` ping omit authentication. A secured Redis is therefore
reported unhealthy. Docker passes `WEBUI_PORT` to nginx, but `nginx.conf`
hardcodes port `20082`, so any documented non-default backend port breaks the
proxy.

**Change.** Authenticate health/install probes without printing the password.
Render the nginx upstream from `WEBUI_HOST`/`WEBUI_PORT` at container startup
and validate the generated config with `nginx -t`.

**Acceptance.** Docker is healthy with a non-empty Redis password, and both HTTP
and WebSocket proxying work with a non-default WebUI backend port.

### P8. Bound Task Viewer Redis work

**Problem.** Search scans every retained task, performs one `HGETALL` round trip
per key, stores all matches, sorts them, and only then applies `limit`. Retention
is long enough for this to become a recurring Redis and event-loop load spike.

**Change.** Pipeline each SCAN batch and retain only the best `limit` rows in a
bounded top-k structure. Preserve current filtering and ordering semantics.

**Acceptance.** A large seeded dataset produces identical results with bounded
memory and a bounded number of Redis round trips per SCAN batch.

### P9. Remove inert context-task legacy only after a reference test

**Problem.** `Task_context_builder`, the `context_builder` queue name, endpoint
`context_builder_worker`, and middleware configuration have no runtime producer
or consumer. Context workers are invoked synchronously by the OpenAIx context
chain, while Scheduler polls a different `context` queue name. This is misleading
dead surface and cannot work as a queued path in its current form.

**Change.** First add a repository/config reference test proving the fields are
unused, then remove the task subtype, stale queue names, and inert config keys.
Keep the existing synchronous context chain unchanged.

**Acceptance.** Context/tool-injection regressions pass and shipped configs no
longer advertise an unreachable execution mode.

## 4. Previously Documented Security Work Still Open

`_proposal_update_1.md` remains relevant. In particular, endpoint authentication
is still optional, `web_fetch` still needs redirect-safe private-address/metadata
SSRF protection, and secure credential/transport defaults remain unfinished.
These should be scheduled with P1 and P3 rather than rediscovered as a separate
refactoring project. Their detailed requirements and tests should remain in the
original security proposal to avoid two divergent specifications.

## 5. Optional Heavy Improvement

### Unified streaming audit sink

After L3 is complete, client and LLM streaming could share one asynchronous
sink that owns spooling, incremental hashes, size policy, finalization,
cancellation, and cleanup. This would remove five protocol-specific lifecycle
implementations and make future transports safer. It is desirable, but not
required for correctness if L3 is implemented with small local adapters around
one common spool primitive.

## 6. Implementation Order

1. P5 and P6: trivial correctness fixes with focused regressions.
2. P1 and P3: remove credential exposure and enforce existing permission data.
3. L1 and L2: make terminal audit records complete and exactly-once.
4. L3 and L4: bound stream memory and align storage with the preservation policy.
5. L5 and L6: correct operational health and finish the Viewer contract.
6. P4, P7, and P8: lifecycle, deployment, and scale fixes.
7. P2: restart recovery, validated before deployment because it changes queue startup semantics.
8. L7 and P9: remove proven legacy artifacts and update operator documentation.

Each implementation step should add its discriminating regression first or in
the same change and rerun the focused 154-test baseline. Queue recovery and
permission changes additionally require integration tests because their failure
modes cross process/API boundaries.



# Execution 1

## Scope

This execution covers only non-security work that is either correctness-critical
or has a small, well-defined implementation. It intentionally excludes P1 and
P3, all security findings from `_proposal_update_1.md` except F12, and the
optional unified streaming audit sink. Security, secret handling, access
control, network exposure, and transport hardening will be handled in a
separate update.

Each phase must add or strengthen its focused regression before proceeding to
the next phase. Run the affected focused tests after each phase and the complete
154-test baseline after the last phase.

## Phase 1: Immediate Correctness and Input Contracts

- [x] **1.1 Fix `call_ollama` cancellation handling**

Implement P5 by importing `asyncio` in `workers/agent/call_ollama/app.py` and
covering cancellation in both synchronous and streaming execution paths. The
worker must finalize the current audit exchange as `cancelled`, retain any bytes
already received, and re-raise `CancelledError` rather than convert it into a
generic worker error.

**Validation:** cancellation regression tests for sync and stream execution;
existing Ollama/audit suites remain green.

- [x] **1.2 Reject non-object JSON before endpoint logic**

Implement P6 through one shared `BaseEndpoint` helper that distinguishes invalid
JSON from a valid JSON value that is not an object. Apply it to all
task-producing Ollama, OpenAIx, and MCP handlers. Preserve each protocol's
existing error envelope, create no task, and write one compact rejection record.

**Validation:** parameterized tests for `null`, arrays, strings, numbers, and
booleans on every endpoint family, asserting HTTP 400 rather than HTTP 500.

- [x] **1.3 Stop a child tool when its parent agent is canceled**

Implement P4 in `OpenAIxWorker._execute_internal_tool()`. Track the enqueued
child until cleanup completes. On parent cancellation or any abnormal exit,
terminate the child through Core, wait for the terminal state where necessary,
and then apply the normal internal-task deletion policy. Preserve the existing
child timeout behavior.

**Validation:** a blocking tool test cancels the parent and proves the child
does not run or complete afterwards; resource reservations are released.

## Phase 2: Audit Journal Correctness

- [x] **2.1 Make terminal-event references deterministic**

Implement L1. Give queued audit writes a receipt state: persisted, dropped, or
failed. Before recording a terminal task snapshot, collect only confirmed
receipts for that task instead of querying a potentially lagging SQLite index.
If a client-response reference is necessarily produced after task completion,
retain the current append-only reconciliation mechanism and generalize it only
when another event class can legitimately arrive late.

**Validation:** block the best-effort writer, finish a task with client and LLM
events, then release it. Verify that every persisted event is reachable from the
terminal snapshot and that a queue-dropped event is not referenced.

- [x] **2.2 Enforce one terminal record per task**

Implement L2 inside the writer-owned critical section. Reserve a task ID before
enqueueing its terminal write and re-check journal/index state immediately
before append. Reconstruct reservations while rebuilding the index or scanning
existing journals at startup.

**Validation:** concurrently invoke completed, failed, and canceled finalizers
for one task; assert one `task` JSONL record and one terminal index entry.

- [x] **2.3 Repair retention reporting and emergency stopping condition**

Implement L5 without changing retention eligibility rules. Store the small
retention health snapshot in the audit sidecar so a cron-created `AuditLog` and
the Core status endpoint observe the same state. During ENOSPC recovery, delete
the oldest closed partition one at a time and stop once the target is met.

**Validation:** cross-instance health test plus injected-ENOSPC tests proving
minimum deletion and active-partition preservation.

## Phase 3: Bounded Streaming Storage

- [x] **3.1 Introduce a shared incremental audit spool primitive**

Implement the minimum common primitive required by L3: one open spool per
stream, incremental byte count and SHA-256, a bounded decode/parser buffer, and
idempotent close/finalize cleanup. Do not implement the optional full unified
streaming sink in this execution.

Adapt `call_ollama`, `call_llama_cpp`, and `openaix` to write raw upstream bytes
to this spool instead of retaining `raw_parts`; remove unbounded llama.cpp
`raw_lines` and `stream_events` diagnostics in favor of existing compact
summaries. Adapt client stream writers to keep the spool handle open rather
than opening it once per chunk. Finalization must run for normal completion,
HTTP error, timeout, cancellation, and unexpected exception.

**Validation:** large normal and canceled stream cases for each worker verify
byte equality, one terminal LLM event, bounded in-memory buffering, and no stale
spool. Add a client stream test covering one open/close lifecycle.

- [x] **3.2 Apply the normal body policy to finished spools**

Implement L4 using the shared finalizer. A completed spool must be classified
as valid JSON, UTF-8 text, or file-backed binary/large data by the same policy
as a non-stream body. Define one explicit maximum-inline-body setting if a
size threshold is needed; otherwise retain the existing file behavior only for
binary/media data.

**Validation:** JSON, SSE/NDJSON text, binary, cancellation, and threshold
tests verify schema fields and preserve client-delivered bytes.

## Phase 4: Viewer, Operational Scale, and Deployment Correctness

- [x] **4.1 Complete the summary-only Task Viewer contract**

Implement L6. At task creation, persist a bounded safe request preview plus
route/provider/model summary as dedicated summary fields. Search must use those
fields without decoding payload, result, config, or LLM history. Replace the
current smoke-only Viewer search check with asserting API coverage.

**Validation:** search returns the expected summary fields, excludes raw bodies,
and does not read the heavy Redis fields in a test double.

- [x] **4.2 Bound Task Viewer search work**

Implement P8 after 4.1 so the new summary fields are used consistently. Pipeline
Redis reads per SCAN page and keep only the best requested number of matches in
a bounded top-k collection. Preserve filters and current ordering.

**Validation:** a large seeded task dataset returns identical ordered results
with bounded intermediate memory and no one-request-per-key Redis pattern.

- [x] **4.3 Repair non-security deployment configuration**

Implement only the nginx half of P7: render nginx's upstream host and port from
`WEBUI_HOST` and `WEBUI_PORT` during container startup, then run `nginx -t` on
the rendered configuration. Redis password health-check behavior is explicitly
out of scope because it belongs to the later security update.

**Validation:** container/proxy test with a non-default WebUI port verifies both
an `/api/` request and `/ws/` upgrade through nginx.

- [x] **4.4 Reduce stale external-task accumulation**

Implement F12 from `_proposal_update_1.md` as an operational retention change.
Choose a lower default lifetime based on the documented task timeout envelope,
allow an explicit configuration override for deployments that need longer
history, and ensure cleanup deletes only terminal external tasks.

**Validation:** terminal external tasks expire at the configured interval;
queued and running tasks remain untouched.

## Phase 5: Queue Restart Recovery

- [x] **5.1 Hydrate persisted queued tasks before scheduling**

Implement the queued-task part of P2. Add subtype-aware deserialization from a
Redis task hash, recover valid queued tasks during Core startup before Scheduler
can call `ZPOPMIN`, and quarantine malformed/missing records without silently
discarding unrelated queue entries.

**Validation:** seed Redis with each supported queued task type, start a new
Core, and prove each valid task executes exactly once.

- [x] **5.2 Define stale running-task recovery**

Implement the remaining P2 policy explicitly. Unless a worker provides an
idempotent resumable contract, mark startup-discovered running tasks terminal
with a stable `SERVICE_RESTARTED` error and release their queue/resource state.
Do not replay potentially side-effecting work automatically.

**Validation:** restart simulation verifies the configured terminal state,
Dashboard/Viewer visibility, and absence of orphaned queue entries or resource
reservations.

## Phase 6: Retire Proven Legacy Surfaces and Document the Result

- [x] **6.1 Remove obsolete raw-call references**

Implement the code/config portion of L7. Remove obsolete raw-call log overrides
and fixtures from the example configuration and log-maintenance tests. Convert
`test_ollama_interference.py` to task-correlated audit input only if it still
has an operational use; otherwise remove the obsolete utility.

**Validation:** repository search finds no runtime `*_call_raw_log.jsonl`
writer or configuration reference; focused log/audit tests pass.

- [x] **6.2 Remove the inert queued context-builder surface**

Implement P9 after a reference/config regression establishes that the
synchronous OpenAIx context chain is the only supported path. Remove
`Task_context_builder`, stale `context_builder` queue declarations, and inert
configuration keys, while leaving actual context and tool-injection behavior
unchanged.

**Validation:** configuration loading and context/tool-injection regressions
pass, and no shipped configuration advertises the removed queue mode.

- [x] **6.3 Update operator documentation**

Finish L7 by documenting audit storage configuration, retention health, Task
Viewer raw-event access, stream body behavior, the updated external-task
retention setting, and the nginx template behavior. Update README examples only
after their corresponding runtime phase is complete.

**Validation:** documented paths, API names, and configuration keys are checked
against the resulting source and configuration template.

## Final Validation

- [x] Run the focused audit, endpoint, worker, queue/scheduler, cron, WebUI,
  and deployment tests added by these phases.
- [x] Run the existing 154-test baseline and JavaScript syntax checks.
- [x] Run `git diff --check`.
- [x] Perform one restart-recovery integration run with Redis fixtures and one
  nginx proxy run using a non-default backend port.
- [x] Do not include security assertions in this execution; they belong to the
  later dedicated security update.