# AI Director

**Resource-aware AI request orchestrator with universal endpoints.**

AI Director (aidir) sits between your clients and AI inference backends. It accepts requests in standard protocols (Ollama API, MCP), queues them with priorities, dispatches them to the appropriate workers, and returns responses — synchronously or via streaming. Multiple instances can run on the same server with full isolation.

```
Client → Endpoint → [Middleware chain] → Worker → upstream AI → Client
```

## Features

- **Easy to install** : just clone it and run install.sh
- **Ollama-compatible endpoint** — drop-in replacement; point any Ollama client at aidir instead of Ollama directly
- **Priority task queue** backed by Redis
- **Pluggable workers** — loaded dynamically from `workers/<id>/app.py`
- **Opt-in lifecycle hooks** — simple Python experiments loaded from `hooks/`
- **Streaming support** — server-sent chunks forwarded to clients in real time
- **WebUI** — dashboard with live task queue, resource view, and log streaming
- **Full isolation** — Python venv, Docker for Redis and nginx; no system-level dependencies modified

---

## Requirements

| Dependency | Notes |
|---|---|
| Python 3.11+ | with `python3-venv` |
| Docker + Docker Compose | for Redis and nginx |


```sh
sudo apt update && sudo apt install -y ca-certificates curl gnupg python3 python3-pip python3-venv && sudo install -m 0755 -d /etc/apt/keyrings && sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && sudo chmod a+r /etc/apt/keyrings/docker.asc && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null && sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin && sudo systemctl enable --now docker && sudo usermod -aG docker $USER
```
---

## Installation

```bash
git clone https://github.com/DenisDx/aidir.git
cd aidir
./install.sh
```

Optional config template selection:

```bash
# uses config.json5.example
./install.sh

# same as above
./install.sh config

# uses prod.json5.example
./install.sh prod

# uses an explicit template filename
./install.sh prod.json5.example
```

The script:
1. Checks prerequisites (Docker, Python, venv).
2. Creates or replaces `config.json5` from a selected config template when needed.
  If a template argument is passed explicitly, or if `config.json5` does not exist, the installer:
  - rotates backups: `config.json5.bak` -> `.bak.bak` -> `.bak.bak.bak`
  - keeps up to 3 backup generations
  - copies the selected template into `config.json5`
2. Creates .env file and fills required field in the dialogue mode 
3. Creates a Python virtual environment in `./venv/` and installs dependencies.
4. Builds Docker images, checks all aidir listener ports, then starts Docker containers (Redis, nginx).
  On a fresh installation, an occupied default port is replaced with the next available port and saved in `.env`.
  On an update, the installer proposes a replacement for an occupied configured port and asks for confirmation.
5. Adds a cron entry for periodic maintenance (`core/cron.py`).
6. Registers `core/app.py` as a `systemd` user service (`aidir.service`) and starts it.

On **re-install / update** the script rebuilds images and restarts services without touching existing data.

### WSL without system cron

When `crontab` is unavailable, set `cron.embedded` to `true` in `config.json5`.
AIDIR will run periodic maintenance, including log retention and model keep-alive
checks, inside its main process. The embedded scheduler runs only while AIDIR is
running and starts after the next service restart.

---

## Configuration

The proposed cross-project settings library, aiset, is described in
[aiset/SPEC_.md](aiset/SPEC_.md). It is the complete working draft, not an available
feature. The [aiset directory](aiset/) is maintained separately and excluded
from this repository's Git tracking. See [DOCUMENTS.MD](DOCUMENTS.MD) for the
project document index.

Configuration is split into two files:

### `.env` — secrets and environment-specific values

Copy from `.env.example`. **Never commit this file** (it is in `.gitignore`).

