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

The script:
1. Checks prerequisites (Docker, Python, venv).
2. Creates .env file and fills required field in the dialogue mode 
3. Creates a Python virtual environment in `./venv/` and installs dependencies.
4. Builds and starts Docker containers (Redis, nginx).
  Also auto-fixes WebUI port conflicts: `NGINX_HTTP_PORT` is set and kept different from `WEBUI_PORT`.
5. Adds a cron entry for periodic maintenance (`core/cron.py`).
6. Registers `core/app.py` as a `systemd` user service (`aidir.service`) and starts it.

On **re-install / update** the script rebuilds images and restarts services without touching existing data.

---

## Configuration

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
| `TASK_RESTART_WAIT_TIMEOUT_SECONDS` | `120` | Max graceful restart wait for active tasks before shutdown continues |
| `TASK_QUEUE_TIMEOUT_SECONDS` | `300` | Max time a task waits in queue before cancellation |
| `TASK_RUN_TIMEOUT_SECONDS` | `300` | Max execution time per task |
| `TLS_CERT_PATH` | *(optional)* | Path to TLS certificate (handled by nginx) |
| `TLS_KEY_PATH` | *(optional)* | Path to TLS private key |

### `config.json5` — system configuration

JSON5 format (comments and trailing commas allowed). Uses `${VAR}` and `${VAR:-default}` substitution from `.env`. Contains no secrets or machine-specific paths directly.

Key sections:

- **`endpoints`** — which APIs to expose and on which ports
- **`workers.items`** — per-worker config (logging overrides, upstream provider)
- **`models.providers`** — upstream AI providers (Ollama local/remote, OpenAI-compatible)
- **`webui`** — WebUI port, auth mode, users
- **`tasks`** — queue and run timeouts
- **`logging`** — log levels per subsystem (0=EMERG … 7=DEBUG)
- **`resources`** — hardware resources to track (VRAM etc.; enforced in future releases)

### Network ports and routing map

This project uses host services plus two Docker containers (nginx and redis).

#### Effective port map

| Port | Direction | Service | Config source | How to change |
|---|---|---|---|---|
| `8080` (default) | host -> docker nginx:80 | Public WebUI entrypoint (HTTP + WS) | `.env: NGINX_HTTP_PORT`, `docker-compose.yml` | Change `NGINX_HTTP_PORT` in `.env`, then `docker compose up -d` |
| `20082` (default) | host -> core app | WebUI backend (FastAPI) | `.env: WEBUI_PORT`, `config.json5: webui.port` | Change `WEBUI_PORT` in `.env` |
| `21434` (default) | host -> core app | OpenAIx/Ollama-compatible endpoint | `.env: OPENAIX_ENDPOINT_PORT`, `config.json5: endpoints[api=openaix].port` | Change `OPENAIX_ENDPOINT_PORT` in `.env` |
| `20001` (default) | host -> core app | MCP JSON-RPC endpoint | `.env: MCP_ENDPOINT_PORT`, `config.json5: endpoints[api=mcp].port` | Change `MCP_ENDPOINT_PORT` in `.env` |
| `6379` (default) | host -> docker redis:6379 | Redis queue/state | `.env: REDIS_HOST/REDIS_PORT`, `docker-compose.yml` | Change `REDIS_HOST`/`REDIS_PORT` in `.env`, then `docker compose up -d` |
| `11434` (typical) | core app -> external | Upstream Ollama API | `.env: OLLAMA_BASE_URL`, provider base URL in `config.json5` | Change `OLLAMA_BASE_URL` in `.env` |

Notes:

- Nginx container listens on internal port `80`. Docker publishes it to host `${NGINX_HTTP_PORT}`.
- WebSocket does not require a separate public port: it is served via the same nginx port as HTML.
- `WEBUI_PORT` is backend-only and should not equal `NGINX_HTTP_PORT`.

#### Route map

Public via nginx (`http://HOST:${NGINX_HTTP_PORT}`):

| Route | Type | Upstream |
|---|---|---|
| `/` | HTTP | Static frontend (`webui/frontend`) |
| `/api/*` | HTTP proxy | `http://host.docker.internal:${WEBUI_PORT}` |
| `/ws/*` | WebSocket proxy | `ws://host.docker.internal:${WEBUI_PORT}` |

Direct WebUI backend (`http://HOST:${WEBUI_PORT}`):

| Route | Type | Purpose |
|---|---|---|
| `/api/auth/login` | POST | Login |
| `/api/auth/me` | GET | Session check (`401` without cookie is expected) |
| `/api/auth/logout` | POST | Logout |
| `/api/tasks` | GET | Tasks list |
| `/api/status` | GET | Runtime summary |
| `/api/logs` | GET | Last log lines |
| `/api/config` | GET | Effective config |
| `/api/config/raw` | GET/POST | Raw config read/write |
| `/api/config/fields` | GET/POST | Field-level config operations |
| `/api/workers/models` | GET | Workers/providers info |
| `/api/endpoints/info` | GET | Endpoints/tools info |
| `/api/test/llm` | POST | Proxy test to ollama endpoint |
| `/api/test/mcp` | POST | Proxy test to MCP endpoint |
| `/api/test/agent/endpoints` | GET | Agent test endpoint options |
| `/api/test/agent/catalog` | GET | Endpoint catalog for agent test |
| `/api/test/agent/models` | GET | Models for selected endpoint |
| `/api/test/agent` | POST | End-to-end agent test |
| `/api/restart` | POST | Request graceful restart |
| `/ws/logs` | WS | Live log stream (auth required) |

OpenAIx endpoint (`http://HOST:${OPENAIX_ENDPOINT_PORT}`):

| Route | Type | Purpose |
|---|---|---|
| `/api/chat` | POST | Ollama-compatible chat |
| `/api/tags` | GET | Model list (ollama format) |
| `/v1/chat/completions` | POST | OpenAI-compatible chat |
| `/v1/models` | GET | Model list (openai format) |
| `/health` | GET | Health check |

MCP endpoint (`http://HOST:${MCP_ENDPOINT_PORT}`):

| Route | Type | Purpose |
|---|---|---|
| `/mcp` | POST | JSON-RPC methods (`initialize`, `ping`, `tools/list`, `tools/call`) |
| `/health` | GET | Health check |

Redis:

- TCP: `${REDIS_HOST}:${REDIS_PORT}` -> redis container `6379`.

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

These tools require `BRAVE_APIKEY` (via `.env` and `config.json5` mapping).

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
