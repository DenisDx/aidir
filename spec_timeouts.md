# Timeout Semantics

## Goal

This document defines the timeout model for queued tasks and upstream LLM/API calls.

The system uses three distinct operational timeouts and one graceful-restart timeout:

- `REQUEST_TIMEOUT`: timeout for one outbound HTTP call to an upstream LLM or API.
- `TASK_QUEUE_TIMEOUT_SECONDS`: timeout for waiting in queue before the task starts running.
- `TASK_RUN_TIMEOUT_SECONDS`: timeout for the execution phase after the task actually starts running.
- `TASK_RESTART_WAIT_TIMEOUT_SECONDS`: graceful restart drain timeout for already running tasks.

## Required Behavior

### 1. One outbound LLM/API call

`REQUEST_TIMEOUT` limits exactly one outbound HTTP request.

Examples:

- one `POST /api/chat` from `workers/agent/openaix/app.py` to Ollama
- one `POST /api/chat` from `workers/agent/call_ollama/app.py`
- one `GET/POST` from tool workers such as web search or web fetch

If a task performs multiple LLM calls because of tool loops, each call gets its own `REQUEST_TIMEOUT` budget.

`REQUEST_TIMEOUT` does not define total task lifetime.

### 2. Queue wait before first execution

`TASK_QUEUE_TIMEOUT_SECONDS` limits only the pre-execution queue phase.

Rules:

- the clock starts when the task is created/enqueued
- the timeout applies only until the first transition to `running`
- if the task reaches `running` just before queue timeout expires, queue timeout is permanently disabled for that task
- after the first start, the task is governed by `TASK_RUN_TIMEOUT_SECONDS`, not by queue timeout

If queue timeout expires before the first execution starts, the task is failed with `QUEUE_TIMEOUT` and the client receives a timeout response.

### 3. Total execution after start

`TASK_RUN_TIMEOUT_SECONDS` limits the execution phase only.

Rules:

- the clock starts at the first transition to `running`
- the limit applies to the whole task execution, including internal tool loops and multiple outbound LLM calls
- once execution starts, queue timeout no longer matters for that task

If run timeout expires, the running scheduler coroutine is canceled and the task fails with `TIMEOUT`.

## State Model

### Queued phase

Applicable limit: `TASK_QUEUE_TIMEOUT_SECONDS`

The task may:

- remain queued because of worker/resource unavailability
- be requeued due to temporary resource shortage before its first start

As long as `started_at` is empty, queue-timeout accounting remains active.

### Running phase

Applicable limit: `TASK_RUN_TIMEOUT_SECONDS`

The first transition to `running` freezes queue-timeout logic for that task.

The task may:

- call one or more LLM requests
- execute internal tools
- wait for child tasks

All of that still belongs to the same run-time budget.

## Restart Semantics

`TASK_RESTART_WAIT_TIMEOUT_SECONDS` is not a per-task execution timeout.

It is used only during graceful restart/shutdown orchestration.

Behavior:

- the system stops accepting new external work
- already running tasks are allowed to finish for up to `TASK_RESTART_WAIT_TIMEOUT_SECONDS`
- if the drain timeout expires, active tasks are canceled and shutdown continues

This timeout does not replace queue or run timeout.

## Config Mapping

### `.env`

- `REQUEST_TIMEOUT`
- `TASK_QUEUE_TIMEOUT_SECONDS`
- `TASK_RUN_TIMEOUT_SECONDS`
- `TASK_RESTART_WAIT_TIMEOUT_SECONDS`

### `config.json5`

Relevant fields:

- `workers.items.openaix.request_timeout`
- `workers.items.web_search.request_timeout`
- `workers.items.web_fetch.request_timeout`
- `tasks.queue_timeout`
- `tasks.run_timeout`
- `tasks.restart_wait_timeout`

Also present:

- `endpoints.*.request_timeout`

Current status of `endpoints.*.request_timeout`:

- deprecated legacy config for backward compatibility only
- removed from shipped config templates
- ignored for task lifecycle decisions
- startup logs emit a warning when it is still configured in a local deployment
- task lifetime is governed by `tasks.queue_timeout` and `tasks.run_timeout`
- one upstream call is governed by worker `request_timeout`

Recommended cleanup:

- keep worker `request_timeout`
- keep `tasks.queue_timeout`
- keep `tasks.run_timeout`
- remove `endpoints.*.request_timeout` from any remaining local custom configs

## Current Implementation Rules

### Scheduler

The scheduler enforces:

- queue timeout before the first execution starts
- run timeout during execution

### Endpoints

Task-serving endpoints wait according to task phase:

- before first start: queue timeout
- after start: run timeout

If an endpoint-side phase timeout is reached, it terminates the live task through `Core.terminate_task()`.

### Workers

Workers use `request_timeout` only for one outbound upstream HTTP call.

They do not stretch one LLM call to `TASK_RUN_TIMEOUT_SECONDS`.

## Notes

- `Task.to_redis_hash()` now persists `queue_timeout` and `run_timeout` for diagnostics.
- Legacy per-request top-level payload field `timeout` is no longer used to override task lifecycle timeouts.

## TBD

### Retry and fallback semantics after first execution

Current implementation choice:

- queue timeout is disabled forever after the first transition to `running`, even if the same task is later requeued because of retry or fallback logic

Reason:

- this matches the requirement that once a task managed to start, queue timeout must no longer kill it

Open question:

- if a task executes once, fails, and is requeued for a later retry, should the later wait be governed only by remaining run budget, or should there be a separate retry-wait budget in the future?

No additional timeout type was introduced in this change.