| Variable | Default | Description |
|---|---|---|
| `AIDIR_INSTANCE` | `aidir1` | Key prefix in Redis; change if running multiple instances |
| `AIDIR_ROOT` | *(must set)* | Absolute path to the project directory |
| `REDIS_HOST` | `127.0.0.1` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_PASSWORD` | *(empty)* | Redis password (leave empty for no auth) |
| `OPENAIX_ENDPOINT_PORT` | `21434` | Port where aidir listens for OpenAIx/Ollama-compatible API clients |
| `OPENAIX_ENDPOINT_HOST` | `0.0.0.0` | Bind address for the OpenAIx endpoint |
| `MCP_ENDPOINT_PORT` | `20001` | Port where aidir listens for MCP JSON-RPC requests |
| `MCP_ENDPOINT_HOST` | `0.0.0.0` | Bind address for the MCP endpoint |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Upstream Ollama server URL |
| `WEBUI_PORT` | `20082` | WebUI backend listen port |
| `WEBUI_HOST` | `0.0.0.0` | WebUI backend bind address |
| `NGINX_HTTP_PORT` | `8080` | Public HTTP port exposed by nginx (must differ from `WEBUI_PORT`) |
| `ROOT_USER` | `admin` | WebUI login |
| `ROOT_PASSWORD` | `changeme` | WebUI password — **change this** |
| `LOG_WIPE_PERIOD` | `0` | Log cleanup interval in seconds (0 = disabled) |
| `CRON_PERIOD` | `1` | Cron run interval in minutes (any integer >= 1) |
| `REQUEST_TIMEOUT` | `100` | Max duration of one outbound HTTP call to an upstream LLM/API |
| `TASK_RESTART_WAIT_TIMEOUT_SECONDS` | `120` | Max graceful restart wait for active tasks before shutdown continues |
| `TASK_QUEUE_TIMEOUT_SECONDS` | `300` | Max time a task may wait before its first transition to `running` |
| `TASK_RUN_TIMEOUT_SECONDS` | `300` | Max total execution time after a task has started running |
| `TLS_CERT_PATH` | *(optional)* | Path to TLS certificate (handled by nginx) |
| `TLS_KEY_PATH` | *(optional)* | Path to TLS private key |

### Timeout semantics

The timeout model is split into three separate responsibilities:

- `REQUEST_TIMEOUT` limits one outbound HTTP request to an upstream LLM or API.
- `TASK_QUEUE_TIMEOUT_SECONDS` limits only the waiting period before the task starts running for the first time.
- `TASK_RUN_TIMEOUT_SECONDS` limits the task after it has started running, including tool loops and multiple LLM calls.

Important rule:

- if a task reaches `running` just before queue timeout expires, queue timeout no longer applies to that task
- from that point on, only `TASK_RUN_TIMEOUT_SECONDS` can stop it

`TASK_RESTART_WAIT_TIMEOUT_SECONDS` is separate from task lifecycle timing. It is used only during graceful restart/shutdown to decide how long the service should wait for already running tasks before forced cancellation.

### `config.json5` — active system configuration

This is the live runtime config used by the installed instance.
It is not tracked in git and may be replaced by `install.sh` from a template when:

- you pass an explicit config template argument to `install.sh`
- `config.json5` is missing

The tracked template shipped with the repository is `config.json5.example`.
You can add more templates such as `prod.json5.example`, `lab.json5.example`, and select them at install time.

JSON5 format (comments and trailing commas allowed). Uses `${VAR}` and `${VAR:-default}` substitution from `.env`. Contains no secrets or machine-specific paths directly.

Key sections:

- **`endpoints`** — which APIs to expose and on which ports
- **`workers.items`** — per-worker config (logging overrides, upstream provider, per-call `request_timeout`)
- **`models.providers`** — upstream AI providers (Ollama local/remote, OpenAI-compatible)
- **`webui`** — WebUI port, auth mode, users
- **`tasks`** — restart drain timeout plus queue/run lifecycle timeouts
- **`logging`** — log levels per subsystem (0=EMERG … 7=DEBUG)
- **`resources`** — hardware resources to track (VRAM etc.; enforced in future releases)

### Hooks

Hooks are administrator-controlled Python files for quickly trying lifecycle
behavior without changing core code. They are discovered from `hooks/<id>.py`
or `hooks/<id>/app.py` and are disabled unless the module sets `ENABLED = True`.

The bundled `hooks/example.py` is a disabled no-op template. The optional
`hooks/llama_cpp_empty_response_recovery.py` restarts an aidir-owned local
llama.cpp provider and retries one empty OpenAI-compatible response. See
[proposal_hooks.md](proposal_hooks.md) for the event API and safety rules.

### Resource sensor monitoring

Each resource may optionally define `monitoring`. aidir runs each configured
sensor command at `poll_interval` seconds, parses one numeric value, and shows
the latest value and alert/error state on the Resources panel. Commands run as
the aidir service user; only trusted administrators must be able to edit the
configuration.

```json5
{
  id: "local_machine",
  type: "cuda",
  limits: { VRAM: 10000 },
  monitoring: {
    poll_interval: 10,
    command_timeout: 5,
    sensors: [
      {
        id: "gpu_temperature",
        label: "GPU temperature",
        unit: "C",
        command: "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits",
        threshold: {
          operator: "above",
          value: 85,
          reactions: ["notify", "release_resource"],
          cooldown: 300
        }
      },
      {
        id: "cpu_temperature",
        label: "CPU temperature",
        unit: "C",
        command: "sensors",
        pattern: "Package id 0:\\s+\\+([0-9.]+)",
        threshold: { operator: "above", value: 90, reactions: ["notify"] }
      }
    ]
  }
}
```

Without `pattern`, a command must write exactly one finite number to standard
output. With `pattern`, its first capture group is parsed as the number; use
`match_group` to select another numbered or named group. This accommodates
motherboard and fan readings from `sensors`, SSD readings from a configured
`smartctl` command, and any other executable probe.

A threshold uses `above` (strictly greater than) or `below` (strictly less
than). `notify` records a visible alert and a warning in the system log.
`release_resource` reuses the dashboard's safe release behavior: it unloads
only idle models, and reports that active tasks prevent release rather than
interrupting them. Reactions run once on crossing a threshold, then no more
often than `cooldown` seconds (300 seconds by default); a normal reading re-arms
the sensor.

### Audit storage and task retention

`logging.audit` stores task-correlated client and LLM request/response events in
`logs/audit/`. JSON and small UTF-8 text bodies are inline; binary bodies and
bodies larger than `logging.audit.max_inline_body_bytes` are file-backed. The
default inline limit is 1 MiB. Audit partitions are retained according to
`retention_days` and `max_total_bytes`; `emergency_max_total_bytes` is used only
when normal retention cannot keep storage within the emergency limit.

The authenticated Task Viewer exposes compact task summaries at
`/api/tasks/viewer/search`. Individual task details include an audit-event
manifest at `/api/tasks/viewer/{task_id}/detail`; request and raw event bodies
are loaded only through `/api/tasks/viewer/{task_id}/request` and
`/api/tasks/viewer/{task_id}/raw`. File-backed bodies use the opaque
`/api/tasks/viewer/audit-files/{file_id}` endpoint.

External terminal task metadata is removed by cron after
`tasks.external_task_live` seconds (default: 86400). Queued and running tasks
are never removed by this retention job. On service startup, persisted queued
agent and tool tasks are restored before scheduling; malformed queue members
are quarantined, while persisted running tasks are marked failed with
`SERVICE_RESTARTED` rather than replayed.

### llama.cpp providers

Set a provider's `api` to `llama-cpp` to use `llama-server` through its OpenAI-compatible API. `call_llama_cpp` automatically converts incoming Ollama requests to `/v1/chat/completions`; OpenAI requests follow the same internal route.

llama.cpp HTTP errors are returned with the original status, complete response body, and content type, without replacing their message, code, type, or extra diagnostic fields with an aidir error envelope. This also applies to streaming requests rejected before their first output. Once streaming has begun, the original JSON error is forwarded as an SSE or NDJSON error event; the already-sent HTTP status cannot change. Connection failures and timeouts still use aidir's own error handling.

Use `exec_cmd` for a local server command. aidir starts it on demand, waits for `/health`, and records its PID in `logs/llama_cpp_servers.json`. Only such recorded processes are stopped when shared resources need VRAM. An already healthy server is treated as external and is never stopped. Leave `exec_cmd` empty for an externally managed or remote server.

For a `llama-cpp` provider, resource `keep_alive` means restart-on-failure during its active window; it does not send API pings. Smart routes probe a running provider via `/v1/models`; an unavailable provider with `exec_cmd` remains eligible for lazy startup after the scheduler has freed resources.

### Network ports and routing map

Runtime topology:

```text
browser/client
  -> host:8080 (nginx in docker, public HTTP + WS)
  -> host:21434 (OpenAIx/Ollama-compatible endpoint)
  -> host:20001 (MCP endpoint)

