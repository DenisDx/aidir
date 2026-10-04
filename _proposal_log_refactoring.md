# Logging Refactoring Proposal

## Status

Stage 1 is complete. The runtime has an append-only JSONL writer, rebuildable
SQLite event index, compact pre-task rejection records, task-correlated client
request/response records, byte-exact upstream LLM exchange records, compact
terminal task records with append-only client-response reconciliation, basic
whole-partition retention, lazy Task Viewer event APIs, safe file-ID download,
and summary-only Redis LLM diagnostics. File-backed bodies are available to the
writer for normal, partial, timed-out, and cancelled stream completion paths.
Stage 2 storage lifecycle, bounded writer, health, retention, data-URI
attachment, and configured secret-redaction work is complete. Stage 3 Viewer,
Live Logs isolation, legacy raw-call retirement, and focused end-to-end
validation are complete.

There is no remaining implementation work in this refactoring. Completion is
based on the focused regression suite, not merely the presence of plumbing.

## Outstanding Implementation

The focused audit suite validates all body classes, streams, retries/tool loops,
rejection paths, terminal references, Viewer authorization, index rebuild, and
Live Logs isolation. Legacy raw-call writers have been removed; compact
operational call summaries remain available.

## Goal

Preserve a complete task-correlated audit trail while keeping Task Viewer and
routine log inspection fast. The redesign must retain every client and LLM
interaction without a broad rewrite of routing, workers, Redis task storage, or
the existing human-readable operational logs.

The design has two logical layers:

1. A compact task journal with exactly one terminal record per task.
2. Append-only raw-data journals for potentially large client and LLM bodies.

## Preservation Policy

The event record contains technical metadata as sibling fields and the body in
`data`.

- If a body is valid JSON, `data` stores that JSON value. AIDIR must not add,
  remove, rename, redact, or wrap fields inside `data`. Whitespace, JSON key
  order, and serialization spelling are intentionally not preserved.
- If a body is not valid JSON, `data` stores the original body as an escaped
  string. UTF-8 text must round-trip to identical bytes. Non-UTF-8 binary uses
  a file reference under `logs/audit/files/` to retain byte-exact fidelity.

```json
{"type":"client_request","task_id":"...","data":{"model":"qw","stream":true},"data_encoding":"json"}
{"type":"llm_response","task_id":"...","data":"data: partial SSE text\n\n","data_encoding":"utf-8"}
{"type":"client_response","task_id":"...","body_storage":"file","body_file":{"file_id":"..."}}
```

This makes JSON immediately inspectable while preserving non-JSON data exactly.

## Current State

### Request and response capture

- Accepted non-stream requests and responses for Ollama/OpenAIx chat and
  embeddings, plus MCP tool calls, are written with task correlation.
- The writer semantically preserves valid JSON; UTF-8 text remains inline,
  declared binary media is file-backed with byte count and SHA-256, and
  recognized base64 data-URI media is stored as an indexed attachment without
  modifying its original JSON value. Configured envelope secret fields are
  redacted, while `data` remains unchanged.
- Rejections and client stream terminal paths are task-correlated across the
  supported endpoint variants.

### LLM capture

- Generic worker lifecycle writes task-correlated byte-exact LLM request and
  response events with stable call indices.
- Legacy raw-call journal writes are retired; structured operational call logs
  contain summaries only. Redis-persisted LLM diagnostics are summary-only and
  retain audit event references instead of raw stream bodies.

### Task Viewer and retention

- Task Viewer has lazy request/detail/raw-event APIs and no longer uses worker
  call-log search for its actions. Search now returns a summary-only projection
  without decoding request/result/config/history bodies.
- Generic trimming excludes `logs/audit/`; a Core-owned bounded writer queue
  exposes drop and I/O health, while dedicated retention uses partition
  lifecycle state, restart reconciliation, stale-spool cleanup, normal and
  emergency quota settings. `/api/status` exposes audit storage health.

## Requirements

