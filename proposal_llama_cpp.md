# Proposal: llama.cpp Provider Support

## Summary

This document defines the llama.cpp integration model for aidir. It is a reference for the implemented design and future changes.

The integration adds an `api: "llama-cpp"` provider family and the `call_llama_cpp` agent worker. It supports both a locally managed `llama-server` process and an externally managed local or remote server.

Goals:

1. Route requests to llama.cpp through its OpenAI-compatible API.
2. Start a local `llama-server` on demand when configured.
3. Safely stop only aidir-owned llama.cpp processes when shared resources are needed.
4. Account for a provider's ownership of shared resources such as VRAM.
5. Allow llama.cpp models to participate in smart routing with Ollama and remote aidir providers.
6. Preserve the public Ollama and OpenAI endpoint contracts exposed by aidir.

## Configuration

### Worker

The worker is an ordinary agent worker and selects its default upstream provider in the same way as other inference workers.

```json5
"workers": {
  "items": {
    "call_llama_cpp": {
      "enabled": true,
      "provider": "llama_local",
      "request_timeout": 100,
      "tools": {
        "search": {"worker": "web_search"},
        "fetch": {"worker": "web_fetch"}
      }
    }
  }
}
```

The worker remains an implementation detail. Normal clients select a model, not a worker. Endpoint route resolution selects `call_llama_cpp` automatically for a resolved `api: "llama-cpp"` provider.

### Provider

```json5
"llama_local": {
  "api": "llama-cpp",
  "host": "127.0.0.1",
  "port": 8888,
  "baseUrl": "http://127.0.0.1:8888",
  "exec_cmd": "~/llama.cpp/build/bin/llama-server -m \"/models/model.gguf\" --alias \"example-llama-model\" --host 127.0.0.1 --port 8888",
  "startup_timeout": 120,
  "models": [
    {
      "id": "example-llama-model",
      "contextWindow": 32768,
      "resources": {
        "local_machine": {"VRAM": 8000}
      }
    }
  ]
}
```

Field rules:

1. `api` must be `llama-cpp`.
2. `baseUrl` is required and identifies the HTTP server.
3. `host` and `port` are explicit configuration metadata and should agree with `baseUrl` and the command.
4. `exec_cmd` is optional. When empty, aidir never starts or stops the server; this supports remote or independently managed deployments.
5. `startup_timeout` is the maximum wait for `GET /health` after a local start. It defaults to 120 seconds.
6. Each model must declare its resource requirements when it shares scheduler-managed resources with other providers.

`exec_cmd` is parsed with `shlex.split` and executed directly without a shell. `~` and environment variables are expanded in each argument. Shell operators, pipelines, redirection, and command substitution are intentionally unsupported.

## Server Lifecycle

`core/local_server_manager.py` owns local llama.cpp process management.

### Start and readiness

When a task reaches `call_llama_cpp`:

1. aidir requests `GET {baseUrl}/health`.
2. If it is healthy, aidir uses the server immediately.
3. If it is not healthy and `exec_cmd` is empty, the task fails with `UPSTREAM_UNREACHABLE`.
4. If it is not healthy and `exec_cmd` is configured, aidir starts the command and waits for `/health` until `startup_timeout`.
5. Startup failures, command errors, an early process exit, and timeout fail the task with an actionable error code.

The command's stdout and stderr are written to `logs/llama_cpp_<provider_id>.log`.

### Ownership and persistence

After aidir starts a server, it stores the PID and the Linux process start ticks in `logs/llama_cpp_servers.json`.

The start-ticks value prevents an old PID record from matching an unrelated process after PID reuse. A process may be terminated only when both values still match.

An already healthy server without a matching aidir record is treated as external:

1. aidir may send inference requests to it;
2. aidir does not persist it as managed;
3. aidir never stops or restarts it.

This rule avoids killing a server started by a user, systemd, Docker, or another aidir instance.

### Stop and force-unload

llama-server does not expose a model-unload API. Force-unload therefore means terminating the owned server process:

1. send `SIGTERM` to the server process group;
2. wait briefly for termination;
3. send `SIGKILL` only when graceful termination did not finish;
4. remove the persisted PID record.

If no matching owned process exists, aidir does not signal anything. The unload attempt fails and resource tracking remains occupied.

## Protocol Translation

llama-server is called through its OpenAI-compatible endpoint:

```text
POST {baseUrl}/v1/chat/completions
GET  {baseUrl}/v1/models
GET  {baseUrl}/health
```

The worker accepts aidir's internal Ollama-shaped agent payload. This makes both public interfaces work without special client configuration:

