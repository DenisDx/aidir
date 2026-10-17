# AIDIR Test Guide

This document is the test protocol for the AIDIR repository. Run commands from
the repository root.

## 1. Test levels

| Level | Purpose | When to run |
|---|---|---|
| Syntax and static checks | Detect import, syntax, and whitespace errors before running services. | Every change |
| Unit and component tests | Verify core, workers, endpoints, scheduler, resource handling, and WebUI behavior with test doubles. | Every code change |
| Smoke tests | Confirm that a deployed instance starts and its essential dependencies and endpoints respond. | After installation, restart, configuration, or deployment |
| Regression tests | Run the complete automated suite and the critical live checks. | Before merging or releasing |
| Manual acceptance checks | Verify user-visible protocols, streaming, authentication, WebUI, and MCP behavior. | Before a release or after changing the affected surface |

## 2. Test environment

Use Python 3.11 or newer and the project virtual environment. If the
environment has not been created yet:

```bash
./install.sh
```

For an existing checkout, install the pinned Python dependencies without
re-running the installer:

```bash
source venv/bin/activate
python -m pip install -r requirements.txt
```

The automated tests are mostly isolated from external AI services, but the
live smoke and acceptance checks require:

- Redis running on the configured `REDIS_HOST` and `REDIS_PORT`;
- the AIDIR core service running for endpoint and WebUI checks;
- Docker Compose services running for Redis and the public nginx entrypoint;
- an upstream Ollama or another configured provider for inference checks;
- valid `.env` credentials for authenticated WebUI checks;
- provider credentials such as `BRAVE_APIKEY` only when testing those tools.

Never run destructive test requests against production data. Use a dedicated
instance name (`AIDIR_INSTANCE`) and a test configuration where possible.

## 3. Syntax and static checks

Run these checks before the test suites:

```bash
source venv/bin/activate
python -m compileall -q core workers webui smoke_test.py
git diff --check
```

`compileall` checks Python syntax and `git diff --check` detects whitespace
errors in the current change.

## 4. Automated unit and component tests

Run the complete repository suite with the standard-library unittest runner:

```bash
source venv/bin/activate
python -m unittest discover -s . -p 'test_*.py' -v
```

The suite includes tests for:

- endpoint auditing, request limits, embeddings, queue state, and queue
  timeout behavior;
- OpenAIx/Ollama argument parsing, routing, response transparency, and
  llama.cpp lifecycle;
- scheduler priority, queue bypass, timeout diagnostics, restart recovery,
  shutdown, and task call counts;
- resource monitoring, resource reuse, peer resources, and context overflow;
- cron logging and repair, hooks, HTTP API workers, web search fallback, and
  WebUI live logs and task viewer search.

To rerun only the tests relevant to a change, pass module names explicitly:

```bash
python -m unittest -v \
  test_openaix_tool_args_parser \
  test_openaix_queue_state \
  test_openaix_queue_timeout
```

Examples:

```bash
# Scheduler and task lifecycle
python -m unittest -v \
  test_task_priority_defaults \
  test_scheduler_queue_bypass \
  test_queue_restart_recovery \
  test_shutdown_lifecycle

# Resource and timeout behavior
python -m unittest -v \
  test_resource_monitor \
  test_resource_reuse \
  test_peer_resources \
  test_scheduler_timeout_diagnostics \
  test_context_overflow

# Endpoint, protocol, and WebUI behavior
python -m unittest -v \
  test_llama_cpp \
  test_openaix_embeddings \
  test_response_transparency \
  test_viewer_search \
  test_webui_live_logs
```

A focused test run is not a replacement for the full suite before a release.

## 5. Smoke test after startup or deployment

### 5.1 Process and container checks

```bash
docker compose ps
systemctl --user status aidir --no-pager -n 30
ss -ltnp | grep -E ':(8080|20082|21434|20001|6379|11434)\b'
```

The exact ports come from `.env`; replace the defaults in the command when
they differ. Redis and nginx containers should be running, the core service
should be active, and expected listeners should be present.

### 5.2 Dependency and endpoint health