1. Preserve four raw-data categories:
   - client request received by AIDIR;
   - client response sent by AIDIR;
   - request sent by AIDIR to an LLM provider; and
   - response received by AIDIR from an LLM provider.
2. Preserve every LLM exchange, including tool loops, retries, compatibility
   retries, and streams.
3. Write exactly one compact terminal task record per task, without raw bodies.
4. Keep raw data out of task-list responses and load it only on demand.
5. For streams, write one final raw event at normal completion, failure, or
   cancellation. Do not append one JSONL event per chunk.
6. Preserve current endpoint, scheduler, worker, Redis, and UI behavior where
   possible.
7. Keep JSONL journals as the source of truth and readable by external tools;
  any lookup index must be fully rebuildable from those journals.
8. Treat audit storage as an independent subsystem: it has separate retention,
  is not a Dashboard Live Logs source, and is accessed in the UI only through
  task-correlated audit views.
9. Reject inbound HTTP request bodies larger than the configured maximum before
  endpoint JSON parsing or task creation, and record a compact rejection.

## Proposed Files

Use daily-partitioned JSONL files below `logs/audit/`:

```text
logs/audit/tasks-YYYY-MM-DD.jsonl
logs/audit/raw_client_requests-YYYY-MM-DD.jsonl
logs/audit/raw_client_responses-YYYY-MM-DD.jsonl
logs/audit/raw_llm_requests-YYYY-MM-DD.jsonl
logs/audit/raw_llm_responses-YYYY-MM-DD.jsonl
logs/audit/rejected_requests-YYYY-MM-DD.jsonl
logs/audit/files/YYYY-MM-DD/<file-id>
logs/audit/audit-index.sqlite
```

The design uses one compact task journal plus four raw journals. Separate files
keep common UI lookups and retention work narrow. Existing `all.log`,
`http.log`, and `worker.log` remain human-readable operational logs. Existing
`*_call_log.jsonl` and `*_call_raw_log.jsonl` remain during migration, then are
deprecated. Every JSONL record remains directly inspectable with external tools
such as `rg` and `jq`; the SQLite file is an internal lookup accelerator only.

## Audit Boundary

`logs/audit/` is an independent audit store, not an operational log directory.
Dashboard Live Logs lists only supported files directly below `logs/` and must
continue to reject paths, subdirectories, and `logs/audit/` files. Task Viewer
is the sole Dashboard surface for audit data and exposes it only through the
task-specific lazy raw-data APIs.

This isolation keeps large audit records out of live-tail scans, log dropdowns,
and generic operational-log maintenance while preserving direct filesystem
access for external analysis tools.

## File-Backed Bodies and Attachments

Inline event bodies use an unambiguous representation:

```json
{"body_storage":"inline","data":{"model":"example"},"data_encoding":"json"}
{"body_storage":"inline","data":"data: text\n\n","data_encoding":"utf-8"}
{"body_storage":"inline","data":{"object":"chat.completion","choices":[{"index":0,"message":{"content":"answer"}}]},"data_encoding":"json","body_format":"sse","sse_chunk_count":2}
```

Completed error-free OpenAI chat SSE responses are validated and assembled as a
whole before one reconstructed final JSON completion is stored as decoded `data`.
The assembler joins content, reasoning, tool-call arguments, choices, finish
reasons, usage, and compatible fields from all chunks. Comments, SSE metadata,
and the optional OpenAI `[DONE]` marker are not JSON chunks. `body_format` is
always `"sse"`, and `sse_chunk_count` counts only decoded source payloads. A
stream that cannot be unambiguously assembled, or is malformed, incomplete,
failed, cancelled, or error-bearing, retains its original text or file reference
rather than a partial result.

Normalization happens only when the completed body is classified; the live spool
may remain raw text/bytes. This does not change the original stream delivered to
the client. `body_bytes` and `body_sha256` describe those original bytes, not the
decoded JSON representation.

Binary HTTP bodies are stored as files under
`logs/audit/files/YYYY-MM-DD/<file-id>`. Their JSONL event omits `data` and
uses a distinct file reference:

