# AI Director (aidir) — Technical Specification

> **Status:** this document replaces the original `SPEC.md`, which was written for the
> first prototype and no longer describes the system. `SPEC.md` stays read-only until
> this document is accepted as its successor.
>
> **Audience:** developers and agents working on the codebase. It assumes the reader
> can read Python and knows what an HTTP server, a Redis queue and a JSON5 file are.
> It does **not** assume they know anything about aidir.
>
> **Language:** English, per `AGENTS.md`. The Russian original is `SPEC.md`.

---

## 0. About this document

### 0.1 How to read it

| If you want to… | Read |
|---|---|
| Understand what the project is for | [§1](#1-project-purpose) |
| Get oriented in the code | [§2](#2-system-overview), [§3](#3-domain-model), [§8](#8-file-system-layout) |
| Add or change an endpoint | [§5](#5-protocols-and-apis), [§6.2](#62-endpoint-layer) |
| Add or change a worker | [§3.2](#32-worker), [§6.7](#67-workers) |
| Change routing, scheduling or resources | [§6.4](#64-scheduler) – [§6.6](#66-smartrouter-and-kv-affinity), `spec/scheduling.md` |
| Change configuration | [§9](#9-configuration), `spec/configuration.md` |
| Change the WebUI | [§6.9](#69-webui) |
| Deploy, diagnose or operate | `spec/operations.md` |
| Know what is broken | [§14](#14-known-problems-open-tasks-and-development-directions) |
| Pick up a task to work on | [proposal_update1.md](proposal_update1.md) — the prioritized work list |

File references use `path:line` against the working tree. Where a line number might
drift, the file name alone is given as well.

### 0.2 Document map

This document is the entry point. Large topics that would otherwise swamp it live in
satellite documents under `spec/`:

| Document | Covers |
|---|---|
| **SPEC_NEW.md** (this file) | Purpose, architecture, domain model, component interfaces, implementation decisions, file system, configuration model, security, testing, known problems |
| [spec/protocols.md](spec/protocols.md) | Complete HTTP surface: every route, request and response contract, headers, error envelopes, streaming rules |
| [spec/scheduling.md](spec/scheduling.md) | Task lifecycle, queue semantics, scheduler algorithm, resource model, smart routing, KV affinity |
| [spec/configuration.md](spec/configuration.md) | `aiset` integration, descriptor format, document model, worker-local configuration, generated settings editor, configuration assistant |
| [spec/operations.md](spec/operations.md) | Installation, systemd, Docker/Compose, nginx, doctor, cron, logs, audit retention, test protocol |

Pre-existing documents that remain authoritative for their own scope:

| Document | Scope |
|---|---|
| [README.md](README.md) | User-facing installation and operation. Not a specification; it is a manual. |
| [TEST.md](TEST.md) | Test protocols and acceptance matrix |
| [openaix_spec.md](openaix_spec.md) | The OpenAIx protocol extensions (normative, predates this document) |
| [spec_timeouts.md](spec_timeouts.md) | The four timeout semantics (normative) |
| [plan_aiset_integration.md](plan_aiset_integration.md) | Why the settings subsystem moved to `aiset` |
| [proposal_update1.md](proposal_update1.md) | **The current work list**: the 45 confirmed defects and design gaps, each with its evidence, the proposed modification and its validation |
| [proposal_update_3.md](proposal_update_3.md) | The defects of the two production investigations: the managed local server and the task that waits on a remote aidir |
| [proposal_*.md](DOCUMENTS.MD) | Frozen design proposals; see [DOCUMENTS.MD](DOCUMENTS.MD) for the index |
| [memory/context.md](memory/context.md) | Working state, open decisions and traps between sessions |
| [memory/history.md](memory/history.md) | Chronological request log |

### 0.3 Conventions

- "aidir" is the project, the process and the configuration instance.
- `provider` is an upstream service configuration; `model` is one model inside a provider;
  a `smart` provider is a provider whose models are *candidate lists*, not real models.
- "Executor" means the concrete upstream that actually served a request.
- Protocol names are lowercase (`openaix`, `ollama`, `anthropic`, `mcp`); the OpenAIx
  protocol is the union of the Ollama and OpenAI wire formats plus aidir extensions.

---

## 1. Project purpose

### 1.1 The problem

A machine that runs local LLMs has a small number of expensive resources (VRAM, one
GPU, one llama-server process) and a large number of clients that speak incompatible
protocols (Ollama, OpenAI, Anthropic Messages, MCP) and want incompatible things
(priority, streaming, tool use, embeddings, context reuse).

Running Ollama directly gives you none of the following:

- **No queue.** Two clients asking for the same model collide; the second one either
  waits inside the model server with no visibility, or fails.
- **No admission control.** Nothing knows that a 27B model does not fit next to
  another 27B model in 22 GiB of VRAM.
- **No protocol unification.** A coding agent that speaks Anthropic Messages cannot
  talk to an Ollama-only machine.
- **No request affinity.** A multi-turn conversation re-sends the same prefix to
  whatever executor happens to be free, destroying the provider's KV cache and
  multiplying latency.
- **No visibility.** Which model served which request, what it cost, what the upstream
  actually answered — none of it is recorded in a form you can inspect.

### 1.2 What aidir is

aidir is an **orchestrator** that sits between clients and inference backends.

```
Client → Endpoint → [middleware] → Queue → Scheduler → Worker → upstream AI → Client
```

It accepts requests in Ollama, OpenAI, OpenAIx, Anthropic Messages and MCP, places
them in a Redis-backed priority queue, decides *which* concrete model may serve them
given the current hardware state, runs them through a worker that speaks the upstream
protocol, and returns the answer — synchronously or streamed — while preserving the
upstream's original bytes wherever the protocols match.

It also owns the operational surface: a WebUI with a live task queue, resource view,
log stream and audit viewer, a generated configuration editor with an assistant, a
`doctor` that diagnoses the host, and a cron cycle that keeps logs, audit storage and
model keep-alive in order.

### 1.3 Goals

1. **Protocol transparency.** When the client's protocol equals the executor's
   protocol, the client receives the executor's original body, content type and
   stream events byte for byte. aidir does not re-serialize what it does not have to.
2. **Resource correctness.** A model is never dispatched when it does not fit. The
   scheduler knows the measured free capacity of every resource, and reservations are
   atomic.
3. **Prefix affinity.** A conversation stays on the executor that already holds its
   prefix, so the provider's KV cache survives.
4. **One configuration surface.** Every knob is described by a schema, validated on
   read and on write, and editable from the browser without hand-editing JSON5.
5. **Full auditability.** Every client request, every upstream request and every
   response is retained, task-correlated, and inspectable.

### 1.4 Non-goals

- aidir is **not** a model server. It never loads weights itself (except by starting
  and supervising a `llama-server` it owns).
- aidir is **not** a multi-tenant SaaS platform. Authorization exists but is
  deliberately simple: bearer tokens and environment ids.
- aidir does **not** implement a chat UI, a model catalogue UI, or an inference
  engine.
- aidir does **not** abstract away the upstream protocols. It converts between them
  where it must, and is otherwise byte-transparent.

### 1.5 Design principles

These principles explain most of the individual decisions later in the document.

| Principle | Consequence |
|---|---|
| **Never lose the upstream's answer** | Errors and successes are forwarded verbatim when protocols match (§7.1) |
| **Fail closed on capacity** | A resource whose free capacity cannot be measured blocks dispatch instead of guessing (§7.4) |
| **Defer, never block** | The scheduler re-queues a task instead of holding a worker slot while waiting (§7.12) |
| **The schema is the contract** | An undeclared configuration key is a startup error, not a warning (§7.8) |
| **The host owns the design** | The library generates markup; this project owns the visual language (§7.9) |
| **Assistants stage, humans apply** | The configuration assistant cannot write (§7.10) |
| **Append-only observability** | Audit journals are immutable; the index is disposable (§7.11) |
| **One source of truth per fact** | Alias → model → provider → worker resolution happens once, and both the endpoint and the scheduler use the same policy (§6.6) |

---

## 2. System overview

### 2.1 Components

```
                                   ┌──────────────────────────────┐
  browser / client ───:8080──────▶│ nginx (docker)               │
                                   │  /api/ /ws/ /assets/ → :20082│
                                   └──────────────┬───────────────┘
                                                  │
  client ───────────────────:21434───────────────┼────▶ OpenAIx endpoint
  client ───────────────────:21435───────────────┼────▶ Anthropic endpoint
  client ───────────────────:20001───────────────┼────▶ MCP endpoint
                                                  ▼
                          ┌───────────────────────────────────────────┐
                          │ core/app.py  (single asyncio process)      │
                          │                                           │
                          │  Endpoint layer (FastAPI + uvicorn)       │
                          │      │                                    │
                          │      ▼                                    │
                          │  QueueManager ◀──▶ Redis                  │
                          │      │                                    │
                          │      ▼                                    │
                          │  Scheduler ──▶ Resources / SmartRouter    │
                          │      │           KV affinity store         │
                          │      ▼                                    │
                          │  Worker (openaix, call_ollama, ...)       │
                          │      │                                    │
                          │      ▼                                    │
                          │  upstream (Ollama / llama.cpp / OpenAI /   │
                          │            Anthropic / OpenCode Go)        │
                          │                                           │
                          │  Settings runtime ──▶ aiset (submodule)   │
                          │  WebUI backend ──▶ /api/*  /ws/logs        │
                          │  AuditLog ──▶ logs/audit/                  │
                          └───────────────────────────────────────────┘
                                                  │
                                    ┌─────────────┴──────────────┐
                                    ▼                            ▼
                          Redis (docker)              Ollama / llama-server
```

### 2.2 Runtime topology and ports

| Port | Service | Bound by | Traffic | Source of truth |
|---|---|---|---|---|
| `8080` | nginx public entrypoint | Docker publish of container `:80` | HTTP + WebSocket | `.env: NGINX_HTTP_PORT`, `docker-compose.yml` |
| `20082` | WebUI backend | `core/app.py` (uvicorn) | HTTP + WS | `.env: WEBUI_PORT`, `config.json5: webui.port` |
| `21434` | OpenAIx / Ollama-compatible endpoint | `core/app.py` | HTTP | `.env: OPENAIX_ENDPOINT_PORT`, `config.json5: endpoints[]` |
| `21435` | Anthropic Messages endpoint | `core/app.py` | HTTP | `.env: ANTHROPIC_ENDPOINT_PORT`, `config.json5: endpoints[]` |
| `20001` | MCP endpoint | `core/app.py` | HTTP JSON-RPC | `.env: MCP_ENDPOINT_PORT`, `config.json5: endpoints[]` |
| `6379` | Redis | Docker container | TCP | `.env: REDIS_HOST/PORT`, `docker-compose.yml` |
| `11434` | Ollama upstream | external | HTTP | `.env: OLLAMA_BASE_URL` |

Rules that the installer enforces (`install.sh`):

- nginx always listens on container port `80`; Docker publishes it to host
  `${NGINX_HTTP_PORT}`.
- The WebUI backend and the OpenAIx/Anthropic/MCP endpoints are separate listeners on
  separate ports; they are never multiplexed.
- `install.sh` probes every listener port before starting anything and **reserves**
  the ports it accepted, so two aidir listeners can never be assigned the same host
  port (`install.sh:212-311`).
- On a fresh install an occupied port is silently moved to the next free one and the
  change is written to `.env`; on a re-install the installer asks first and refuses
  to change a port without a TTY (`install.sh:276-311`).

### 2.3 Process model

There is exactly **one** long-lived application process: `venv/bin/python core/app.py`.

It owns:

- one asyncio event loop,
- one Redis client,
- one `QueueManager`,
- one `Scheduler`,
- one `Resources` registry,
- one `KvAffinityStore` and one `ModelReadinessTracker` (in-memory),
- one `AuditLog` with a dedicated writer thread,
- one `ResourceMonitor` task,
- one optional embedded cron task,
- one uvicorn server per configured endpoint,
- one uvicorn server for the WebUI.

Redis and nginx run as separate containers. Ollama and any `llama-server` are external
processes; aidir may start and supervise the latter.

### 2.4 Startup sequence

`core/app.py:798-915` (`main`) and `core/app.py:149-299` (`Core.start`):

1. **Pre-import dispatch** (`core/app.py:21-38`). `core/app.py` handles `--help` and
   the `doctor` sub-command *before* importing `core.config`, because that module
   builds the process-wide configuration at import time and raises when a document is
   invalid — exactly the situation the doctor exists to diagnose.
2. `doctor.startup_report()` — a one-line summary and a two-line hint. Never raises.
3. `faulthandler.register(SIGUSR1)` — dumps all thread stacks on demand.
4. `Core.start()`:
   1. `AuditLog` — directory, limits, writer thread started.
   2. Redis — `redis.asyncio.from_url(...)`. A failed ping prints a diagnostic block
      naming the exact host/port and the `docker compose` commands, then re-raises.
      **There is no degraded mode; Redis is a hard dependency.**
   3. `QueueManager` — namespace = `config.instance` (default `aidir1`).
   4. **Startup recovery** — `queue.recover_startup_tasks()`.
   5. `EnvidRegistry` — load from Redis, then merge from config (config wins).
   6. **Workers** — discovery, import, `initialize()`.
   7. `Resources` + `LocalServerManager` + `HookManager`; `validate_model_parallel_config()`
      raises `RuntimeError` on an inconsistent parallel model.
   8. **Adoption** of healthy inherited `llama-server` processes; restore of idle-model
      reservations from Redis activity records.
   9. `Scheduler` — constructed with the queue, workers, resources, hooks, monitor and
      the affinity store.
   10. Hooks loaded; resource monitor started; embedded cron started if enabled.
5. Endpoints are built from `config.endpoints` and bound — *after* the Core is fully
   initialized, so no socket accepts traffic before Redis, workers and the scheduler
   exist.
6. The WebUI server is built and bound.
7. Signal handlers are installed: `SIGHUP` → config reload, `SIGTERM`/`SIGINT` →
   graceful shutdown.

### 2.5 Shutdown and restart

`Core.graceful_shutdown` (`core/app.py:476-562`):

- New **external** tasks are rejected with `ServiceBusyError(code="RESTART_IN_PROGRESS")`;
  queued external tasks are cancelled.
- Running tasks get a drain window:
  - `SIGTERM`/`SIGINT` → a short 5-second drain, then cancel;
  - a WebUI-requested restart → the full `tasks.restart_wait_timeout`
    (default 120 s), and cancel only if it expired.
- Then the scheduler stops, the audit writer drains, managed local servers are stopped
  and `Redis` is closed.

`SIGHUP` reloads the configuration *only*: `Core.reload_config` → `runtime.refresh()`.
Endpoints, workers and the resource model are **not** rebuilt. See
[§9.4](#94-apply-policies) for which configuration changes are actually picked up by
a reload and which need a restart.

---

## 3. Domain model

### 3.1 Task

`core/task.py`. A task is one unit of queued work.

| Field group | Fields |
|---|---|
| Identity | `type` (`agent`\|`tool`), `id` (uuid4), `created_at` |
| Routing | `worker_id`, `payload` (dict), `priority`, `config` (dict, holds `route`, `kv_client_key`, `route_trace`, `incoming_bearer_token`, `request_kind`, `opencode_session`) |
| Lifecycle | `status`, `updated_at`, `started_at`, `finished_at`, `result`, `upstream_response`, `error`, `llm_call_count`, `llm_call_history`, `hook_metadata` |
| Timeouts | `queue_timeout` (s), `run_timeout` (s) |
| Retry | `retry_count`, `retry_period`, `retry_attempt`, `next_retry_at`, `fallbacks`, `fallback_index`, `on_reject` |
| Resources | `resource_requirements` (`{resource_id: {metric: amount}}`) |
| Viewer | `request_preview`, `route_provider_id`, `model_id`, `envid` |
| Origin | `external` (bool) — true for endpoint-created tasks |
| Chain | `parent_worker`, `parent_context` |

Two concrete types (`core/task_types/`):

| Type | Queue | Extra | Created by |
|---|---|---|---|
| `Task_agent` | `{ns}:queue:agent` | `stream: bool` | The Ollama/OpenAIx/Anthropic endpoints |
| `Task_tool` | `{ns}:queue:tool` | — | The MCP endpoint, and `openaix` for internal tool calls |

**States** (`core/task.py:18-23`):

```
created ──▶ queued ──▶ running ──▶ completed
                │          ├──────▶ failed
                │          └──────▶ canceled
                └─────────────────▶ canceled
```

`completed`, `failed` and `canceled` are terminal. All three push a `None` sentinel
into the task's chunk queue and set its done event, which is how a waiting endpoint
wakes up.

**Serialization.** `Task.to_redis_hash()` writes every field as a string (Redis HASH
constraint); `from_redis_hash` reverses it and dispatches on `type`. An unknown type
raises `ValueError`, which is exactly what makes the scheduler quarantine a corrupt
queue member instead of crashing (`core/queue_manager.py:72-141`).

Known persistence gaps: `fallbacks`, `on_reject` and `next_retry_at` are **not**
persisted, so a task that round-trips through Redis loses its fallback chain.

### 3.2 Worker

`core/worker.py`. A worker is a pluggable unit of execution discovered from the
file system:

```
workers/<type>/<id>/app.py    →    must expose a module-level `worker` object
```

The directory name **is** the worker id; the loader overwrites `instance.id`
(`core/workers_loader.py:116`).

Contract:

```python
class BaseWorker:
    task_type: str = ""            # "agent" | "tool" | "context" | ...
    enabled: bool = True
    is_async: bool = False
    tags: list[str] = []

    async def initialize(self, config: dict) -> None: ...
    async def execute(self, task: Task, emit_chunk=None) -> WorkerResult: ...
```

`WorkerResult` (`core/worker.py:20-27`):

```python
@dataclass
class WorkerResult:
    ok: bool
    data: dict | None = None                 # final response payload
    error: dict | None = None                # error dict when ok is False
    usage: dict | None = None
    upstream_response: UpstreamResponse | None = None
```

A worker signals failure either by raising or by returning `ok=False`. A raised
exception becomes `{"code": "EXCEPTION", "message": str(exc)}`.

`BaseToolWorker` (`core/worker.py:433-448`) adds `task_type = "tool"` and
`get_tool_description()`, which returns MCP-style metadata
(`name`, `description`, `inputSchema`). One worker may publish several tools.

**Worker families present in the tree** (`workers/`):

| Family | Directory | Ids |
|---|---|---|
| Agent | `workers/agent/` | `openaix`, `call_ollama`, `call_openai`, `call_llama_cpp`, `call_anthropic`, `call_opencode_go`, `echo_agent`, `reject_agent` |
| Tool | `workers/tool/` | `web_search`, `web_fetch`, `external_mcp`, `http_api`, `selftest` |
| Context | `workers/context/` | `context_builder`, `context_add_internal_tools`, `context_render_openclaw_style` |

`context` workers are **not** queued. They run in-process inside `openaix`'s context
chain ([§6.7](#67-workers)).

### 3.3 Endpoint

`core/endpoint.py`. `BaseEndpoint` is the abstract base:

```python
class BaseEndpoint(ABC):
    id: str = ""
    api: str = ""

    @abstractmethod
    def create_app(self, core: "Core") -> Any: ...     # returns a FastAPI app
    @abstractmethod
    async def initialize(self, core: "Core") -> None: ...
```

Registration is **configuration-driven**: `config.endpoints[]` entries are matched by
their `api` field in `Core._init_endpoints` (`core/app.py:761-783`). Each entry
produces exactly one uvicorn server. An unknown `api` is logged and skipped.

The class hierarchy is deliberately inverted from intuition:

```
BaseEndpoint
└── Endpoint_ollama            (api="ollama")    the minimal Ollama surface + all shared machinery
    └── Endpoint_openaix       (api="openaix")   the wide surface
        └── Endpoint_anthropic (api="anthropic") the Messages surface
BaseEndpoint
└── Endpoint_mcp               (api="mcp")       JSON-RPC, outside the hierarchy
```

`Endpoint_openaix` inherits all routing, auth, smart-routing and streaming machinery
from `Endpoint_ollama`; `Endpoint_anthropic` inherits the task-building pipeline and
overrides only the HTTP surface. `Endpoint_mcp` sits outside the hierarchy and
re-implements the audit helpers.

### 3.4 Provider, model, smart route

`config.models.providers.<provider_id>` is one upstream service:

```json5
{
  "id": "ollama_local",
  "api": "ollama",                 // ollama | openaix | openai | opencode-go | anthropic | llama-cpp | smart
  "baseUrl": "http://127.0.0.1:11434",
  "auth": { "type": "bearer", "token": "${...}" },
  "exec_cmd": "",                  // llama-cpp only: the command that starts llama-server
  "models": [ { "id": "...", "alias": "...", "protocol": "chat-completions" } ]
}
```

A model inside a provider may be:

- a **real model** — `id`, optional `alias`, optional `protocol`
  (`chat-completions` | `anthropic-messages`), optional `embedding: true`,
  `resources` (base footprint), `sessionResources` (per-request cost),
  `maxConcurrentRequests`, `cache_group`, `contextWindow`, `max_output_tokens`,
  `default_tool_injection`, `upstream_retry_count`.
- a **smart model** — `type: "first_available"` plus `items[]`, each
  `{ provider, model, request_timeout_ms, fallback_prio, cache_group }`.

**Alias resolution** (`core/endpoints/endpoint_openaix.py:1350-1421`) ranks matches:

| Rank | Match |
|---|---|
| 0 | `alias` equals the requested name |
| 1 | `model.id` equals the requested name and the model has **no** alias |
| 2 | `model.id` equals the requested name and the model **has** an alias |

Ties are broken by (rank, preferred-provider penalty, configuration order). A
duplicate alias or a duplicate unaliased id produces a **one-time warning**, not an
error — the endpoint silently picks one.

**Smart resolution** (`core/smart_router.py`) probes every candidate and picks one.
See [§6.6](#66-smartrouter-and-kv-affinity) and `spec/scheduling.md`.

### 3.5 Resource

`core/resource.py`, `core/resources.py`. A resource is a **capacity pool with a
metric vector**, not a device:

```json5
{
  "id": "local_machine",
  "type": "cuda",                       // informational only
  "limits": { "VRAM": 22000 },
  "units": { "VRAM": "MiB" },
  "provider": "ollama_local",           // used for the unload / keep-alive call
  "alive_time": 300,                    // seconds a model stays resident after release
  "keep_alive": 3600,                   // keep-warm window
  "availability": { "command": "...", "metric": "VRAM" },
  "monitoring": { "poll_interval": 10, "sensors": [ ... ] },
  "telemetry": { "sensors": ["gpu_temperature"] }
}
```

Two reservation kinds:

| Kind | Meaning | Consumer id |
|---|---|---|
| **Hard** | An in-flight task's reservation | `{task_id}:{worker_id}` |
| **Soft** | A model still resident in VRAM after its task ended | `base:{provider}:{model}` |

Soft occupancy keeps memory honest — an idle model still blocks a bigger one — at the
cost of liveness; the scheduler resolves the conflict with an explicit
`waiting_unload` admission state and a force-unload path, not with an eviction policy.

### 3.6 Envid and context

An **envid** (`core/envid.py`) is a named execution environment: a context-building
specification plus per-environment worker overrides, optionally inheriting from
another envid through `parent`.

```python
@dataclass
class Envid:
    id: str
    description: str = ""
    parent: str | None = None        # template reference, resolved at load
    context: dict = {}               # context-building config
    workers: dict = {}               # per-worker overrides for this envid
    saved_at / used_at / extra
```

A **context** (`core/context.py`) is the payload the context chain builds:

```python
@dataclass
class Context:
    envid: str | None
    system_rendered: str     # the rendered system prompt
    tooling / safety / skills / files / history / tools / rules / meta
```

`tools` is the resolved internal-tool map: `name → {description, inputSchema, worker}`.

### 3.7 Session

Two unrelated kinds of session exist; do not confuse them.

| Kind | Where | Purpose |
|---|---|---|
| **WebUI session** | Redis `aidir:session:<token>` | Browser login; carries `{login, permissions}` |
| **OpenCode session** | Redis `aidir:opencode-caller-session:v1:<task_id>` and `aidir:openai-session:v1:<provider>:<model>:<digest>` | A conversation binding for the OpenCode Go upstream; Fernet-encrypted at rest |

The OpenCode session **identity digest** is an HMAC over
`v1:{identity_source}:{identity_value}`, where the identity source is, in priority
order: the authenticated bearer token, the envid, or `host|user-agent` for anonymous
callers. The digest never leaves the process and the session value is never logged.

### 3.8 Configuration document

A configuration document is a JSON5 file with a **descriptor** that names every legal
key. Documents are:

| Id | File | Descriptor |
|---|---|---|
| `main` | `config.json5` | `config.descriptor.json5` |
| `worker:<type>:<id>` | `workers/<type>/<id>/config.json5` | `workers/<type>/<id>/descriptor.json5` |

See [§9](#9-configuration) and `spec/configuration.md`.

---

## 4. Request lifecycle

### 4.1 Inbound request → task → response

```
1  HTTP request arrives at an endpoint
2  RequestBodyLimitMiddleware     reject > http.max_request_size with 413
3  request-id middleware           set/propagate X-Request-ID
4  ingress logging                log method + path
5  parse JSON body                400 INVALID_REQUEST on failure
6  authorize                      optional bearer token; envid check
7  route-trace check              409 ROUTING_LOOP / ROUTING_HOPS_EXCEEDED
8  resolve the model              alias → provider → model (or smart route)
9  build the Task                 Task_agent or Task_tool
10 audit the client request       audit_log
11 core.on_task_added(task)       503 if the service is busy
12 ── wait for the first chunk ── the task runs in the background
13 ── wait for a terminal state ──
14 map the status to HTTP         200 / 4xx / 5xx
15 audit the client response      audit_log
16 delete the task                in-memory; Redis hash kept for external tasks
```

Step 12 is the **two-phase streaming** decision: the endpoint waits for the first
chunk before opening the response, so a failure that happens before any output still
carries a meaningful HTTP status. See [§7.2](#72-two-phase-streaming).

### 4.2 Dispatch path

```
Scheduler.run loop (core/scheduler.py:73-251)
  for task_type in ("agent", "tool"):
      pop the most urgent task (Redis ZPOPMIN, lowest score = highest priority)
      gates:
        1  the task is still in memory
        2  the queue timeout has not expired
        3  next_retry_at is in the past
        4  a worker exists for the task type
        5  the smart route is refreshed
        6  resource requirements are resolved
        7  measured availability is refreshed (fail closed)
        8  a remote aidir reports it can run the model now
        9  admission control (concurrency / session memory)
       10  legacy admission (reuse / force-unload / reserve)
      if a gate fails: defer (next_retry_at = now + 1s or 5s) and re-queue
      else: mark_running, reserve, run the worker, release, mark the terminal state
```

Full detail in `spec/scheduling.md`.

### 4.3 Failure paths

| Failure | Where it is handled | Result |
|---|---|---|
| Upstream HTTP error | worker → `build_upstream_error` | The **original** status, body and content type are returned to the client when protocols match |
| Upstream unreachable / timeout | worker | `UPSTREAM_UNREACHABLE` / `UPSTREAM_TIMEOUT`; the task fails |
| Queue timeout | scheduler | `QUEUE_TIMEOUT`, HTTP 504 |
| Run timeout | scheduler (`asyncio.wait_for`) | `TIMEOUT`, HTTP 504 |
| Client disconnect | endpoint | the task keeps running; the response is dropped |
| Smart-route executor failure | scheduler | the failed candidate is forgotten and the task is re-queued on the next candidate |
| Worker raises | scheduler | `EXCEPTION`, HTTP 502 |
| Hook cancels | scheduler | `HOOK_CANCELED` |
| Service restart | `Core.graceful_shutdown` | queued external tasks cancelled, running tasks drained then cancelled |
| Process crash | `queue.recover_startup_tasks` | queued tasks restored, running tasks marked `SERVICE_RESTARTED` |

A deliberate rule appears repeatedly in the failure paths: **a secondary failure must
not mask the original error**. When a re-queue fails, the scheduler logs it and
returns `False` rather than raising, so the client still sees the real
`UPSTREAM_ERROR` instead of an aidir-internal exception (`core/scheduler.py:649-673`).

---

## 5. Protocols and APIs

The full route tables with request and response contracts are in
[spec/protocols.md](spec/protocols.md). This section is the map.

### 5.1 Endpoint summary

| Endpoint | `api` | Default port | Auth | Purpose |
|---|---|---|---|---|
| OpenAIx | `openaix` | 21434 | token for chat/embeddings; none for the model lists and queue-state | The main client surface: Ollama + OpenAI + embeddings + introspection |
| Anthropic Messages | `anthropic` | 21435 | token (`x-api-key` or `Bearer`) | Anthropic SDK clients and coding agents |
| MCP | `mcp` | 20001 | **none** | JSON-RPC tool exposure |
| Ollama (minimal) | `ollama` | 21434 | token | The base class of the above; rarely configured on its own |

### 5.2 OpenAIx

Routes:

| Method | Path | Notes |
|---|---|---|
| POST | `/api/chat` | Ollama-compatible chat, NDJSON stream |
| POST | `/api/embed` | Ollama-compatible embeddings, non-streaming |
| GET | `/api/tags` | Model list, Ollama shape |
| POST | `/api/show` | Model metadata, synthesized from config |
| POST | `/v1/chat/completions` | OpenAI-compatible chat, SSE stream |
| POST | `/v1/embeddings` | OpenAI-compatible embeddings, non-streaming |
| GET | `/v1/models` | Model list, OpenAI shape |
| GET | `/api/providers/{provider}/models/{model}/queue-state` | Read-only queue state |
| GET | `/v1/providers/{provider}/models/{model}/queue-state` | The same, OpenAI-shaped path |
| GET | `/api/models/{model}/queue-state` | Alias-only variant |
| GET | `/v1/models/{model}/queue-state` | Alias-only variant |
| GET | `/api/resources`, `/v1/resources` | Peer resource list |
| GET | `/api/resources/{id}`, `/v1/resources/{id}` | One peer resource |
| GET | `/v1/tasks` | Peer task list (read-only, sanitized) |
| GET | `/health` | `{"status":"ok"}` |

Protocol extensions over plain OpenAI/Ollama are specified normatively in
[openaix_spec.md](openaix_spec.md). The short list: the `worker` routing field,
`envid` with user-scoped access control, `context_builder` per-request pipeline
overrides, `log.options.save_llm_request`, `timeout` / `queue_timeout`, the built-in
server-side internal tool loop, the `errors_compatibility_mode` switch, the
queue-state endpoints, the peer resource and task endpoints, and `real_id` on the
model lists.

### 5.3 Anthropic Messages

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/messages` | Anthropic Messages |
| POST | `/v1/messages/count_tokens` | Deterministic local estimate; creates no task and calls no model |
| GET | `/v1/models` | Anthropic-shaped model list |
| GET | `/health` | Health check |

Conversions live in `core/messages_protocol.py` (a pure module with no HTTP, Redis or
FastAPI dependency, so both the endpoint and the workers use it). System prompts,
images, tool use, tool results, thinking blocks, usage, stop reasons and errors are
converted; a native Anthropic upstream is forwarded byte for byte to an Anthropic
client.

`count_tokens` is `ceil(total_characters / 4)` over the system text, the message text
and the serialized tool schemas — an estimate, not a tokenizer.

### 5.4 MCP

One JSON-RPC 2.0 endpoint at `POST /mcp`.

| Method | Behavior |
|---|---|
| `initialize` | Protocol version, capabilities, server info |
| `ping` | `{"result": {}}` |
| `notifications/initialized` | HTTP 204 |
| `tools/list` | The tools of the workers configured in `endpoints[mcp].tools` |
| `tools/call` | Runs a tool worker as a queued `Task_tool` |
| anything else | HTTP 404 with JSON-RPC error `-32601` |

The MCP endpoint has **no authentication**. This is a deliberate, documented gap —
see [§12](#12-security-model) and [§14](#14-known-problems-open-tasks-and-development-directions).

### 5.5 WebUI API

`webui/backend/app.py`. Route groups:

| Group | Routes |
|---|---|
| Auth | `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout` |
| Tasks | `GET /api/tasks`, `GET /api/tasks/viewer/meta`, `GET /api/tasks/viewer/search`, `GET /api/tasks/viewer/{id}`, `GET /api/tasks/viewer/{id}/detail`, `GET /api/tasks/viewer/{id}/request`, `GET /api/tasks/viewer/{id}/raw`, `GET /api/tasks/viewer/audit-files/{file_id}`, `POST /api/tasks/{id}/terminate`, `POST /api/tasks/{id}/extend-timeout` |
| Status | `GET /api/status`, `POST /api/cron/repair`, `POST /api/cron/embedded/enable`, `POST /api/resources/{id}/use`, `POST /api/resources/{id}/force-release`, `POST /api/restart` |
| Logs | `GET /api/logs/files`, `GET /api/logs`, `GET /api/logs/search`, `WS /ws/logs` |
| Catalog | `GET /api/workers/models`, `GET /api/endpoints/info` |
| Test | `POST /api/test/llm`, `POST /api/test/mcp`, `GET /api/test/agent/endpoints`, `GET /api/test/agent/catalog`, `GET /api/test/agent/models`, `POST /api/test/agent` |
| Settings (host) | `GET /api/settings-info`, `POST /api/settings/chat`, `GET /api/settings/chat/history`, `DELETE /api/settings/chat`, `POST /api/settings/reload` |
| Settings (library) | everything under `/api/settings`, mounted from the `aiset` ASGI app |

There is **no** endpoint that returns the whole resolved configuration. It used to
exist and it included every substituted credential. The tree snapshot, which honours
the secret permission, replaced it.

### 5.6 Peer APIs

An aidir instance can expose itself to another aidir instance:

- `GET /v1/resources`, `GET /v1/resources/{id}` — the resource inventory and the
  measured availability of each resource.
- `GET /v1/tasks` — the active task summaries.

Both are governed by `auth_mode` on the peer's `openaix` endpoint:

| `auth_mode` | Behavior |
|---|---|
| `disabled` (default) | Published unauthenticated on a trusted network |
| `endpoint` | Requires the normal bearer-token and environment authorization checks |

The payloads are deliberately sanitized: no request or response bodies, no sensor
commands, no credentials, no consumer lists. The task preview is truncated to 300
characters and the list is capped at 1000 rows.

### 5.7 Headers

| Header | Direction | Meaning |
|---|---|---|
| `X-Request-ID` | both | Correlation id; generated when absent; returned on every response |
| `Authorization: Bearer <token>` | in | API token (endpoints) or WebUI session |
| `x-api-key` | in | API token or Anthropic credential |
| `anthropic-version` | in | Defaults to `2023-06-01` |
| `anthropic-beta` | in | Optional upstream beta flag |
| `X-Aidir-Route-Id` | both | The route id of the current hop |
| `X-Aidir-Visited-Instances` | both | The instance ids already visited; used for loop detection |
| `X-Aidir-KV-Key` | in | A client-supplied affinity key |
| `x-opencode-session` | in | An upstream conversation binding |

The route-trace headers implement hop protection: a request that names an instance
already in the visited list is rejected with `409 ROUTING_LOOP`; more than
`routing.max_hops` (default 8) hops is `409 ROUTING_HOPS_EXCEEDED`.

### 5.8 Error envelopes

The envelope depends on the endpoint and on `errors_compatibility_mode`:

```jsonc
// OpenAI-style
{"error": {"message": "...", "type": "<code>", "code": "<code>", "param": "...", "task_id": "..."}}

// Ollama-style
{"error": {"code": "...", "message": "...", "task_id": "..."}}

// Anthropic-style
{"type": "error", "error": {"type": "timeout_error", "message": "..."}}
```

With `errors_compatibility_mode: false` every error uses the flat
`{"error": {"code", "message"}}` shape.

**Exception:** an upstream error that carries an original body is returned with the
upstream's own status, body and content type. aidir does not re-wrap it.

---

## 6. Components

Each subsection gives the responsibility, the interface and the notable internals.

### 6.1 `core/app.py` — the Core

The orchestrator. Owns every long-lived object and is the only place that wires them
together.

Public surface used by the rest of the system:

| Member | Purpose |
|---|---|
| `config` | The `AisetConfig` facade (`core/settings/facade.py`) |
| `redis` | The `redis.asyncio` client |
| `queue` | The `QueueManager` |
| `workers` | `dict[worker_id → BaseWorker]` |
| `resources` | The `Resources` registry |
| `scheduler` | The `Scheduler` |
| `envid_registry` | The `EnvidRegistry` |
| `audit_log` | The `AuditLog` |
| `hooks` | The `HookManager` |
| `llama_cpp_server_manager` | The `LocalServerManager` |
| `on_task_added(task)` | Admission into the queue; rejects when the service is busy |
| `terminate_task(task_id)` | Cancel a running task |
| `get_runtime_status()` | `restart_requested`, `accepting_new_tasks`, `active_tasks`, … |
| `get_effective_worker_config(worker_id, envid)` | Per-envid worker overrides |
| `reload_config()` | Re-read the configuration (SIGHUP) |
| `graceful_shutdown(source)` | Drain, cancel, stop |

### 6.2 Endpoint layer

`core/endpoint.py`, `core/endpoints/`.

`BaseEndpoint` provides the shared helpers:

| Helper | Purpose |
|---|---|
| `_attach_ingress_logging(app)` | Log every inbound method and path |
| `_warn_deprecated_request_timeout(cfg)` | Warn about the legacy `endpoints[].request_timeout` |
| `_task_timeout_phase(task)` | `"queue"` or `"run"` plus the remaining seconds |
| `_wait_for_task_terminal(task)` | Wait for the terminal state, in ≤1 s slices |
| `_terminate_task_on_timeout(task)` | Terminate a task whose phase expired |
| `_read_json_body(request)` | Read and parse the body |
| `_audit_pre_task_rejection(...)` | Audit a request rejected before a task existed |
| `_audit_rejection_response(...)` | Audit an error response |

`Endpoint_openaix` is the widest surface and carries the model resolution, the smart
router wiring, the route-trace validation, the remote probes, the streaming encoder
and the error mapping.

`Endpoint_ollama` is the base class and the minimal Ollama surface. Its own route
table is only `/api/chat` and `/health`; the wider Ollama routes (`/api/tags`,
`/api/show`) live on `Endpoint_openaix`.

`Endpoint_anthropic` overrides the route table completely: it serves only the
Messages routes and does **not** also serve `/api/chat`.

`Endpoint_mcp` implements JSON-RPC over HTTP and publishes the tool registry.

### 6.3 Queue manager

`core/queue_manager.py`. A Redis-backed priority queue per task type.

| Key | Type | Content |
|---|---|---|
| `{ns}:queue:agent` | ZSET | member = task id, score = priority |
| `{ns}:queue:tool` | ZSET | the same for tool tasks |
| `{ns}:task:{task_id}` | HASH | the full task, all values as strings |
| `{ns}:queue:quarantine` | HASH | unrecoverable members and the reason |

Priority is `0` = urgent, `5` = normal, `10` = idle. **Lower is more urgent**, so the
ZSET score is the priority and `ZPOPMIN` pops the most urgent task. Equal priorities
are ordered by task id (lexicographic), so there is **no FIFO guarantee within a
priority class**.

The manager owns: `add_task`, `pop_next`, `mark_running`, `mark_completed`,
`mark_failed`, `mark_canceled`, `delete_task`, `extend_active_timeout`,
`increment_llm_call_count`, `persist_llm_call_diagnostics`, `recover_startup_tasks`,
and the two queue-state aggregations.

**No TTL is ever set on a task hash.** External task hashes are reaped by the cron
job `cleanup_expired_tasks` after `tasks.external_task_live` seconds; internal task
hashes are deleted outright.

### 6.4 Scheduler

`core/scheduler.py`. One asyncio task drains the queue.

Responsibilities: pop the most urgent task, apply the dispatch gates, resolve the
worker and provider, refresh the smart route, evaluate admission, reserve resources,
run the worker, apply the retry/fallback policy, release the reservation, and write
the terminal state.

The scheduler **owns admission** and the endpoints **own routing**. The endpoint
resolves an alias once; the scheduler may re-resolve it before dispatch. Both use
the same `SmartRouter` policy and the same affinity store, so the two decisions
cannot disagree.

Full algorithm and the gate table: `spec/scheduling.md`.

### 6.5 Resources and the resource monitor

`core/resource.py` (one resource), `core/resources.py` (the registry),
`core/resource_monitor.py` (the polling loop), `core/peer_resources.py` (the peer
client).

The registry owns the reservation ledger and the admission decision:

```python
Resources.evaluate_model_admission(reqs, model_id, provider_id) -> dict
# {admission, admissible_now, admissible_after_unload, model_loaded,
#  active_count, max_concurrent_requests, session_resources,
#  free, needed, resource_ready, resource_ready_after_unload}
```

`admission` is one of `ready`, `concurrency`, `waiting_unload`, `base_memory`,
`session_memory`.

The monitor polls each resource at `monitoring.poll_interval` (default 10 s when a
command or peer probe exists) and, on every sensor, parses one numeric value —
optionally through a regex with a selectable capture group — and applies thresholds
(`above` / `below`) with reactions (`notify`, `release_resource`) and a cooldown.

### 6.6 SmartRouter and KV affinity

`core/smart_router.py`, `core/kv_affinity.py`.

The router resolves a smart model into a concrete `(provider, model)`:

1. Evaluate **every** candidate (not just the first) and collect the probes.
2. Apply the affinity policy: if the candidate that last served this context prefix
   is still healthy and not hopelessly behind, keep it — even if another candidate
   could start immediately.
3. Otherwise pick the best alternative: `(can_run_now, queue_depth, fallback_prio,
   index)`.
4. If nothing can run now, pick the lowest-`fallback_prio` candidate and queue.

The only implemented strategy is `first_available`. The descriptor also permits
`direct`, which is **not** implemented and produces HTTP 400 at request time.

**KV affinity** keeps the request with the executor that already holds its prefix:

- The **canonical context** is an append-stable line stream
  (`system, tools, tool_choice, response_format, …`, then one line per message, then
  `prompt`/`input`, then the remaining keys sorted). Prompt-shaping keys (`model`,
  `stream`, timeouts, sampling parameters, …) are excluded. Because messages are
  separate lines, a growing conversation *appends* bytes rather than rewriting the
  closing brackets of an already-hashed prefix — that is what keeps an old anchor
  valid.
- The **anchor** is an adaptive fraction of the context: 90 % up to 10 KiB, 50 % up
  to 20 KiB, interpolated down to 10 % at 100 KiB, clamped to
  `[512, 32768]` bytes and quantized onto a 256-byte ladder.
- The **hash** is `HMAC-SHA256(secret, "kv1\0alias\0kind\0client_key\0prefix")`,
  truncated to 128 bits. Without a configured secret it degrades to plain SHA-256 and
  the store is documented as internal-only. **The router never stores prompt text.**
- The **store** is in-process, TTL-bounded (default 3600 s), LRU-bounded (256 per
  alias, 20000 total). Lookup tries every stored anchor length not exceeding the
  current one and keeps the longest match.
- Affinity is **dropped** when the executor fails, when it is known to have unloaded
  the model, when its queue is longer than the least loaded alternative by both
  configured thresholds, or when a warm model has been resource-blocked for longer
  than the wait timeout. A **queue timeout does not drop it** — the executor did not
  lose its cache.

`ModelReadinessTracker` records, per candidate, when it entered a resource-blocked
run and how long it stayed there. The transition list is what makes the
"blocked ratio" anti-flap guard possible.

### 6.7 Workers

`workers/`. See [§3.2](#32-worker) for the contract.

The inheritance chain of the agent transports:

```
BaseWorker
└── OpenAIxWorker                     workers/agent/openaix/app.py
    ├── CallOpenAIWorker              workers/agent/call_openai/app.py
    │   ├── CallLlamaCppWorker        workers/agent/call_llama_cpp/app.py
    │   └── CallAnthropicWorker       workers/agent/call_anthropic/app.py
    │       └── CallOpenCodeGoWorker  workers/agent/call_opencode_go/app.py
└── CallOllamaWorker (independent)    workers/agent/call_ollama/app.py
```

`OpenAIxWorker` is the reference implementation and the base of the whole family:

- It runs the **context chain** in-process: `context_builder` →
  `context_add_internal_tools` → `context_render_openclaw_style`
  (`workers/agent/openaix/app.py:716-811`).
- It owns the **internal tool loop** (`workers/agent/openaix/app.py:928-1121`): only
  the tools aidir itself injected are executable; any other tool call is passed
  through to the client untouched. A tool call becomes a child `Task_tool` pushed
  through the normal queue, with a parent/child chain and a depth guard.
- It performs **upstream retries** for timeout / connect / HTTP / invalid-JSON
  failures, and has a special Ollama-parser-compatibility retry ladder.
- It applies the **20-message trim** (all system messages plus the last 18 others).

`CallOllamaWorker` is deliberately independent: it speaks the Ollama protocol
directly, has no context chain and no tool loop. It is the `workers.default`.

`CallLlamaCppWorker` overrides `execute()` only to ensure the managed `llama-server`
is running before delegating to `CallOpenAIWorker`.

`CallAnthropicWorker` does not override `execute()` at all — it is a pure transport
plug-in that changes the URL, the headers, the payload conversion and the response
decoding.

`CallOpenCodeGoWorker` adds session management and dispatches per model protocol.

`echo_agent` and `reject_agent` are deterministic stubs used to exercise the success
and failure paths (the reject agent is the fallback-chain fixture).

### 6.8 Settings subsystem

`core/settings/` plus the `aiset` submodule. Detailed in
`spec/configuration.md`; the short version:

| Module | Responsibility |
|---|---|
| `aiset_path.py` | Make `aiset/src` importable; fail with an actionable instruction, not a traceback |
| `runtime.py` | The single `Service`; registers every document; owns the effective cache; the `on_after_save` / `on_application` callbacks |
| `documents.py` | Find worker-local documents; the root-wins merge |
| `facade.py` | `AisetConfig` — the `config` object the rest of the code already uses |
| `wsgi.py` | The ASGI adapter and the session → principal mapping |
| `agent.py` | The configuration assistant |

The facade deviates from the library contract in three documented ways: `get()`
returns the default for an explicit `null`; `raw()` returns the **effective**
(resolved) tree; and the facade writes with the host principal, never a browser
session's.

### 6.9 WebUI

`webui/backend/app.py` (FastAPI) and `webui/frontend/` (vanilla HTML/CSS/JS, no
build step, no framework).

The backend mounts, in order:

1. the `aiset` ASGI app at `/api/settings` **and** at `/assets` (the library serves
   its editor assets at the application root, so the app is mounted twice),
2. the API routes,
3. the static frontend at `/` — mounted last, so API routes win.

The frontend has six pages: Dashboard, Task Viewer, Test LLM, Test MCP, Agent
Request, Settings.

The boot order is load-bearing:

```html
<script src="/api/settings/assets/aiset.js"></script>
<script src="/api/settings/assets/aiset-tree.js"></script>
<script src="app.js"></script>
<script src="task_steps_viewer.js"></script>
```

The asset URLs are under the **API** prefix on purpose: nginx forwards `/api/`, `/ws/`
and `/assets/`, and the SPA fallback answers any other path with
`200 text/html` — which the browser can only report as a missing global. The
scripts must be classic (not `type="module"`), because a module script is deferred
and would race with `app.js`.

`initSettingsBackend()` is called from `onLoggedIn()` and from `showPage('settings')`,
**not** at script load: before authentication, `/api/settings-info` answers 401 and
nothing would retry it.

### 6.10 Audit log

`core/audit_log.py`. An append-only journal plus a disposable SQLite index.

| Event type | Journal |
|---|---|
| `client_request` | `raw_client_requests-YYYY-MM-DD.jsonl` |
| `client_response` | `raw_client_responses-YYYY-MM-DD.jsonl` |
| `llm_request` | `raw_llm_requests-YYYY-MM-DD.jsonl` |
| `llm_response` | `raw_llm_responses-YYYY-MM-DD.jsonl` |
| `rejected_request` | `rejected_requests-YYYY-MM-DD.jsonl` |
| `task` (terminal) | `tasks-YYYY-MM-DD.jsonl` |

Layout under `logs/audit/`:

```
audit-index.sqlite          # disposable, rebuildable
raw_*-YYYY-MM-DD.jsonl      # the journals
files/YYYY-MM-DD/<uuid>     # file-backed bodies and data-URI attachments
.spool/<uuid>               # in-flight stream spools (transient)
```

A body is inline when it is JSON or small UTF-8 text and its size is below
`logging.audit.max_inline_body_bytes` (default 1 MiB). Binary media types and
oversized bodies are file-backed and referenced by an opaque id.

The index rows are written **after** the journal append, so the index never points at
bytes that were not written. The index itself is disposable: `rebuild_index()` can
reconstruct it from the journals.

The writer runs on its own thread with a bounded queue. Under the default
`best_effort` overflow policy a full queue **drops** the event and records it in the
health snapshot; under `durable` the caller blocks until the write completes.

**SSE normalization** is audit-only: a completed, error-free OpenAI SSE response is
reassembled into one final JSON completion with `body_format: "sse"` and
`sse_chunk_count`. A stream that cannot be unambiguously assembled, or that is
failed or error-bearing, stays one raw string. Client delivery is never affected.

### 6.11 Logger

`core/logger.py`. A line is `<iso-ts> [LEVEL] [type:tag] message`.

Levels follow the journald scale `0 EMERG … 7 DEBUG`; `WARNING`→4, `TRACE`→7, and an
unknown name falls back to `INFO`.

Each message may go to two files: `logs/<type>.log` and `logs/all.log`. The
threshold for each is resolved separately:

| File | Resolution order |
|---|---|
| `logs/<type>.log` | `workers.items.<tag>.logging.level` → `middleware.items.<tag>.logging.level` → `endpoints[].logging.level` → `logging.levels.<type>` → `logging.level` |
| `logs/all.log` | `workers.items.<tag>.logging.alllevel` → `endpoints[].logging.alllevel` → `logging.alllevels.<type>` → `logging.level` |

Actual logger types are `system`, `core`, `worker`, `webui`, `audit`, `http`,
`hooks`. **Note:** the shipped example config uses `logging.levels.workers` (plural)
while the logger type is `worker` (singular) — those two keys never take effect.
See [§14](#14-known-problems-open-tasks-and-development-directions).

### 6.12 Cron

`core/cron.py`. Two modes: a system crontab entry, or an embedded in-process
scheduler (`cron.embedded: true`) for hosts without cron.

The cycle runs nine jobs, each isolated so one failure never stops the cycle:

| Job | What it does |
|---|---|
| `run_loop_workers_cycle` | Calls `loop()` on every worker that exposes it, rotating the start index |
| `refresh_external_mcp_tools` | Re-reads the remote MCP tool list |
| `wipe_logs` | Removes log lines older than `wipe_max_age` |
| `trim_logs_by_size` | Trims a log file to `max_log_size`, **in place** so the inode survives |
| `retain_audit_logs` | Reconciles interrupted partitions, then applies the audit retention |
| `health_check` | `redis.ping()` |
| `cleanup_stale_tasks` | **Empty stub** — needs a worker heartbeat mechanism |
| `cleanup_expired_tasks` | Removes terminal external task hashes older than `external_task_live` |
| `keep_alive_ping` | Pings Ollama models / restarts managed llama.cpp servers that are still wanted |

The keep-alive ping measures the window from the **last real use**, not from the last
ping, so a ping cannot keep a model warm forever.

### 6.13 Doctor

`core/doctor.py`, `./doctor.sh`. One command diagnoses the configuration documents
(through the `aiset` doctor) and the host, and repairs what can be repaired
mechanically. It runs **even when aidir refuses to start**, which is the case it
exists for — which is also why `core/app.py` dispatches to it before importing
`core.config`.

Fifteen checks, in execution order: `aiset`, `config`, `env-file`, `env-root`,
`dirs`, `store-lock`, `venv`, `docker`, `compose`, `redis`, `nginx`, `ports`,
`logs`, `ollama`, `systemd`.

Fix classes:

| Class | Requires | Example |
|---|---|---|
| `SAFE` | `--fix` or `--fix-all` | Create a missing directory, install the venv dependencies |
| `VISIBLE` | `--fix-all` | Recreate a container |
| `LOSSY` | `--fix-all` | Drop a key the descriptor does not declare |
| `PRIVILEGED` | `--root` | Start the Docker daemon, `chown` the Redis volume |

Configuration-side repair classes are `clamp` (safe, `--fix`), `coerce` and
`drop_unknown` (`--fix-all`). A repaired document is written **only** when the
repairs make it fully valid; a partial repair is reported, not stored.

Redis is diagnosed per layer, because each layer has a different fix: the container
state, the port mapping, the answer from inside the container, and the identity of
the host process that owns the port.

Exit codes: `0` healthy or warnings only, `1` failures remain, `2` repaired but
issues remain.

### 6.14 Hooks

`core/hooks.py`, `hooks/`. Administrator-controlled Python modules, discovered from
`hooks/<id>.py` or `hooks/<id>/app.py`. A hook is **disabled unless** the module
sets a module-level `ENABLED = True`; the flag is read with `ast`, so a disabled
hook is never imported.

Four events:

| Event | Signature | When |
|---|---|---|
| `task_created` | `(task)` | A task enters the queue |
| `before_llm_request` | `(task, request)` | Before the upstream call (agent tasks only) |
| `llm_response_complete` | `(task, response)` | After the upstream call |
| `before_consumer_delivery` | `(task, response)` | Before the answer reaches the client |

Handlers are coroutines, run serially in registration order; an exception is logged
and **never changes the task outcome**.

The control API is deliberately narrow: `restart_local_llama_cpp(provider_id)`,
`is_owned_local_llama_cpp(provider_id)`, `retry_task(task, provider_id=None)`,
`cancel_task(task, reason)`. A hook cannot fabricate a successful response.

### 6.15 LocalServerManager

`core/local_server_manager.py`. Manages the `llama-server` processes aidir owns.

- A provider with a healthy `baseUrl` is **external**: aidir never adopts or stops
  it.
- Otherwise, `exec_cmd` is started with `shlex.split`, the PID and the process start
  ticks are recorded in `logs/llama_cpp_servers.json`, and `/health` is polled every
  0.5 s until the server answers (timeout = `provider.startup_timeout`, default 120 s).
- The start ticks (`/proc/<pid>/stat` field 19) are the **proof of ownership**: a
  recycled PID is never acted upon.
- Stopping signals the whole process group (`start_new_session=True` makes the child
  a group leader).
- On restart, healthy inherited servers are **adopted** and re-registered as
  persistent VRAM consumers; unhealthy ones are stopped.

### 6.16 Messages protocol module

`core/messages_protocol.py`. Pure functions, no I/O. Both the endpoint and the
workers import it, so the conversion is written once.

| Function | Direction |
|---|---|
| `messages_request_to_internal(body, default_max_tokens)` | Anthropic → internal |
| `internal_request_to_messages(payload, default_max_tokens)` | internal → Anthropic |
| `messages_response_to_internal(body)` | Anthropic response → internal result |
| `internal_result_to_messages(result, model, task_id, include_thinking)` | internal result → Anthropic response |
| `estimate_input_tokens(body)` | local token estimate |
| `messages_error_type_for_status(status)` / `messages_error_type_for_code(code)` | error type mapping |
| `AnthropicStreamDecoder` | Anthropic SSE → internal deltas |
| `AnthropicStreamEncoder` | internal deltas → Anthropic SSE |

Thinking blocks keep their `signature` while the corresponding assistant turn is
unchanged; a context rewrite drops them with a warning. The synthesized stream
encoder never emits `signature_delta`, and a non-streaming result carries an empty
thinking signature.

### 6.17 Upstream response and error transparency

`core/upstream_response.py`, `core/upstream_errors.py`.

```python
UpstreamResponse = {protocol, status_code, content_type, body_base64}
UpstreamChunk    = dict + {protocol, raw, content_type, original}
```

`original_response(response, protocol)` and `original_chunk(chunk, protocol)` return
the original bytes **only when the protocol matches the client's**. That single
condition is what makes the transparency rules work: an OpenAIx client talking to an
OpenAI upstream gets the upstream's bytes; an Ollama client talking to an Anthropic
upstream gets a conversion.

`build_upstream_error(status, body, content_type)` captures the upstream status,
body and content type; `upstream_error_response(error)` rebuilds the original HTTP
response; `upstream_error_payload(error)` is the streaming variant.

---

## 7. Implementation decisions and their rationale

This section explains **why** the system is the way it is, with the trade-offs.

### 7.1 Protocol transparency and native passthrough

**Decision.** When the client's protocol equals the executor's protocol, the client
receives the executor's original body, content type and stream events. aidir
converts only when it must.

**Why.** An orchestrator that re-serializes every answer loses fields it does not
know about: reasoning details, cache-control markers, vendor extensions, unknown
`choices` fields, the exact finish reason, the exact usage breakdown. Clients that
depend on them break. The requirement "reasoning is not substituted for an empty
answer" is the same principle in miniature.

**Cost.** Two code paths everywhere. The endpoint must know which protocol the
upstream used, and a native stream suppresses aidir's own terminator
(`data: [DONE]` is emitted only when the stream was *not* native). The Anthropic
endpoint has the same rule with `encoder.finish()`.

**Two documented exceptions.**

1. **Internal multistep tool execution.** Intermediate model and tool turns stay
   inside aidir; only the final answer is returned, with the reasoning fields
   removed after an internal tool actually executed. Usage and other final-response
   fields survive. A stream may be synthesized from the final synchronous response.
2. **Different protocols.** aidir converts the envelope as closely as possible:
   Ollama `thinking` becomes OpenAI `reasoning_content` and back, compatible extra
   fields survive, but byte-for-byte identity is impossible.

**Caller-owned tool definitions do not trigger the internal loop** and keep native
streaming. Only the tools aidir itself injected are executable.

### 7.2 Two-phase streaming

**Decision.** A streaming endpoint waits for the **first chunk** before opening the
response.

**Why.** Otherwise a failure that happens before any output would be delivered as
`200 OK` with an error object inside the stream, and the client could not tell a
failure from a slow start. Waiting for the first chunk keeps the HTTP status
meaningful.

**Cost.** The client sees no bytes until the upstream produces its first token, and
the timeout accounting differs between the "wait for the first chunk" phase and the
"stream" phase. The endpoint-side timeout is phase-aware:
`BaseEndpoint._task_timeout_phase` returns `"queue"` or `"run"` and the remaining
budget for that phase.

### 7.3 Redis as the only durable state

**Decision.** The queue, the task hashes, the WebUI sessions, the envid registry,
the resource activity records and the cron bookkeeping all live in Redis. There is
no local-only degraded mode.

**Why.** Multiple aidir instances on one machine must not share a queue, and a
restart must not lose queued work. A ZSET with the priority as the score gives
atomic priority popping for free (`ZPOPMIN`), and a HASH beside it gives a durable
task record without a second store.

**Cost.** Redis is a hard dependency: `Core.start` raises if the ping fails. The
in-memory `QueueManager._tasks` dict is a cache, not a queue. The queue-state
aggregations are `O(queued tasks)` with one `HGETALL` per task, and the dashboard's
`/api/tasks` takes about four seconds above 12000 stored task hashes (which is why
the browser refreshes every three seconds and carries an in-flight guard).

### 7.4 Fail-closed resource availability

**Decision.** When a resource's free capacity is measured by a command or by a peer,
a failed measurement makes the resource **unavailable**. Dispatch is blocked.

**Why.** Guessing "probably enough" is how an orchestrator OOMs a GPU. The
alternative — treating a failed probe as "assume it fits" — silently corrupts the
one guarantee the resource system exists to provide.

**Cost.** An unreachable probe stalls the resource. This is mitigated by refreshing
only the resources a task actually needs, immediately before dispatch, and by
restoring the resource automatically after a successful probe.

**Measurement generations.** Each reservation records the availability generation at
which it was made; the observed free capacity subtracts only the reservations made
*after* the observation. This avoids double-counting old reservations while still
discounting new ones.

**Fail-open for reuse.** A resource without a command or peer probe uses the
*estimated* capacity (`limit - used - soft_used`). That is a different rule on
purpose: there is nothing to fail.

### 7.5 Base footprint versus session cost

**Decision.** A model's `resources` is the one-time footprint (weights and base
buffers) and `sessionResources` is the incremental cost of one in-flight request.
The scheduler reserves the base once and one session per active request.

**Why.** One model serving four concurrent requests needs four times the KV cache
but not four times the weights. A single number cannot express that, and the naive
reading — reserve the whole footprint per request — makes parallel admission
impossible.

**Cost.** `resources` changes meaning depending on `maxConcurrentRequests`, and
`sessionResources` must be configured correctly. The system refuses to boot when
`maxConcurrentRequests > 1` and a metric has no `sessionResources` entry
(`Resources.validate_model_parallel_config`).

The admitted count is the smaller of `maxConcurrentRequests` and the number of
sessions that fit in free memory.

### 7.6 KV affinity as a heuristic

**Decision.** The fingerprint is a canonical, append-stable line stream; the anchor
is an adaptive fraction quantized onto a ladder; the hash is an HMAC of the prefix.

**Why.** A false positive merely sends a request to the executor it would have gone
to anyway — harmless. A false negative loses the cache and costs latency. The
canonicalization is therefore tuned to avoid false negatives: the line-per-message
layout means a growing conversation appends instead of rewriting, and the anchor
fractions are generous.

**Cost.** The store is in-process, so a restart loses the mapping (queued tasks keep
their already-resolved route, so only new requests are affected). The heuristic
cannot distinguish two conversations that share a long prefix, and the 20-message
trim in the `openaix` worker breaks conversational prefix reuse in practice — the
executor may only ever see a trimmed history.

**Advisory, not a guarantee.** A record may outlive the provider's actual cache, so
the TTL is generous. Affinity is dropped on a known unload and on an upstream
failure, but those are the only hard signals available.

### 7.7 Source-preserving configuration

**Decision.** Configuration is JSON5 and every write preserves comments, quoting,
trailing commas, line endings and the exact bytes outside the edited region.

**Why.** A configuration file that a tool rewrites into a different shape is a
configuration file people stop editing by hand. The whole point of a hand-written
config is the comments.

**How.** The `aiset` library parses the file into a source-preserving template,
applies the structural operations to an in-memory candidate, and commits the whole
file under a lock.

### 7.8 The descriptor is a hard contract

**Decision.** An undeclared key is an **error**, not a warning. The document becomes
`invalid_on_disk`, the effective tree is empty, every write is refused, and the
process stops at startup with a precise diagnostic listing **every** offending path.

**Why.** The alternative — ignoring unknown keys — turns every typo into a silent
no-op and every removed feature into a lingering key that hides the next typo. The
cost is a hard failure mode ("all or nothing"), which is why the coverage gate
(`test_config_descriptor.py`) exists and why `doctor` can diagnose exactly this.

**The rule for contributors.** Adding a key to a configuration file **without**
adding it to the descriptor fails the build. The converse also holds: a key that no
code reads should be **deleted**, not declared.

### 7.9 The generated editor, the host-owned design

**Decision.** The library generates the editor's markup and ships light-theme
defaults. This project owns the visual design in `webui/frontend/settings.css`.

**Why.** A generated interface cannot guess the design of the host. The library
emits 47 `aiset-tree-*` / `aiset-form-*` classes and expresses its palette through
`--aiset-*` variables; the host maps those variables onto the project palette and
styles the controls the library names but does not paint.

**Load order is load-bearing.** The library sheet must load first: equal
specificity is decided by order. `test_settings_styles.py` fails if the order is
reversed, if a class the editor emits is left undesigned, if a host rule escapes the
editor scope, or if a host control carries an inline style (an inline declaration
outranks any stylesheet rule).

### 7.10 The assistant stages, the human applies

**Decision.** The configuration assistant's principal holds only
`settings_read_source`, `settings_read_effective` and `settings_validate`. It
physically cannot read a secret and cannot write.

**Why.** An assistant that can write is an assistant that can break the system. The
assistant proposes **operations**, the browser stages them in the tree, and the
change reaches disk only when a human presses **Apply**. The staged batch is then
validated against the descriptor and committed atomically.

**Cost.** The assistant is not a chat that "fixes" things; it is a source of
reviewable diffs. The loop is bounded (`max_tool_rounds`, default 8), the history is
bounded by both turn count and a character budget, and a provider failure is never
turned into an answer.

### 7.11 Append-only observability

**Decision.** The audit journals are immutable append-only files; the SQLite index
is disposable and rebuildable.

**Why.** An audit trail that can be rewritten is not an audit trail. Making the
index disposable means the index can be rebuilt, moved or lost without losing
evidence.

**Cost.** Lookups need the index; a lost index costs a full rescan. The writer
thread can drop events under the default `best_effort` overflow policy — the drop
is counted in the health snapshot and logged, but the caller does not see it.

### 7.12 Defer, never block

**Decision.** A task that cannot start now is re-queued with `next_retry_at = now + 1s`
(or `+5s` when a force-unload did not free enough). The scheduler never holds a
worker slot while waiting.

**Why.** Blocking would pin an executor, a resource reservation or a concurrency
slot on a task that may never run — and it would make the "drain the whole queue"
property impossible.

**Cost.** The scheduler's deferral loop is a busy poll: each pass re-probes
availability and re-resolves the smart route, so a blocked task costs one remote
probe per candidate per second. The upside is that the same re-evaluation is what
feeds the affinity readiness history.

### 7.13 Wire compatibility with several protocols at once

**Decision.** One process serves the Ollama, OpenAI, OpenAIx, Anthropic Messages and
MCP surfaces, and the same configuration describes all of them.

**Why.** A client ecosystem is fragmented; an orchestrator that picks one protocol
forces every client into a translation. Converting centrally, once, with a pure
module that both the endpoint and the worker import, is cheaper than making every
client translate.

**Cost.** The Anthropic endpoint inherits the whole OpenAI/Ollama machinery
(smart routing, peer APIs, generation defaults) even though it serves four routes,
and the MCP endpoint re-implements the audit helpers because it sits outside the
hierarchy.

---

## 8. File system layout

### 8.1 Repository root

| Path | Contents |
|---|---|
| `core/` | The application (see [§8.2](#82-core)) |
| `workers/` | The worker plugins (see [§8.4](#84-workers)) |
| `webui/` | The WebUI backend and frontend (see [§8.5](#85-webui)) |
| `hooks/` | Optional lifecycle hooks |
| `nginx/` | The nginx image, template and entrypoint |
| `docker/` | Compose volumes (the Redis data directory) |
| `memory/` | `context.md`, `context_archive.md`, `history.md` — the agent working memory (git-ignored) |
| `logs/` | Runtime logs, audit journals, the llama.cpp PID record (git-ignored) |
| `aiset/` | The settings library, a **Git submodule** pinned to one commit |
| `spec/` | The satellite documents of this specification |
| `config.json5` | The active configuration (git-ignored) |
| `config.json5.example` | The shipped configuration template |
| `config.descriptor.json5` | The schema of `config.json5` |
| `config.types.json5` | The reusable configuration shapes shared by all descriptors |
| `install.sh` / `uninstall.sh` / `install_prerequisites.sh` | Installation |
| `doctor.sh` | A wrapper around `core/app.py doctor` |
| `docker-compose.yml` | Redis and nginx |
| `requirements.txt` | Pinned runtime dependencies |
| `test_*.py`, `test_*.js` | The test suite |
| `smoke_test.py`, `search_check.py` | Live helpers (not part of the test protocol) |
| `proposal_*.md`, `plan_*.md`, `spec_*.md` | Design history; see [DOCUMENTS.MD](DOCUMENTS.MD) |

### 8.2 `core/`

| File | Responsibility |
|---|---|
| `app.py` | Process entry point, the `Core` orchestrator, endpoint and WebUI server construction, signal handling |
| `endpoint.py` | `BaseEndpoint` and the shared request/audit/timeout helpers |
| `endpoints/endpoint_ollama.py` | The base endpoint: auth, smart router wiring, route traces, probes, streaming |
| `endpoints/endpoint_openaix.py` | The wide surface: models, queue-state, peer APIs, embeddings, OpenAI conversion |
| `endpoints/endpoint_anthropic.py` | The Messages surface |
| `endpoints/endpoint_mcp.py` | JSON-RPC over HTTP, the tool registry |
| `queue_manager.py` | The Redis priority queue, task storage, recovery, queue-state aggregation |
| `scheduler.py` | The dispatch loop, admission, retry/fallback policy, smart-route refresh |
| `smart_router.py` | Candidate evaluation, ranking, affinity policy, remote probes |
| `kv_affinity.py` | The fingerprint, the anchor policy, the bounded store, the readiness tracker |
| `resources.py` | The resource registry, reservations, admission, unload |
| `resource.py` | One resource: capacity, soft consumers, availability accounting |
| `resource_monitor.py` | The polling loop, sensors, thresholds, availability commands, peer refresh |
| `peer_resources.py` | The peer probe client and its capability cache |
| `local_server_manager.py` | The managed `llama-server` lifecycle |
| `worker.py` | `BaseWorker`, `BaseToolWorker`, `WorkerResult`, the LLM-call diagnostics |
| `workers_loader.py` | Worker discovery, import, the worker-local configuration merge |
| `task.py` | The `Task` dataclass, the states, the Redis serialization |
| `task_types/` | `Task_agent`, `Task_tool` |
| `context.py` | The `Context` dataclass |
| `envid.py` | The `Envid` dataclass and the `EnvidRegistry` |
| `config.py` | The process-wide `config` object |
| `config_merger.py` | `update_config` and `merge_contexts_top_level` |
| `settings/` | The `aiset` integration (see [§8.3](#83-coresettings)) |
| `messages_protocol.py` | The Anthropic ⇄ internal conversions and the stream codecs |
| `upstream_response.py` | The upstream response/chunk capture and the byte-preserving readers |
| `upstream_errors.py` | The upstream error capture and the original-response rebuild |
| `request_limits.py` | The request body size limit middleware |
| `generation_options.py` | The generation parameter name mappings |
| `opencode_session.py` | The encrypted OpenCode session store |
| `hooks.py` | The hook manager, the events, the control API |
| `audit_log.py` | The audit journals, the index, the retention, the SSE assembler |
| `logger.py` | The log writer and the threshold resolution |
| `error_logging.py` | The request id, the request-id middleware, the exception logging |
| `call_log.py` | The compact per-worker LLM call log |
| `cron.py` | The maintenance cycle |
| `doctor.py` | The host diagnostics |
| `__init__.py` | `log()` — the process-wide logging shortcut |

### 8.3 `core/settings/`

| File | Responsibility |
|---|---|
| `aiset_path.py` | Make `aiset/src` importable; two distinct failure messages |
| `runtime.py` | The single `Service`, the document table, the effective cache, the callbacks |
| `documents.py` | Worker-local document discovery and the root-wins merge |
| `facade.py` | `AisetConfig` — the `config` object |
| `wsgi.py` | The ASGI adapter, the permission sets, the session → principal mapping |
| `agent.py` | The configuration assistant |

### 8.4 `workers/`

```
workers/<type>/<id>/app.py          the worker module (must expose `worker`)
workers/<type>/<id>/config.json5    the worker-local configuration (optional)
workers/<type>/<id>/descriptor.json5 the worker-local schema (optional)
```

A worker directory becomes a configuration document **only if it carries both**
`config.json5` and `descriptor.json5`. Six workers currently do:
`call_ollama`, `openaix`, `context_builder`, `http_api`, `web_fetch`, `web_search`.

`__init__.py` files are optional: the loader uses `importlib.import_module`, and
Python 3 namespace packages make the directory importable without them.

### 8.5 `webui/`

| Path | Responsibility |
|---|---|
| `backend/app.py` | The WebUI FastAPI app: auth, tasks, status, logs, resources, test routes, the settings mount |
| `frontend/index.html` | The single page with all six views and the script/style load order |
| `frontend/app.js` | The front-end logic: boot, polling, the task viewer, the settings page, the assistant |
| `frontend/settings.css` | The host-owned design of the generated settings editor |
| `frontend/task_steps_viewer.js` | The JSON tree renderer, the task detail view, the call-history table |

### 8.6 `logs/`

```
logs/all.log, system.log, core.log, worker.log, webui.log, audit.log, http.log, hooks.log
logs/cron.log
logs/<worker_id>_call_log.jsonl
logs/llama_cpp_servers.json          the managed llama-server PID records
logs/local_llama_cpp.log              the managed server's own output
logs/audit/                          the audit journals, the index, the body files
```

---

## 9. Configuration

### 9.1 Files and precedence

| File | Purpose | Tracked |
|---|---|---|
| `.env` | Secrets and machine-specific values (`${VAR}` substitution source) | No (`chmod 600`) |
| `config.json5` | The active configuration | No |
| `config.json5.example` | The shipped template | Yes |
| `config.descriptor.json5` | The schema of `config.json5` | Yes |
| `config.types.json5` | The reusable shapes | Yes |
| `workers/<type>/<id>/config.json5` | A worker-local configuration | Yes (the six existing ones) |
| `workers/<type>/<id>/descriptor.json5` | A worker-local schema | Yes |

Substitution order for `${VAR}`: host overrides → process environment → the `.env`
mapping → the inline `${VAR:-default}` → the descriptor default. POSIX `:-` only,
single pass; a resolved value is data, never structure.

`.env` is parsed by aidir itself and injected as **data**, so the process environment
still wins — the behaviour of the old `load_dotenv(override=False)`.

### 9.2 Top-level sections of `config.json5`

| Section | Purpose |
|---|---|
| `version` | The configuration format version (`const: "1.0"`, read-only) |
| `title` | The instance title shown in the WebUI |
| `instance` | The instance id; the Redis key prefix |
| `root` | The absolute installation path |
| `routing` | `max_hops` and the `kv_affinity` block |
| `redis` | The Redis connection |
| `http` | `max_request_size` (the inbound body limit) |
| `cron` | `cron.embedded` |
| `logging` | Levels, rotation, retention, per-subsystem maps, the audit journal settings |
| `resources` | The resource pools |
| `endpoints` | The listeners |
| `workers` | `tools_max_turns`, `default`, `items.<id>` |
| `middleware` | The middleware registry (declared; the chain is not executed — see §14) |
| `models` | `providers.<id>` — the model catalogue |
| `webui` | The listener, the authentication, the backend request timeouts |
| `users` | The API accounts (login, token, envids) — **not** the WebUI accounts |
| `tasks` | The lifecycle timeouts |
| `envids` | The named execution environments |
| `ai` | The configuration assistant |

### 9.3 Descriptor format

A descriptor names every legal key and describes it:

| Property | Meaning |
|---|---|
| `type` | `string`, `int`, `number`, `bool`, `null`, `object`, `array`, `union`, `any`, plus the `log_level` shape |
| constraints | `minimum`, `maximum`, `min_length`, `pattern`, `enum`, `const`, `min_items`, `nullable`, `required`, `key_pattern` |
| `default` | The effective-view value; never written back |
| `secret: true` | The subtree is denied without `settings_read_secret`; writes need `settings_edit_secret` |
| `read_only: true` | Every write below it is refused |
| `apply` | `live`, `reload` or `restart` (inherited from the nearest ancestor) |
| `env: {allowed, convert}` | Whether `${VAR}` is permitted on the node and how it converts |
| `description` | The help text; also the editor's node description |
| `ui: {widget, placeholder, help, group}` | Editor hints |
| `children` / `additional_children` / `children_options` | The object members, the dynamic member type, the union alternatives |
| `typedef` | A reference to a shape in `config.types.json5` |

`config.types.json5` declares 20 reusable shapes: `log_level`, `worker_tool`,
`worker_logging`, `http_operation`, `http_connector`, `mcp_server`, `search_provider`,
`provider_ref`, `env_context_workspace`, and the worker families `worker_entry`,
`worker_agent`, `worker_llm`, `worker_search`, `worker_http_api`,
`worker_mcp_client`, `worker_context`, `worker_reject`, `worker_plain`.

**A known compromise:** `workers.items.<id>` is a single flat `worker_entry` object
rather than a union of the eight families. The editor showed eight identical
"object" choices and there was no way to tell them apart. The cost is that a field
read by one family validates in any worker; the per-worker local descriptor is the
only place the exact family is enforced.

### 9.4 Apply policies

| Policy | Meaning |
|---|---|
| `live` | The next read already sees the new value |
| `reload` | A SIGHUP or the WebUI **Reload settings** action re-reads and re-applies it |
| `restart` | Only a process restart picks it up |

A node with no `apply` asks for `restart`. The most restrictive policy among the
changed paths wins and becomes the reported `application_state`: `applied`,
`reload_required`, `restart_required` or `apply_failed`.

Examples:

| Change | Policy |
|---|---|
| `logging.*` (levels, periods, maps) | `live` |
| `endpoints[].logging`, `middleware.items[].logging` | `live` |
| `resources[].availability`, `resources[].monitoring` | `live` |
| `title`, `instance`, `routing.*`, `cron.embedded` | `reload` |
| `logging.audit.*`, `workers.tools_max_turns` | `reload` |
| `endpoints[].worker`, `models.providers.*.models[]` | `reload` |
| `webui.request_timeouts.*`, `users.items[]`, `tasks.*`, `envids.*` | `reload` |
| `root`, `redis.*`, `http.max_request_size` | `restart` |
| `endpoints[]` (id, api, port, bindAddress) | `restart` |
| `workers.*`, `middleware.*`, `models.providers.*`, `webui.port/bind/auth` | `restart` |
| `resources[]`, `ai.*` | `restart` |

**Important limitation:** `Core.reload_config` only refreshes the settings cache.
Workers, endpoints and the resource model are **not** rebuilt, so a `reload` change
to `workers.*` or `models.providers.*` effectively needs a restart in practice. This
is a known gap — see [§14](#14-known-problems-open-tasks-and-development-directions).

### 9.5 Worker-local configuration

A worker-local `config.json5` is merged **under** `workers.items.<id>`, and the
**root entry wins** on every key present in both:

```python
merge(root_entry, local_entry, prefer="base")   # base = root, wins
```

The merge uses the library's own `aiset.merge`, not a private deep merge, so the
behaviour cannot drift from the library's.

---

## 10. Redis schema

Namespace = `config.instance` (default `aidir1`).

| Key | Type | Content | TTL |
|---|---|---|---|
| `{ns}:queue:agent` | ZSET | member = task id, score = priority | — |
| `{ns}:queue:tool` | ZSET | the same | — |
| `{ns}:task:{task_id}` | HASH | The full task; all values are strings | none |
| `{ns}:queue:quarantine` | HASH | task id → `{task_type, reason}` | — |
| `{ns}:envids` | SET | The envid ids | — |
| `{ns}:envid:{id}` | HASH | The envid record | — |
| `{ns}:ctx:{id}` | STRING | The saved conversation context (JSON) | **none** |
| `{ns}:session:{token}` | STRING | The WebUI session (JSON) | `webui.auth.session_ttl` |
| `{ns}:resource:{rid}:activity:{provider}:{model}` | STRING | The idle-model activity record | `max(keep_alive, alive_time, 3600)` |
| `{ns}:opencode-caller-session:v1:{task_id}` | STRING | An encrypted caller session | `SET ... EX` |
| `{ns}:openai-session:v1:{provider}:{model}:{digest}` | STRING | An encrypted managed session | `SET ... EX` |
| `{ns}:worker:external_mcp:startup_time` | STRING | The MCP tool-cache generation marker | — |
| `{ns}:cron:{job}:last_run` | STRING | The per-job interval gate | — |
| `{ns}:cron:loop_workers:start` | STRING | The `loop()` rotation cursor | — |
| `{ns}:cron:last_success_at` | STRING | The cycle heartbeat | — |

Two gaps worth knowing: `{ns}:ctx:*` never expires and no cron job prunes it (the
declared `envids.check_period_minutes` and `envids.store_duration_minutes` are read
by nobody), and task hashes never expire either — external ones are reaped by
`cleanup_expired_tasks`, internal ones are deleted when the task is.

---

## 11. Timeout semantics

Four independent clocks. The normative document is [spec_timeouts.md](spec_timeouts.md).

| Timeout | Scope | Default | On expiry |
|---|---|---|---|
| `REQUEST_TIMEOUT` | **one** outbound HTTP call to an upstream | `100` s (`.env`) | The worker returns `UPSTREAM_TIMEOUT` |
| `TASK_QUEUE_TIMEOUT_SECONDS` | The wait **before the first transition to `running`** | `300` s | The task fails with `QUEUE_TIMEOUT`; HTTP 504 |
| `TASK_RUN_TIMEOUT_SECONDS` | The execution **after `running`**, including tool loops | `300` s | The coroutine is cancelled; `TIMEOUT`; HTTP 504 |
| `TASK_RESTART_WAIT_TIMEOUT_SECONDS` | The graceful-restart drain | `120` s | The running tasks are cancelled and the shutdown continues |

The critical rule: **if a task reaches `running` just before the queue timeout
expires, the queue timeout no longer applies to that task.** From that point only
the run timeout can stop it. A task may be re-queued for resource shortage while
still in the queue phase, and the queue-timeout accounting stays active.

`endpoints[].request_timeout` is **deprecated and ignored**. It is still declared in
the descriptor so that older local configurations keep loading, and a warning is
logged when it is found.

---

## 12. Security model

### 12.1 Two authorization planes

| Plane | Where | Mechanism |
|---|---|---|
| **API** (endpoints) | `config.users.items[]` | A bearer token (`Authorization: Bearer` or `x-api-key`), compared with `hmac.compare_digest`; optional `envid` scoping |
| **WebUI** | `config.webui.auth.users[]` | A login and a plaintext password, a Redis session, an `httponly` `samesite=strict` cookie |

The two are independent: `users.items` are API accounts, `webui.auth.users` are
browser accounts. The WebUI never reads `users.items`, and the endpoints never read
`webui.auth.users`.

### 12.2 Properties

- **API authentication is optional.** A request with no token proceeds
  **anonymously**. A token that *is* presented is validated strictly, and an
  `envid` it names must be in the account's `envids` list and must exist.
- **The settings API is permission-based.** A session maps to an `aiset` principal
  with a permission set. A secret subtree is denied as a whole to a principal
  without `settings_read_secret` — it is not redacted field by field.
- **The configuration assistant cannot read a secret and cannot write**
  ([§7.10](#710-the-assistant-stages-the-human-applies)).
- **Secrets stay in the file.** A credential keeps its `${VAR}` placeholder in the
  file and is resolved only for a principal that holds the secret permission.
- **Audit bodies are not scrubbed.** The metadata envelope is scrubbed (the built-in
  key set is `authorization`, `cookie`, `set-cookie`, `token`, `api_key`, `api-key`,
  `x-api-key`, `x-opencode-session`, `password`, `secret`, extendable through
  `logging.audit.secret_metadata_keys`), but a secret **inside a body** stays in the
  body.
- **Passwords are stored in plaintext** in `config.json5`. A hashed-password scheme
  is a recorded wish, not an implemented feature.

### 12.3 Gaps

- No CSRF token anywhere; the write surface relies on `samesite=strict`.
- The MCP endpoint has no authentication.
- The peer resource and peer task endpoints default to `auth_mode: disabled`.
- The OpenAIx model lists and the queue-state endpoints are unauthenticated.
- The queue-state endpoints are unauthenticated **and** `O(queued tasks)`.

---

## 13. Testing

The protocol is in [TEST.md](TEST.md). The canonical command:

```bash
source venv/bin/activate
python -m unittest discover -s . -p 'test_*.py' -v
```

44 test modules, 117 test classes, 586 test methods. The suite is standard-library
`unittest` — there is no pytest configuration in this repository (the `aiset`
submodule has its own pytest suite, which is **not** run by the host's command).

Coverage by area:

| Area | Modules |
|---|---|
| OpenAIx protocol | `test_openaix_tool_args_parser.py` (89 methods), `test_response_transparency.py`, `test_openaix_embeddings.py`, `test_openaix_queue_timeout.py`, `test_openaix_queue_state.py`, `test_peer_resources.py`, `test_request_limits.py` |
| Anthropic Messages | `test_anthropic_protocol.py`, `test_anthropic_endpoint.py`, `test_anthropic_worker.py`, `test_anthropic_routing.py` |
| Providers | `test_llama_cpp.py` |
| Scheduler and lifecycle | `test_scheduler_queue_bypass.py`, `test_scheduler_timeout_diagnostics.py`, `test_queue_restart_recovery.py`, `test_shutdown_lifecycle.py`, `test_task_llm_call_count.py`, `test_task_priority_defaults.py`, `test_external_task_retention.py` |
| Resources and routing | `test_kv_affinity.py`, `test_kv_affinity_routing.py`, `test_resource_reuse.py`, `test_resource_monitor.py`, `test_session_resources.py` |
| Settings | `test_config_descriptor.py`, `test_settings_backend.py`, `test_settings_parity.py`, `test_settings_agent.py`, `test_settings_styles.py`, `test_aiset_path.py` |
| Front end | `test_frontend_boot_order.py` + `test_frontend_boot_probe.js` (Node), `test_aiset_class_inventory.js` (Node) |
| Observability | `test_audit_log.py` (27 methods), `test_endpoint_audit.py`, `test_cron_logging.py`, `test_cron_repair.py`, `test_viewer_search.py`, `test_webui_live_logs.py` |
| Tools | `test_web_search_fallback.py`, `test_http_api_worker.py` |
| Doctor | `test_doctor.py` (53 methods) |

Two test files are **CLI probes**, not unit tests, even though they match the
discovery glob: `test_context_overflow.py` and `test_timeout_semantics_live.py`.
`test_logs.py` performs real log wipe and trim operations at import time.

Two Node harnesses (`test_frontend_boot_probe.js`, `test_aiset_class_inventory.js`)
are skipped when Node is unavailable. They exist because HTTP tests cannot see
front-end load order: the requests are already authenticated, so an editor that
initialises too early still looks correct.

---

## 14. Known problems, open tasks and development directions

### 14.1 Confirmed defects

| # | Problem | Where |
|---|---|---|
| 1 | ~~`logging.levels.workers` (plural) never matches the logger type `worker`~~ **Fixed**: the keys are `worker`, and a startup diagnostic names every threshold whose key is not a known subsystem | `core/logger.py`; `core/app.py` |
| 2 | ~~`endpoints[].peer_resources` is read by the endpoint but **not declared** in the descriptor~~ **Fixed**: the block is declared and read | `config.descriptor.json5` |
| 3 | `envids.check_period_minutes` and `envids.store_duration_minutes` are declared and shipped but read by nobody; envid contexts in Redis never expire | `config.descriptor.json5:1437-1452` |
| 4 | `logging.logs` / `logging.log_overrides` is read by the cron job but not declared in the descriptor, so it cannot be set through the editor | `core/cron.py:60` |
| 5 | ~~`Core.reload_config` refreshes only the settings cache~~ **Fixed**: the reload re-initializes the workers, updates the resource registry in place and reports the structural sections | `core/app.py` |
| 6 | The README's WebUI route table still lists `/api/config`, `/api/config/raw` and `/api/config/fields`, which were removed | `README.md:646` |
| 7 | `proposal_hooks.md` states both bundled hooks ship disabled; `hooks/llama_cpp_empty_response_recovery.py` has `ENABLED = True` | `proposal_hooks.md:160-163` |
| 8 | `prod.json5.example`, referenced by the README, does not exist in the repository | `README.md:62-63` |
| 9 | `plan_kv_keeper.md` and `plan_aiset_integration.md` quote stale test counts (574 and 366; the actual number is 586) | those files |
| 10 | `proposal_kv_keeper.md` says "Proposed, not implemented" and `proposal_searxng.md` says "Status: proposal", although both are shipped | those files |

### 14.2 Design gaps

| # | Gap | Impact |
|---|---|---|
| 1 | Only the `first_available` routing strategy is implemented, although the descriptor permits `direct` | `direct` produces HTTP 400 at request time |
| 2 | The scheduler's smart router cannot probe OpenAI-family providers, so an `openai` / `opencode-go` / `anthropic` candidate is classified as `provider_down` in the pre-dispatch refresh, which forgets its affinity record | the most significant integration gap found |
| 3 | The KV affinity store is process-local; there is no Redis mirror and no distributed fingerprint forwarding | a restart loses the mapping; a peer that re-routes breaks the edge's affinity |
| 4 | The 20-message trim in the `openaix` worker breaks conversational prefix reuse | the executor may never see the hashed prefix |
| 5 | **Deferred** — the middleware registry is declared and the logger reads `middleware.items.<tag>.logging.level`, but no middleware chain is executed | the `middlewares` list in `endpoints[]` is inert; see [§14.4](#144-deferred-tasks) |
| 6 | The Anthropic stream encoder never emits `signature_delta`, and a non-streaming result carries an empty thinking signature | a client that requires a real signature does not get one |
| 7 | `count_tokens` is `chars/4`; images are not counted and tool schemas are counted as raw JSON | an estimate, documented as such |
| 8 | The MCP endpoint has no authentication | the published tools are callable by anyone who can reach the port |
| 9 | The queue-state endpoints are unauthenticated and `O(queued tasks)` with one `HGETALL` per task | a scalability and exposure problem on a busy instance |
| 10 | The audit writer can drop events under the default `best_effort` policy | the caller does not see the drop |
| 11 | `fallbacks`, `on_reject` and `next_retry_at` are not persisted in the task hash | a task that round-trips through Redis loses its fallback chain |
| 12 | The scheduler's deferral loop is a busy poll that re-probes every candidate per second per blocked task | CPU and network cost under contention |
| 13 | The admission lock is a single global `asyncio.Lock` | all reservation decisions serialize process-wide |
| 14 | `_select_worker` ignores the provider: a task pinned to a worker whose provider cannot serve the model still reaches that worker | the failure surfaces at the worker level |
| 15 | `cleanup_stale_tasks` is an empty stub | a task stuck in `running` with no live worker is never detected |
| 16 | The envid registry, the MCP endpoint and the context workers have no direct test coverage | the envid authorization rules are normative but untested |
| 17 | **Deferred** — `before_tools` / `context_compressor` is designed (`before_tools_worker.md`) but has **no implementation at all** | a planned extension; see [§14.4](#144-deferred-tasks) |
| 18 | The OpenCode Go release gates in `proposal_opencode_go.md` are open; the focused test module does not exist | the session and identity logic is untested |

### 14.3 Development directions

The complete, prioritized work list lives in
[proposal_update1.md](proposal_update1.md) — 45 items, each with its evidence, the
proposed modification and its validation. It supersedes the informal list that
used to live here.

A second round, [proposal_update_3.md](proposal_update_3.md), holds the 24 items
found while investigating two production incidents: a managed `llama.cpp` server
that hangs before opening its listener, and a task that stays `queued` after its
execution was handed to a remote aidir. Every item of that round is applied; the
few remainders are recorded in the sections themselves.

Near-term, in the order the project has been approaching them:

1. **Close the descriptor gaps** (defects 2, 3, 4) and either fix or delete the dead
   logging keys (defect 1). The rule from `memory/context.md` applies: a key that no
   code reads should be **deleted**, not declared.
2. **Make a reload actually reload** (defect 5): rebuild the workers, the endpoint
   configuration and the resource model on SIGHUP, so that the `reload` apply policy
   means what it says.
3. **Teach the scheduler's router to probe OpenAI-family providers** (gap 2), which
   also stops the spurious affinity invalidation.
4. **Persist the affinity store in Redis** and forward the fingerprint across
   instances (gap 3), which also needs the `X-Aidir-KV-Fingerprint` header.
5. **Protect the MCP and queue-state surfaces** (gaps 8, 9), at least by making the
   `auth_mode` default `endpoint` for the peer APIs.
6. **Implement the remaining smart-routing strategies** (gap 1) and the per-alias
   affinity opt-out that `proposal_kv_keeper.md` explicitly deferred.

### 14.4 Deferred tasks

Two items are **deferred** and are deliberately not part of
[proposal_update1.md](proposal_update1.md). Each needs a design decision of its own
rather than a line in a defect list, so neither is scheduled.

#### The middleware chain

**State:** the configuration declares a middleware registry and the endpoints
declare a `middlewares` list, but **no chain is ever executed**.

| What exists | Where |
|---|---|
| The `middleware` section: `middleware.items.<id>.logging` | `config.descriptor.json5:908` |
| `endpoints[].middlewares`: the ordered list of middleware ids | `config.descriptor.json5` (the endpoint children) |
| The logger reads `middleware.items.<tag>.logging.level` for a `middleware` tag | `core/logger.py:130-133` |
| The `middleware` directory | `middleware/.gitkeep` — empty |
| The TODO that says so | `core/endpoints/endpoint_ollama.py:8` — *"apply middleware chain before task creation"* |

**Why deferred.** The question is not "how do I implement a middleware chain" but
"should the configuration promise one at all". A chain that runs before the task
is created, one that wraps the upstream call, and one that wraps the delivery are
three different things with three different failure semantics, and the current
configuration does not say which one is meant. Implementing the wrong one is worse
than having none.

**What a future proposal must settle.**

1. Where the chain runs: before the task is created, around the worker call, around
   the client delivery, or all three.
2. Whether a middleware may reject a request, and with what error.
3. Whether the chain is per endpoint, per worker, or per task type — the current
   configuration places the list on the endpoint and the registry at the root.
4. Whether a middleware failure is fatal, and whether it is visible in the audit
   trail.
5. The alternative that must be considered and rejected explicitly: **delete**
   `middleware` and `endpoints[].middlewares` from the descriptor and keep the
   logger's `middleware` tag for the endpoint-level overrides that already work.

#### The `before_tools` / `context_compressor` extension

**State:** fully designed, **zero implementation**.

| Document | Content |
|---|---|
| [before_tools_worker.md](before_tools_worker.md) | The protocol extension, a four-phase plan, five open TBDs and the explicit non-goals |
| `openaix_spec.md` | **Does not mention it** — the extension was never added to the normative protocol document |

The feature is a pre-tool-call context-processing hook: it compresses or rewrites
the context before the model is asked to call a tool.

**Why deferred.** It is a **new feature**, not a defect. It has its own design
document with open questions, it changes the shape of the agent loop, and it would
land on top of an unfinished round of the existing defects. Mixing it into a defect
round would make both harder to review.

**What a future proposal must settle.**

1. The five open TBDs of `before_tools_worker.md` — the compression contract, the
   size budget, the failure behavior, the interaction with the internal tool loop
   and the audit trail.
2. The relationship with the existing 20-message trim
   ([§6.7](#67-workers)), which already truncates the history and already breaks
   the affinity prefix.
3. Whether it belongs to the `openaix` worker or to a new `context` worker in the
   existing chain (`context_builder` → `context_add_internal_tools` →
   `context_render_openclaw_style`).
4. Whether `openaix_spec.md` gains the extension, so the normative document and
   the implementation stay in step.

### 14.5 Proposal and plan status

See [DOCUMENTS.MD](DOCUMENTS.MD) for the complete index. The notable states:

| Document | State |
|---|---|
| [proposal_update1.md](proposal_update1.md) | **the current work list**: 45 items, excluding the two deferred tasks of [§14.4](#144-deferred-tasks) |
| [proposal_update_3.md](proposal_update_3.md) | the defects of the two production investigations; the applied items are marked as fixed |
| `proposal_doctor.md`, `proposal_recources.md`, `proposal_http_api.md`, `proposal_hooks.md`, `proposal_llama_cpp.md`, `_proposal_log_refactoring.md`, `_proposal_refactoring_1.md` | implemented and frozen |
| `plan_aiset_integration.md` | delivered; the browser acceptance pass (TEST.md 7.1) is the last open item |
| `plan_kv_keeper.md` | delivered |
| `proposal_anthropic.md` | implemented; thinking signatures on rewrites remain |
| `proposal_opencode_go.md` | implemented; 7 release gates open |
| `proposal_aidir_smart_router.md` | phases 1–2 delivered; the later phases are deferred |
| `proposal_kv_keeper.md`, `proposal_searxng.md`, `_proposal_embedding.md`, `proposal_ollama_embed.md` | implemented, but their status lines are stale |
| `before_tools_worker.md` | **deferred** — see [§14.4](#144-deferred-tasks) |
| `_proposal_update_1.md` | analysis only; the resolution is not recorded anywhere |

**A note on proposal documents.** A proposal is written once and then frozen. New
requirements go into the next numbered file; the old one keeps an empty `git diff`.
Editing a proposal that has already been implemented is not permitted, and neither is
editing the `aiset` submodule — outstanding wishes there go into a new
`aiset/proposal_improvementN.md`, applied by that project's owner.

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **aidir** | The project, the process, and a running instance |
| **Endpoint** | A published HTTP surface; one per `config.endpoints[]` entry |
| **Provider** | A configured upstream service |
| **Model** | One model inside a provider; may be a real model or a smart candidate list |
| **Smart model** | A model whose `items[]` are candidates and whose selection is dynamic |
| **Executor** | The concrete upstream that actually served a request |
| **Worker** | A pluggable execution unit discovered from `workers/<type>/<id>/app.py` |
| **Task** | One queued unit of work |
| **Resource** | A capacity pool with a metric vector (VRAM and similar) |
| **Base / session resource** | The one-time model footprint / the incremental cost of one request |
| **Admission** | The decision whether a model may start now, given the resources |
| **Affinity** | The mapping from a context prefix to the executor that holds its KV cache |
| **Anchor** | The truncated prefix length used as the affinity key |
| **Envid** | A named execution environment with a context spec and worker overrides |
| **Context** | The rendered system prompt, tools, rules and files for a task |
| **Audit event** | A recorded client or upstream exchange, correlated by task id |
| **Descriptor** | The schema of a configuration document |
| **Document** | A configuration file plus its descriptor, identified by `main` or `worker:<type>:<id>` |
| **Principal** | An `aiset` identity with a permission set |
| **Protocol transparency** | Returning the upstream's original bytes when the protocols match |
| **Two-phase streaming** | Waiting for the first chunk before opening the response, to keep the HTTP status meaningful |
| **Defer** | Re-queueing a task that cannot start now, instead of blocking on it |
