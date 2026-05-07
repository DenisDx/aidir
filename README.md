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
| `WEBUI_PORT` | `20080` | WebUI backend listen port |
| `WEBUI_HOST` | `127.0.0.1` | WebUI backend bind address |
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

### MCP test tools (included)

Default config exposes an MCP endpoint with two test tools:

- `search` -> worker `web_search` (returns mock search results)
- `fetch` -> worker `web_fetch` (downloads URL and returns short content preview)

Quick checks:

```bash
curl http://127.0.0.1:20001/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

curl http://127.0.0.1:20001/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"query":"aidir"}}}'
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
curl -s http://localhost:20080/health || echo "check WEBUI_PORT / service status"

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