```json
{
  "body_storage": "file",
  "body_file": {
    "file_id": "uuid",
    "relative_path": "files/2026-10-01/uuid",
    "content_type": "image/png",
    "bytes": 12345,
    "sha256": "..."
  }
}
```

Large finalized SSE bodies retain the original stream in `body_file` under the
existing inline-byte threshold policy. Successful JSON normalization additionally
stores decoded `data`, `data_encoding: "json"`, and `sse_chunk_count` in the audit
record even for these file-backed SSE bodies. The authenticated body-file link
still exposes the exact original bytes. Thus the threshold limits raw inline
body storage, not the size of decoded SSE audit data.

Recognized media encoded in a JSON `data:*;base64,...` value uses file-backed
storage for the decoded media. The original JSON value remains inline in
`data` unchanged, and the decoded media is recorded as a separate attachment
with the same `file_id` schema. This allows Task Viewer to open an image
directly without heuristic JSON rewrites or loss of the original request
representation. Unmarked base64-looking strings are not extracted heuristically
and remain part of the JSON body.

The raw-data API returns event metadata and authenticated links of the form:

```text
GET /api/tasks/viewer/audit-files/{file_id}
```

Implemented: the endpoint resolves an opaque file ID through audit events and
serves only allowlisted image types inline; all other content is downloaded with
`nosniff`.

The endpoint resolves `file_id` through the audit index, never accepts a file
path, and is excluded from Dashboard Live Logs. It sends an allowlisted image
media type with `Content-Disposition: inline`; every other type uses
`Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`. Task
Viewer opens these links in a new tab instead of embedding the file in a task
list or detail response.

## Raw Event Schema

