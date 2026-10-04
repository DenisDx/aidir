# Proposal: Hooks

## Summary

Hooks are trusted Python extensions loaded by the core at startup. They are a
simple plug-and-play tool for quickly trying a lifecycle behavior without
modifying endpoint, scheduler, or worker code. Hooks are opt-in and disabled
by default, so an experiment cannot affect production traffic until an
administrator explicitly enables it.

The extension directory is `./hooks`. A hook is either:

- one Python file: `hooks/<hook_id>.py`;
- one directory with its entry module at `hooks/<hook_id>/app.py`.

`hooks` remains the public name. It describes the event-driven scope more
precisely than `plugins`, which could imply arbitrary application extensions.

## Loading

The core scans both forms at startup in lexicographic hook-ID order, reads each
hook's local `ENABLED` setting, and imports enabled entry modules as isolated
modules. It then provides each one with a registration object. The module may
begin with a docstring and local settings, for example:

```python
"""Restart an owned llama.cpp provider after one invalid empty response."""

ENABLED = False
```

If `ENABLED` is absent, it defaults to `False`. A disabled hook is discovered
and logged but is not imported or registered. This keeps all supplied and
experimental hooks inactive by default. Import and registration failures are
logged with the hook ID and do not stop the core; that hook remains unavailable.
Hook files are administrator-controlled executable code and must not be
writable by untrusted users.

## Minimal Example

`hooks/example.py` is the complete smallest hook. It is disabled by default,
explicitly avoids registration while disabled, and has no side effects because
all illustrative mutations and cancellations remain comments:

```python
"""Minimal hook example."""

ENABLED = False


async def on_task_created(task):
    """Observe a newly created task."""
    # task.priority = 100
    # task.hook_metadata["example"] = "created"
    # await hooks.cancel_task(task, reason="Rejected by example hook")


async def before_llm_request(task, request):
    """Observe a task before inference."""
    # task.model = "another-model"
    # request["temperature"] = 0
    # await hooks.cancel_task(task, reason="Inference disabled by example hook")


def register(hooks):
    """Register example handlers when enabled."""
    if not ENABLED:
        return

    hooks.on("task_created", on_task_created)
    hooks.on("before_llm_request", before_llm_request)
```

To try an idea, an administrator sets `ENABLED = True`, adds only the required
handler logic, and restarts the core. No configuration entry, package install,
or core-code change is required.

## Registration API

Each enabled module defines `register(hooks)`. It registers one or more
handlers:

```python
def register(hooks):
    hooks.on("llm_response_complete", on_llm_response_complete)
```

Handlers are async callables. The manager invokes handlers serially in loading
and registration order, so hooks have deterministic effects. The public event
names and arguments are:

| Event | Timing | Handler arguments |
|---|---|---|
| `task_created` | Immediately after task creation | `task` |
| `before_llm_request` | Immediately before a worker sends a request to an LLM | `task`, `request` |
| `llm_response_complete` | After the complete LLM response or error is assembled, before consumer delivery | `task`, `response` |
| `before_consumer_delivery` | Immediately before the completed result is sent to the endpoint/client | `task`, `response` |

`task` exposes the existing task data and a hook-scoped metadata namespace.
`request` and `response` are the normalized internal values. The full upstream
response is available at `llm_response_complete`, including errors, so hooks
must not inspect partial streaming chunks.

The registration object also exposes narrowly scoped core actions rather than
the whole `Core` object:

```python
await hooks.restart_local_llama_cpp(provider_id)
await hooks.retry_task(task, provider_id=provider_id)
await hooks.cancel_task(task, reason="Cancellation reason")
```

`restart_local_llama_cpp` may stop and start only an aidir-owned local
`llama.cpp` provider. It must use the existing local-server manager and reject
external or unowned providers. `retry_task` resets the task's current LLM
attempt and requeues it using the selected provider. It returns control to the
task lifecycle: the original response is discarded and is never delivered to
the consumer. `cancel_task` marks the task as cancelled with the provided
reason and stops its later lifecycle processing.

Handler failures are logged and do not alter the task outcome. A handler may
return an explicit retry action only through the registration object's
`retry_task`; it cannot silently fabricate a successful response.

## Initial Hook: llama.cpp Empty-Response Recovery

The initial bundled single-file hook listens to `llm_response_complete`. It
applies only to an OpenAI-compatible result from a `llama.cpp` provider.

It treats a response as invalid when all of the following are true:

1. the assembled assistant content is empty;
2. reasoning is absent, empty, or consists entirely of `/` characters;
3. the response does not represent an upstream error.

For the first invalid result of a task, the hook:

1. records an attempt marker in hook-scoped task metadata;
2. restarts the resolved local, aidir-owned `llama.cpp` provider;
3. retries the same task once against that provider.

The marker persists with the task, so any later identical invalid response is
passed through unchanged. This avoids a retry loop and preserves the normal
error/result behavior after the one recovery attempt. If the provider is
external, unowned, or cannot be restarted, the hook logs the failure and leaves
the original response untouched.

After a successful provider restart, the hook writes one system `info` record
with its name, the provider ID, and task ID. Normal hook execution is not
logged, preventing experimental hooks from generating high-volume logs.

## Implemented Design

`core/hooks.py` implements the manager owned by `Core`. It loads hooks after
core services are initialized and invokes the lifecycle events before the
corresponding queue terminal transition. `task_created` runs after queueing;
the LLM events apply to agent tasks, and `before_consumer_delivery` runs before
the endpoint is signaled for every task result.

`hooks/example.py` and `hooks/llama_cpp_empty_response_recovery.py` are shipped
disabled. The recovery hook is intentionally opt-in because it restarts a local
provider; it only acts on an aidir-owned `llama.cpp` process and persists its
one-attempt marker in the task.

Regression coverage verifies discovery, disabled hooks, event delivery,
retry/cancellation suppression, and the one-retry llama.cpp recovery path.

Hooks do not add configuration schema, hot reload, sandboxing, dependency
installation, arbitrary event names, or public endpoint APIs in this iteration.