nginx:80 in container
  -> host:${WEBUI_PORT} (WebUI backend, default 20082)

core app
  -> host:${REDIS_HOST}:${REDIS_PORT} (Redis, default 127.0.0.1:6379)
  -> ${OLLAMA_BASE_URL} (upstream Ollama, usually 127.0.0.1:11434)
```

#### Ports

| Port | Service | Traffic | Source of truth | Change path |
|---|---|---|---|---|
| `8080` | nginx public entrypoint | HTTP + WebSocket | `.env: NGINX_HTTP_PORT`, `docker-compose.yml` | Change `.env`, then `docker compose up -d nginx` |
| `20082` | WebUI backend | HTTP + WS upstream for nginx | `.env: WEBUI_PORT`, `config.json5` | Change `.env`, then restart aidir |
| `21434` | OpenAIx endpoint | HTTP | `.env: OPENAIX_ENDPOINT_PORT`, `config.json5` | Change `.env`, then restart aidir |
| `20001` | MCP endpoint | HTTP JSON-RPC | `.env: MCP_ENDPOINT_PORT`, `config.json5` | Change `.env`, then restart aidir |
| `6379` | Redis | TCP | `.env: REDIS_HOST`, `.env: REDIS_PORT`, `docker-compose.yml` | Change `.env`, then `docker compose up -d redis` |
| `11434` | Ollama upstream | HTTP | `.env: OLLAMA_BASE_URL` | Change `.env`, then restart aidir if needed |

Rules:

- nginx always listens on container port `80`; Docker publishes it to host `${NGINX_HTTP_PORT}`.
- HTML and WebSocket share the same public nginx port. WS uses `/ws/*`; it does not need a separate port.
- Before Docker or the core app starts, `install.sh` checks the Redis, nginx, WebUI, OpenAIx, and MCP listener ports.
  It reserves each selected port during that check, so the aidir services cannot be assigned the same host port.
- For a new `.env`, an occupied port is automatically changed to the next free TCP port. For an existing `.env`,
  the installer shows the suggested replacement and waits for confirmation; non-interactive runs fail rather than
  changing a configured port without consent.
- On a re-install, aidir stops only the current instance's core service and Compose containers before checking ports,
  so their existing listeners do not appear as external conflicts.
- The nginx container reaches the WebUI backend through Docker's
  `host.docker.internal` host-gateway and renders the upstream port from
  `WEBUI_PORT` at startup. It then runs `nginx -t` on the rendered
  configuration before serving traffic. Change `WEBUI_PORT` in `.env` and
  recreate the nginx container.

#### Routes

Public nginx entrypoint: `http://HOST:${NGINX_HTTP_PORT}`

| Route | Type | Destination |
|---|---|---|
| `/` | HTTP | Static frontend from `webui/frontend` |
| `/api/*` | HTTP proxy | `http://host.docker.internal:${WEBUI_PORT}` |
| `/ws/*` | WebSocket proxy | `ws://host.docker.internal:${WEBUI_PORT}` |

WebUI backend: `http://HOST:${WEBUI_PORT}`

| Route group | Type | Purpose |
|---|---|---|
| `/api/auth/login`, `/api/auth/me`, `/api/auth/logout` | HTTP | Session management |
| `/api/tasks`, `/api/status`, `/api/logs` | HTTP | Runtime state and logs |
| `/api/config`, `/api/config/raw`, `/api/config/fields` | HTTP | Config read/write |
| `/api/workers/models`, `/api/endpoints/info` | HTTP | Catalog and metadata |
| `/api/test/llm`, `/api/test/mcp`, `/api/test/agent/*`, `/api/test/agent` | HTTP | UI-side test/proxy routes |
| `/api/restart` | HTTP | Graceful restart request |
| `/ws/logs` | WebSocket | Live log stream |

OpenAIx endpoint: `http://HOST:${OPENAIX_ENDPOINT_PORT}`

Use the queue-state endpoint to inspect whether a provider/model resource can start immediately and how many queued tasks already target that resource.

| Route | Type | Purpose |
|---|---|---|
| `/api/chat` | POST | Ollama-compatible chat |
| `/api/embed` | POST | Ollama-compatible non-streaming embeddings |
| `/api/tags` | GET | Ollama-style models list |
| `/api/show` | POST | Configured Ollama-style model metadata (`name` required) |
| `/v1/chat/completions` | POST | OpenAI-compatible chat |
| `/v1/embeddings` | POST | OpenAI-compatible non-streaming embeddings |
| `/v1/models` | GET | OpenAI-style models list |
| `/api/providers/{provider}/models/{model}/queue-state` | GET | Read-only queue state for a provider/model pair |
| `/v1/providers/{provider}/models/{model}/queue-state` | GET | Read-only queue state for a provider/model pair |
| `/health` | GET | Health check |

#### Chat response transparency

For direct chat calls with matching client and executor protocols (OpenAI/OpenAI or Ollama/Ollama), aidir returns the executor's original response body and content type. Streaming preserves the original SSE or NDJSON events, including reasoning, unknown fields, IDs, timestamps, multiple choices, usage details, and stream terminators. Reasoning is not substituted for an empty answer.

Two exceptions apply:

- **Internal multistep tool execution:** intermediate model/tool turns remain inside aidir. Only the final answer is returned, with reasoning fields removed after an internal tool actually executes. Usage statistics and other final-response fields remain available. Streaming may be synthesized from the final synchronous tool-loop response.
- **Different protocols:** aidir converts the response envelope as closely as possible, preserving reasoning and compatible additional fields. Ollama `thinking` is exposed as OpenAI `reasoning_content`, and OpenAI reasoning is retained as Ollama `thinking` alongside its original fields. Native-only fields can remain as extensions; byte-for-byte identity is not possible across protocols.

Caller-owned tool definitions do not trigger aidir's internal tool loop and retain native streaming.

Completed, error-free OpenAI SSE chat responses are displayed in audit logs as one reconstructed final JSON completion with `body_format: "sse"` and `sse_chunk_count`. The audit assembler joins content, reasoning, tool-call arguments, choices, finish reasons, usage, and compatible fields from all chunks. Comments and `[DONE]` are not counted. A stream that cannot be unambiguously assembled, or is malformed, incomplete, failed, or error-bearing, remains one raw string for diagnosis. This audit-only normalization does not change client delivery; large SSE bodies also retain an original body-file link.

MCP endpoint: `http://HOST:${MCP_ENDPOINT_PORT}`

| Route | Type | Purpose |
|---|---|---|
| `/mcp` | POST | JSON-RPC methods: `initialize`, `ping`, `tools/list`, `tools/call` |
| `/health` | GET | Health check |

Redis:

- TCP `${REDIS_HOST}:${REDIS_PORT}` -> redis container `6379`

#### Validation checklist

Use these checks after install or after changing ports:

```bash
# listeners
ss -ltnp | egrep ':(8080|20082|21434|20001|6379|11434)\\b'

# public WebUI entrypoint
curl -s -o /dev/null -w 'webui_public:%{http_code}\n' http://127.0.0.1:${NGINX_HTTP_PORT}

# nginx -> webui backend proxy path (401 expected without auth cookie)
curl -s -o /dev/null -w 'webui_auth_me:%{http_code}\n' http://127.0.0.1:${NGINX_HTTP_PORT}/api/auth/me

# openaix
curl -s -o /dev/null -w 'openaix_health:%{http_code}\n' http://127.0.0.1:${OPENAIX_ENDPOINT_PORT}/health

# mcp health + ping
curl -s -o /dev/null -w 'mcp_health:%{http_code}\n' http://127.0.0.1:${MCP_ENDPOINT_PORT}/health
curl -s http://127.0.0.1:${MCP_ENDPOINT_PORT}/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'

# redis
redis-cli -h ${REDIS_HOST} -p ${REDIS_PORT} ping
```

### MCP test tools (included)

Default config exposes an MCP endpoint with two test tools:

- `search` -> worker `web_search` (Brave Web Search API)
- `fetch` -> worker `web_fetch` (Brave LLM Context API)
- `selftest` -> worker `selftest` (runtime health checks)
- `http_api` -> worker `http_api` (configured connector calls)

These tools require `BRAVE_APIKEY` (via `.env` and `config.json5` mapping).

`http_api` is wired by default in endpoint config, but the worker itself is disabled until configured.
To disable MCP exposure explicitly, remove `http_api` from `endpoints[].tools` where `id: "mcp"`.

Quick checks:

```bash
curl http://127.0.0.1:20001/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

curl http://127.0.0.1:20001/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"query":"aidir"}}}'

curl http://127.0.0.1:20001/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search","arguments":{"query":"python asyncio task queue","count":5,"country":"US","search_lang":"en","freshness":"pm"}}}'

curl http://127.0.0.1:20001/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"fetch","arguments":{"url":"https://docs.python.org/3/library/asyncio.html","query":"python asyncio task group cancellation","maximum_number_of_tokens":4096,"maximum_number_of_urls":8}}}'

curl http://127.0.0.1:20001/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"selftest","arguments":{}}}'

curl http://127.0.0.1:20001/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"http_api","arguments":{"connector":"example_connector","operation":"list_topics","params":{"group_name":"example_group"}}}}'
```

---

## Usage examples

*(Coming soon)*

---

## Diagnostic commands

### Service management

```bash
# Status
systemctl --user status aidir

# Restart
systemctl --user restart aidir

# After changing systemd timeout defaults in install.sh, re-run install.sh once
# to regenerate the unit with TimeoutStopSec=120.

# Reload config (no restart, sends SIGHUP)
systemctl --user reload aidir

# View service logs
journalctl --user -u aidir -f
```

### Docker / infrastructure

```bash
# Container status
docker compose ps

# Redis shell
docker exec -it aidir_redis redis-cli

# Restart only Redis
docker compose restart redis

# View nginx logs
docker logs aidir_nginx -f
```

### Health checks

```bash
# Ollama endpoint health
curl -s http://localhost:21434/health

# WebUI via nginx (public)
curl -s http://localhost:8080

# WebUI backend direct (local service)
curl -s -o /dev/null -w 'status:%{http_code}\n' http://localhost:20082/api/auth/me || echo "check WEBUI_PORT / service status"

# Redis ping
docker exec aidir_redis redis-cli ping

# Check task queue length (agent type)
docker exec aidir_redis redis-cli ZCARD aidir1:queue:agent
```

### Test requests

```bash
# Basic chat request (non-streaming)
curl http://localhost:21434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3",
    "messages": [{"role": "user", "content": "Hello, what is 2+2?"}],
    "stream": false
  }'

# Streaming chat
curl http://localhost:21434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3",
    "messages": [{"role": "user", "content": "Count to 5."}],
    "stream": true
  }'

# Explicit worker selection (extended syntax)
curl http://localhost:21434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "worker": "call_ollama",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'
```

### Troubleshooting Brave tools

If MCP `search`/`fetch` tools fail, check these common cases first:

- `MISSING_API_KEY`: `BRAVE_APIKEY` is not set in `.env` (or `workers.items.web_* .apiKey` is missing).
- `BRAVE_HTTP_ERROR 401/403`: invalid API key or subscription issue.
- `BRAVE_HTTP_ERROR 429`: rate limit exceeded; retry with backoff.
- `BRAVE_REQUEST_FAILED`: network/DNS/timeout issue when connecting to `api.search.brave.com`.

Quick diagnostics:

```bash
# Check env key presence (masked output)
grep '^BRAVE_APIKEY=' .env | sed 's/=.*/=<set>/'

# Check effective worker config contains brave + apiKey placeholder
grep -n '"web_search"\|"web_fetch"\|"provider"\|"apiKey"' config.json5

# Run selftest over MCP (includes brave_web_search_api check)
curl http://127.0.0.1:20001/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":91,"method":"tools/call","params":{"name":"selftest","arguments":{}}}'
```

### Log files

```
logs/all.log        — combined log for all subsystems
logs/system.log     — core system events
logs/worker.log     — worker execution events
logs/http.log       — endpoint request/response events
logs/webui.log      — WebUI backend events
```

```bash
# Tail combined log
tail -f logs/all.log

# Filter errors only
grep '\[ERROR\]\|\[CRIT\]\|\[EMERG\]' logs/all.log
```