Every raw event is one JSON object per line.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "recorded_at": "2026-10-01T04:00:00.000Z",
  "completed_at": "2026-10-01T04:00:01.000Z",
  "task_id": "uuid-or-null",
  "request_id": "HTTP-request-id-or-null",
  "exchange_id": "task-id:llm:1-or-null",
  "exchange_number": 1,
  "type": "client_request|client_response|llm_request|llm_response",
  "protocol": "openai|ollama|mcp|http",
  "endpoint": "/v1/chat/completions",
  "worker_id": "call_llama_cpp",
  "provider_id": "llama_local",
  "model": "qwen3.8-27b-local",
  "http": {"method": "POST", "url": "http://127.0.0.1:8888/v1/chat/completions", "status_code": 200, "content_type": "application/json"},
  "data": {"model": "example"},
  "body_storage": "inline",
  "data_encoding": "json",
  "body_bytes": 0,
  "body_sha256": "...",
  "terminal_status": "completed|failed|cancelled",
  "error_code": null
}
```

Rules:

- `client_request` is recorded after its body is read and a task ID exists.
  Invalid or rejected requests use `task_id: null` and are linked by
  `request_id`.
- `client_response` is recorded after the whole response body or stream ends.
- Every outgoing LLM call has a monotonic task-scoped `exchange_number` and a
  stable `exchange_id`; retries receive separate exchange IDs.
- `llm_request` stores the outgoing JSON value unchanged when valid JSON. A
  non-JSON outgoing body is stored as exact escaped text or a file reference.
- `llm_response` and `client_response` store valid complete JSON as JSON.
  Completed error-free OpenAI chat SSE stores one assembled final completion
  with `body_format: "sse"` and `sse_chunk_count`; original delivery is
  unchanged. SSE that cannot be assembled into a valid final response, and
  invalid, incomplete, failed, cancelled, or error-bearing SSE and other
  non-JSON bodies retain exact escaped text or a file reference. Binary and
  recognized media always use `body_storage: "file"`.
- Failed and cancelled streams still emit a terminal response event containing
  all bytes received or sent before termination plus the error reason.
- Authorization headers, cookies, and secrets are metadata-only and redacted.

### Rejected requests

Existing endpoints return ordinary invalid-request and authorization responses
without consistently writing an operational log record. Record every request
that is rejected before task creation in `rejected_requests-YYYY-MM-DD.jsonl`.
The compact record includes `recorded_at`, `request_id`, protocol, endpoint,
HTTP status, error code, and reason, but never stores the rejected body or
credentials. It follows the same daily partition and retention rules as audit
journals but is not indexed for Task Viewer because it has no task ID.

## Rebuildable Audit Index

`audit-index.sqlite` is a disposable sidecar index, not an audit-data store. It
maps `event_id` and `task_id` to journal path, byte offset, record length,
event type, exchange number, timestamp, and partition lifecycle state. Task
Viewer uses this mapping to load one raw event with a file seek instead of
scanning daily JSONL files.

The writer appends and closes the JSONL record before committing its index row.
An interruption can therefore leave an unindexed journal record, but never make
the index the only copy of an event. Startup and scheduled reconciliation scan
for missing rows and can rebuild the entire index from JSONL journals. A missing
or corrupted index affects only lookup speed while rebuilding, not audit data or
external analysis.

Daily partitions are immutable after rotation. The current active partition is
never eligible for retention, so cleanup cannot race a writer.

Partition dates and daily rotation use the operating system's local timezone,
not UTC and not `logging.timezone`. A partition named `*-YYYY-MM-DD.jsonl` for
local calendar date $D$ becomes eligible for deletion at local `00:00:00` on
date $D + 2$ days. For example, every `*-2026-10-21.jsonl` file may first be
removed at `2026-10-23 00:00:00` local system time, regardless of the timestamp
of its first or final record.

## Legacy Log Simplification

The audit journals replace the large-body role of the current worker call logs:

- Stop writing `*_call_raw_log.jsonl` once the corresponding audit writer is
  enabled. Its unframed bodies duplicate audit data and cannot be reliably
  correlated to a task.
- Replace `save_llm_call(...)` output with a compact operational summary. The
  summary may include
  task ID, exchange number, provider/model, URL path, status, duration, token
  usage, and error code, but must not include request, response, or
  `stream_chunks` bodies.
- Keep full task payload only while it is needed for queued/running execution,
  client delivery, retry, or an existing task API contract. After a terminal
  audit record exists, terminal Redis diagnostics must retain only bounded
  summaries and audit event references. Removing terminal payload/result fields
  requires a separate endpoint-compatibility check and is not a Phase 1 change.

There is no legacy compatibility period: the same release that enables audit
logging removes legacy raw-call writes and the old call-log viewer path. Existing
legacy files may be removed through normal operational-log maintenance. No new
full request or response body is written outside `logs/audit/` after cutover.

## Terminal Task Record

`tasks-YYYY-MM-DD.jsonl` has exactly one record per terminal task:

```json
{
  "schema_version": 1,
  "task_id": "uuid",
  "recorded_at": "2026-10-01T04:00:00.000Z",
  "status": "completed",
  "task": {"type": "agent", "priority": 5, "worker_id": "call_llama_cpp", "created_at": "...", "started_at": "...", "finished_at": "...", "queue_timeout": 2900, "run_timeout": 1900, "route": {"resolved_provider": "llama_local", "resolved_model": "..."}, "error": null, "result_summary": {"usage": {"completion_tokens": 42}}},
  "raw_event_refs": {"client_request": ["event-id"], "client_response": ["event-id"], "llm_requests": ["event-id"], "llm_responses": ["event-id"]}
}
```

The terminal record excludes raw request/response bodies, `raw_sse`,
`stream_events`, and unbounded LLM history. It may retain compact LLM summaries
such as exchange number, status, duration, model, URL path, and usage.

## Proposed Implementation

### 1. Add one focused audit writer

Add `core/audit_log.py` with a narrow API:

- `record_client_request(...)`
- `record_client_response(...)`
- `begin_llm_exchange(...)`
- `record_llm_request(...)`
- `record_llm_response(...)`
- `record_task_terminal(...)`

The writer owns event IDs, UTC timestamps, daily paths, JSON/non-JSON body
conversion, byte count, SHA-256, secret redaction, and sidecar-index updates.
A writer lock per target file prevents interleaved records. A bounded
asynchronous queue and dedicated writer task keep serialization and file I/O
off the event loop. The configured overflow policy is explicit: `best_effort`
records a visible audit-loss diagnostic without delaying inference; `durable`
applies backpressure when audit completeness is more important than latency.
The default policy is `best_effort`. The audit health state becomes warning on
the first dropped event and error after ten drops within five minutes or any
writer I/O failure, including `ENOSPC`.

### 2. Capture endpoint bodies before parsing

Replace direct task-route `request.json()` usage with a shared helper:

1. read `raw_body = await request.body()` once;
2. parse the body as JSON when valid;
3. create a task from the parsed view;
4. record the client request using the created task ID; and
5. record invalid or rejected requests with `request_id` when no task exists.

The helper must preserve current validation, authentication, and task behavior.

### 2a. Enforce a request-body limit

Add one shared ASGI request-body limiter to task-producing HTTP applications.
It checks declared `Content-Length` before consuming the body and counts bytes
received from chunked requests, returning HTTP 413 before endpoint parsing or
task creation when the body exceeds `http.max_request_size`. The default is
`104857600` bytes (100 MiB) when the setting is absent from an existing config.
Every rejection writes the compact `rejected_requests` record and an
operational warning without retaining the oversized body.

### 3. Capture client responses

Use shared response wrappers:

- For JSON responses, serialize once, return the matching response, and record
  the JSON value without changing its fields.
- For `StreamingResponse`, tee bytes to a temporary spool while passing the
  same bytes to the client. At terminal completion, failure, disconnect, or
  cancellation, write one client-response event using the preservation policy.

Phase 1 covers task-producing OpenAI/Ollama chat and embedding routes. Direct
MCP request/response auditing is deferred, but an MCP request that creates an
inference task must enter the same task audit flow. Full direct-MCP audit
coverage is a future extension.

### 4. Capture LLM exchanges

Replace ad hoc `save_llm_call` and `save_llm_raw_call` usage in `openaix`,
`call_ollama`, and `call_llama_cpp` with the audit writer:

- Allocate `exchange_id` before each upstream call.
- Record outgoing valid JSON as the value supplied to the HTTP client without
  field modifications.
- Read sync responses once, record them, then parse them.
- Consume stream raw bytes while a small parser buffer handles SSE semantics.
- In `finally`, write one LLM-response event for normal, failed, timed-out, and
  cancelled exchanges.

The existing live task `llm_call_history` remains useful but must stop retaining
unbounded `raw_sse` and `stream_events`. Keep compact summaries and audit event
references instead.

### 5. Write the terminal task record

Hook idempotent task-journal writing into:

- `QueueManager.mark_completed`;
- `QueueManager.mark_failed`; and
- `QueueManager.mark_canceled`.

Audit-write failure is logged and observable but never changes the task state or
client response.

### 6. Add lazy WebUI APIs

Add authenticated endpoints:

```text
GET /api/tasks/viewer/{task_id}/request
GET /api/tasks/viewer/{task_id}/detail
GET /api/tasks/viewer/{task_id}/raw?type=client_request|client_response|llm_request|llm_response&exchange_id=...
```

- `request` returns the client request event or a precise unavailable state.
- `detail` combines live/Redis task data, terminal task record, and a small raw
  event manifest. It does not inline large raw bodies.
- `raw` returns one selected raw event. The UI displays JSON directly for
  `data_encoding: "json"`; otherwise it displays escaped text or offers a safe
  byte download.

Replace Task Viewer actions:

1. `Show JSON` becomes `Show Request` and loads only the client request.
2. `Show Steps` becomes `Show Task` and loads compact metadata plus raw event
   references. Each large body is an explicit lazy-load action.

For active tasks, `detail` reports that execution is in progress and returns
only completed raw events. Partial stream display is optional in phase 1.

### 7. Make task search summary-only

Change `/api/tasks/viewer/search` to return only task ID, status, type, worker,
envid, priority, lifecycle timestamps, route/model summary, error code, LLM
call count, and a short safe request preview. It must not parse or return full
payloads, results, config, LLM histories, or raw data.

## Streaming and Large Bodies

Raw data is written only when a logical body is terminal, as required. To avoid
unbounded Python memory, stream bytes are held in a temporary per-event spool
under `logs/audit/.spool/`. At terminal completion, failure, or cancellation:

1. close the spool;
2. parse as JSON when valid, otherwise store escaped UTF-8 or a file reference;
3. calculate byte count and SHA-256;
4. append one JSONL event; and
5. delete the spool only after a successful append.

Binary and recognized base64 media are finalized as file-backed bodies rather
than inline JSONL data. The spool is atomically renamed into its date-partition
file location when it is already the final binary body; otherwise it is removed
after the inline event is written. File-backed JSON bodies and media attachments
are written before the referencing JSONL event becomes visible.

## Retention and Performance

Generic cron trimming must exclude `logs/audit/`; rewriting a file tail can
remove a large audit record. Add a dedicated audit retention job that works only
on whole, closed daily partitions:

- rotates strictly by day; it never splits, truncates, or rewrites a journal;
- evaluates eligibility only from the local calendar date in the partition
  filename: a partition for date $D$ cannot be removed before local midnight at
  the start of $D + 2$;
- removes an eligible partition when it exceeds `retention_days`, or removes
  oldest eligible partitions until the whole `logs/audit/` directory is at or
  below `max_total_bytes`;
- marks a selected partition as `deleting` in the sidecar index, hiding it from
  UI lookup before file removal;
- deletes every JSONL file and `files/YYYY-MM-DD/` attachment directory in the
  selected partition, then removes its index rows and marks the partition
  `deleted`;
- deletes stale spool files after a recovery interval;
- preserves active-file append safety; and
- exposes audit disk usage and retention health.

Retention reconciliation resumes interrupted `deleting` operations after a
restart. If a process stops before file removal, the complete text journal stays
available to external tools while hidden from the UI. If it stops after file
removal, the index was already hidden and reconciliation removes its stale rows.
The retention job never truncates, rewrites, or partially removes an audit JSONL
file.

`max_total_bytes` applies to the complete audit directory, including journal,
index, and spool files. It is an enforced cleanup threshold, not an impossible
hard ceiling: if the active or date-protected partitions alone exceed it, the
job must retain them, emit a quota-health error, and let the configured writer
overflow policy govern subsequent audit events. This preserves the calendar
retention guarantee instead of deleting recent evidence.

Emergency retention overrides the normal age and date protection only when
`emergency_max_total_bytes` is exceeded or a write fails with `ENOSPC`. It
deletes the oldest closed partitions, including their attachment directories,
until the audit directory is below the emergency limit or no closed partition
remains. The active local-calendar-day partition is never deleted. Every
emergency deletion is an error-level operational log and audit health event.

Suggested configuration:

```json5
logging: {
  audit: {
    enabled: true,
    directory: "logs/audit",
    retention_days: 30,
    max_total_bytes: 1073741824,
    emergency_max_total_bytes: 2147483648,
    max_spool_age_seconds: 86400,
    writer_queue_size: 256,
    overflow_policy: "best_effort"
  }
},
http: {
  max_request_size: 104857600
}
```

Every field above has the displayed value as its default, so existing
configuration files need no update to enable the audit subsystem.

## Migration Plan

### Phase 1: foundations

- Add audit writer, schema, daily partitions, file-backed bodies, rejected
  request journal, rebuildable SQLite sidecar index, and focused tests.
- Record client requests and terminal task records for OpenAI/Ollama task
  routes.
- Leave all current logs intact.

### Phase 2: LLM exchanges

- Record task-correlated LLM requests and responses for `openaix`,
  `call_ollama`, and `call_llama_cpp`.
- Capture partial terminal stream bodies on timeout and cancellation.
- Bound Redis call diagnostics to summaries and audit references.
- Stop legacy raw call writes and replace legacy structured call bodies with
  operational summaries in the same release.

### Phase 3: client responses and UI

- Record synchronous and streaming client responses.
- Add lazy raw-data APIs and replace Task Viewer buttons.
- Make task search summary-only.

### Phase 4: retirement and retention

- Verify coverage against live traffic and failure paths.
- Remove legacy `*_call_raw_log.jsonl` writers and the old call-log viewer
  route; no compatibility overlap is required.
- Enable daily-only audit retention using `retention_days` and
  `max_total_bytes`, and exclude audit files from generic cron trim/wipe.

## Validation

Add focused tests for:

1. valid JSON client request keeps an identical JSON value in `data`, without
   added or removed fields;
2. invalid JSON request stays byte-exact as escaped text and has `task_id:
  null`; binary input is file-backed;
3. sync client and LLM bodies preserve identical JSON values when valid and
   bytes exactly otherwise;
4. normal SSE writes one terminal client-response and LLM-response event;
5. timeout/cancellation writes partial terminal raw events with correct reason;
6. multi-turn tool loops write distinct ordered exchange IDs;
7. a terminal task record is exactly once, compact, and reference-only;
8. task search does not parse or return raw bodies/full histories;
9. lazy raw API rejects unsafe type/path selection; and
10. an index rebuild restores lookup for records appended before an interrupted
  index commit;
11. audit-writer queue overflow follows the configured `best_effort` or
  `durable` policy without blocking the event loop unexpectedly; and
12. retention deletes only whole closed audit partitions and reconciliation
  completes an interrupted deletion without exposing stale UI pointers.
13. quota retention never deletes a partition for date $D$ before local
  midnight at the start of $D + 2$, even when `max_total_bytes` is exceeded;
  and
14. Dashboard Live Logs neither lists nor serves `logs/audit/` files.
15. binary bodies and recognized base64 media are stored as date-partitioned
  files; their audit events use `body_storage: "file"` and no `data` field.
16. an audit-file endpoint authorizes `file_id`, sends an image inline only for
  allowlisted media types, and downloads every other type safely.
17. rejected pre-task requests write a compact request-ID-associated rejection
  record without storing their body or credentials.
18. emergency quota or `ENOSPC` deletes only closed whole partitions and emits
  an error-level health event.
19. declared and chunked requests above `http.max_request_size` receive HTTP
  413, create no task, and write a compact rejection record without retaining
  the oversized body.

## Future Scope

No unresolved design decisions block the first implementation. This phase
assumes audit storage is trusted and retention-limited; a future security phase
may add encryption, access roles, and stronger secret redaction. Direct MCP
request/response auditing remains deferred, while inference tasks created via
MCP follow the standard task audit path without a separate integration.

## Non-Goals

- Replacing Redis task lifecycle storage.
- Changing task routing, resource scheduling, client protocols, or worker
  semantics.
- Returning raw bodies in the task-list API.
- Reformatting human-readable operational logs into audit records.

## Minimal Completion Plan

### Stage 1: Complete Audit Capture Contracts

Finish all endpoint rejection paths, client stream terminal capture, and
byte-exact upstream capture for `call_ollama`, `call_llama_cpp`, and `openaix`.
Each existing request, response, retry, tool loop, timeout, cancellation, and
queue-admission path must produce the required compact or raw event without
changing routing, retry, or delivery behavior. Add terminal-record
reconciliation after client response finalization so the one logical terminal
task view contains every raw-event reference.

### Stage 2: Make Audit Storage Durable and Maintainable (Complete)

Implemented the bounded writer queue and health state, partition lifecycle
state, startup/cron reconciliation, stale-spool cleanup, normal and emergency
quota retention, disk-usage reporting, metadata redaction, and data-URI media
attachments while preserving original JSON `data` unchanged.

### Stage 3: Complete Viewer and Validate End to End (Complete)

Implemented file-backed-event links and Task Viewer safety coverage. The
complete focused audit suite validates byte equality for body classes and
streams, retries/tool loops, rejection paths, terminal references, writer
overflow/failure, retention recovery/quota, Viewer authorization, index rebuild,
and Live Logs isolation.