1. Incoming `POST /api/chat` requests already use that internal shape.
2. Incoming `POST /v1/chat/completions` requests are converted to the same internal shape by the OpenAIx endpoint.
3. `call_llama_cpp` converts messages, tools, `stream`, `temperature`, `top_p`, `seed`, penalties, and `options.num_predict -> max_tokens` for llama.cpp.
4. llama.cpp synchronous and SSE responses are converted back to the internal Ollama-compatible result shape.
5. The normal endpoint serializers then return either Ollama NDJSON/JSON or OpenAI JSON/SSE to the original client.

Internal tool injection and the existing tool loop remain available because `call_llama_cpp` extends the OpenAIx worker behavior.

## Shared Resource Accounting

Resource definitions retain their legacy `provider` field for compatibility, but it is no longer authoritative for a released model.

Every soft consumer records:

```json
{
  "consumer_id": "task-id:worker-id",
  "model_id": "example-llama-model",
  "provider_id": "llama_local",
  "resources": {"VRAM": 8000},
  "released_at": 0
}
```

The scheduler obtains `provider_id` from `task.config.route.resolved_provider`, falling back to the selected worker configuration. It passes this value on reserve and release.

Consequences:

1. A resource may be shared by Ollama, llama.cpp, and other provider families.
2. Force-unload invokes the provider that owns the blocking soft consumer.
3. Reuse is scoped to the same provider and model, preventing a same-named model on another backend from being treated as warm.
4. Failed unloads keep their soft reservation. The scheduler delays the incoming task instead of assuming VRAM was freed.

For `api: "ollama"`, force-unload remains `POST /api/generate` with `keep_alive: 0`. For `api: "llama-cpp"`, it uses the owned-process lifecycle described above.

## Alive Time and Keep-Alive

`alive_time` continues to mean the interval during which a released model is assumed to occupy resources.

`keep_alive` depends on the owning provider:

1. For Ollama, cron sends the existing keep-alive API request during the configured activity window.
2. For llama.cpp with a non-empty `exec_cmd`, cron checks and restarts the locally owned server if it is unavailable during the activity window.
3. For llama.cpp with an empty `exec_cmd`, keep-alive has no effect because aidir cannot safely restart an external server.

llama.cpp has no idle unload timer equivalent to Ollama. Its VRAM remains occupied while the process is alive and is reclaimed on force-unload only when the resource is required for another task.

## Smart Routing

`api: "llama-cpp"` is a valid concrete candidate in `api: "smart"` models.

```json5
"smart_chat": {
  "id": "smart_chat",
  "type": "first_available",
  "items": [
    {"provider": "llama_local", "model": "example-llama-model", "fallback_prio": 10},
    {"provider": "ollama_local", "model": "another-model", "fallback_prio": 20}
  ]
}
```

Candidate evaluation uses the model's configured local resource requirements. A running llama.cpp candidate is additionally checked through `GET /v1/models`.

For a stopped but locally configurable provider (`exec_cmd` is non-empty), smart routing leaves the candidate eligible for lazy startup. The scheduler first resolves resource pressure and unloads conflicting idle models; only then does the selected worker start llama-server. This avoids starting a second VRAM-heavy server merely to probe it.

## Error Contract

Expected worker errors include:

1. `UPSTREAM_UNREACHABLE`: external server is unavailable, or a local server did not become healthy before `startup_timeout`.
2. `INVALID_EXEC_CMD`: `exec_cmd` cannot be parsed or is empty when a start was required.
3. `LLAMA_CPP_START_FAILED`: the executable could not start or the owned process exited during startup.
4. `UPSTREAM_TIMEOUT`: the inference HTTP request exceeded `request_timeout`.
5. `UPSTREAM_ERROR`: llama.cpp returned a non-success HTTP response.
6. `UPSTREAM_INVALID_JSON`: the non-streaming upstream response could not be decoded.

Endpoint error serialization and task timeout semantics remain unchanged from other agent workers.

## Validation

Focused regression coverage is in:

1. `test_llama_cpp.py`: payload/response conversion and unowned-process safety.
2. `test_resource_reuse.py`: provider-aware unload and warm-model resource behavior.

Run:

```bash
./venv/bin/python -m unittest -v test_llama_cpp.py test_resource_reuse.py
```

The active configuration can also be validated with the normal `core.config.Config` loader and `resolve_worker_configs` to ensure `call_llama_cpp` and `llama_local` resolve correctly.

## Non-Goals

1. Managing externally started llama-server processes.
2. Shell-script execution semantics inside `exec_cmd`.
3. Per-model process management within one llama-server instance.
4. Automatic discovery of GGUF files or llama-server binaries.
5. Inferring VRAM requirements at runtime.
6. Stopping locally owned servers on every aidir restart; ownership persists so resource pressure and keep-alive can manage the server across restarts.