```bash
redis-cli -h "${REDIS_HOST:-127.0.0.1}" \
  -p "${REDIS_PORT:-6379}" ping

curl -fsS "http://127.0.0.1:${OPENAIX_ENDPOINT_PORT:-21434}/health"
curl -fsS "http://127.0.0.1:${MCP_ENDPOINT_PORT:-20001}/health"
curl -sS -o /dev/null -w 'webui:%{http_code}\n' \
  "http://127.0.0.1:${NGINX_HTTP_PORT:-8080}/"
curl -sS -o /dev/null -w 'webui-auth:%{http_code}\n' \
  "http://127.0.0.1:${NGINX_HTTP_PORT:-8080}/api/auth/me"
```

Expected results:

- Redis returns `PONG`;
- OpenAIx and MCP `/health` return HTTP 200;
- the public WebUI returns HTTP 200;
- unauthenticated `/api/auth/me` returns the configured unauthenticated
  response (normally HTTP 401).

Check the configured upstream separately:

```bash
curl -fsS "${OLLAMA_BASE_URL:-http://127.0.0.1:11434}/api/tags"
```

### 5.3 Minimal protocol checks

OpenAI-compatible model discovery:

```bash
curl -fsS \
  "http://127.0.0.1:${OPENAIX_ENDPOINT_PORT:-21434}/v1/models"
```

MCP JSON-RPC ping:

```bash
curl -fsS "http://127.0.0.1:${MCP_ENDPOINT_PORT:-20001}/mcp" \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
```

These checks validate routing and protocol availability without requiring an
inference request. Run a small `/v1/chat/completions` or `/api/chat` request
only when a test model is configured and the request cost is acceptable.

## 6. Regression protocol

Run the following sequence before merging or releasing:

1. Confirm the working tree and configuration target are correct:
   `git status --short`, the intended `.env`, and a non-production
   `AIDIR_INSTANCE`.
2. Run the syntax and static checks from section 3.
3. Run the complete unittest suite from section 4.
4. Restart the changed service or containers:
   `systemctl --user restart aidir` and, when Docker files changed,
   `docker compose up -d --build`.
5. Run the smoke checks from section 5.
6. Exercise the changed protocol manually, including both success and
   expected-error paths.
7. Inspect `logs/core.log`, `logs/http.log`, `logs/worker.log`,
   `logs/webui.log`, and `logs/all.log` for startup errors, unhandled
   exceptions, unexpected retries, or leaked credentials.
8. Repeat a representative request after restart to verify that configuration
   reload and service recovery did not break task execution.

For scheduler, resource, or timeout changes, also verify a task through its
full lifecycle: `queued` -> `running` -> `completed` (or the intended
explicit error/timeout state). Confirm that Redis state, API output, logs, and
the WebUI agree.

For endpoint or response changes, test both non-streaming and streaming
requests where supported, plus an invalid request and an authentication
failure. For WebUI changes, test login, `/api/auth/me`, task list, task
viewer, logs, and WebSocket live updates.

## 7. Manual acceptance matrix

| Area | Acceptance checks |
|---|---|
| Installation | Fresh `./install.sh`, service activation, port checks, and restart |
| Configuration | Valid config loads; invalid values fail clearly; secrets remain in `.env` |
| OpenAIx/Ollama | Model listing, chat, embeddings where configured, non-streaming and streaming responses |
| MCP | `initialize`, `ping`, `tools/list`, and one safe `tools/call` |
| Queue | Priority ordering, queued/running/completed transitions, timeout and cancellation behavior |
| Routing | Direct provider/model route and smart route; expected fallback after an unavailable provider |
| Resources | Admission, reuse of already loaded resources, queue-state endpoint, and release after completion |
| WebUI | Login, status, tasks, viewer search, logs, config display, and live log updates |
| Recovery | Core restart with Redis retained; Docker service restart; no orphaned running tasks or leaked locks |
| Observability | Expected status codes and useful error messages; no request bodies, credentials, or tokens in logs |

Record the date, commit, configuration variant, commands, result, and relevant
log excerpts for failed checks. A test is passing only when its expected
status, response shape, and side effects all match the requirement.

## 8. Troubleshooting failed tests

- Check that the command is run from the repository root and that `venv` is
  activated.
- Confirm that `.env` and `config.json5` point to the intended instance.
- Check `systemctl --user status aidir` and `docker compose ps` before
  retrying live tests.
- Check Redis connectivity and whether another instance is using the same
  `AIDIR_INSTANCE` or ports.
- Read the relevant service log before restarting repeatedly; preserve the
  first traceback and HTTP status.
- If a test needs an external provider, distinguish provider failure from an
  AIDIR failure by checking the provider URL directly.

