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

# Settings subsystem (descriptors and the aiset host adapter)
python -m unittest -v \
  test_config_descriptor \
  test_settings_backend \
  test_settings_parity \
  test_frontend_boot_order \
  test_settings_styles \
  test_aiset_path \
  test_doctor
```

`test_doctor.py` covers the host diagnostic: a drifted configuration key is
reported with its fix class and removed only through `--fix-all`, a dry run writes
nothing, a missing directory is created by `--fix`, and an unreachable Redis is
reported within the socket timeout. It also checks the restart-loop detection
(`activating/auto-restart`, active unit preferred over an inactive one), that the
service is not started again while something still fails, `--help`, and the
`doctor.sh` wrapper.

`test_settings_styles.py` checks the design contract against the library's own
published inventory (`aiset.classes()`): every class is painted by the host or the
library, every `--aiset-*` variable the library reads is set by the host, the host
reintroduces no literal palette, the host sheet loads last, no rule escapes the
editor, and no host control carries an inline style. It needs `node`.

`test_frontend_boot_order` runs the real front-end sources in Node against a
minimal DOM, in the order `index.html` loads them, and reports the page before
and after sign-in. HTTP tests cannot cover this: their requests are already
authenticated, so an editor that initialises before the login still looks
correct. The suite covers a boot helper that was called but not defined, an
editor initialised before authentication, and the document list read from a
response envelope `apiGet` never produces. It is skipped when `node` is absent.

`test_settings_parity` compares the running backend against an independent
reference reader over the same `config.json5` and the same `.env`: every leaf,
every value, and every merged worker entry. It is the automated half of the dual
run described in `plan_aiset_integration.md`.

### 4.1 Settings descriptor gate

The descriptors are a contract, not documentation: aiset treats an undeclared
key as a hard error and then refuses to load the whole document. Run these
before every change that touches `config.json5`, `config.json5.example`, a
worker `config.json5`, or any `descriptor.json5`.

```bash
# 1. Descriptor grammar: no unknown property, no malformed node
for f in config.types.json5 config.descriptor.json5 \
         workers/agent/call_ollama/descriptor.json5 \
         workers/agent/openaix/descriptor.json5 \
         workers/context/context_builder/descriptor.json5 \
         workers/tool/http_api/descriptor.json5 \
         workers/tool/web_fetch/descriptor.json5 \
         workers/tool/web_search/descriptor.json5; do
  aiset/.venv/bin/aiset validate-descriptor "$f" --strict \
      --typedef config.types.json5 --format text
done

# 2. Both shipped root configurations and every worker file must load clean
python -m unittest -v test_config_descriptor
```

Expected: every descriptor prints `valid: True`, and the test run ends with
`OK`. A failure names the exact configuration path that the descriptor does not
cover; add the missing declaration to the descriptor, do not relax the test.

## 5. Smoke test after startup or deployment

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
| WebUI | Login, status, tasks, viewer search, logs, and live log updates |
| Settings (aiset) | See the manual steps in section 7.1 |
| Recovery | Core restart with Redis retained; Docker service restart; no orphaned running tasks or leaked locks |
| Observability | Expected status codes and useful error messages; no request bodies, credentials, or tokens in logs |

### 7.0 Manual acceptance: startup hint and the doctor

| Step | Expected |
|---|---|
| Start aidir and read the journal | One `doctor:` line before the listeners bind, then the two-line hint naming `--help` and `doctor` |
| Run `./doctor.sh --help` | Usage with the `doctor` command, exit 0, even when the configuration is broken |
| Run `./doctor.sh` | One line per check; Redis, nginx and the configuration are all reported |
| Add an undeclared key to `config.json5`, run `./doctor.sh` | The key is reported as `drop_unknown`; nothing is written |
| Run `./doctor.sh --fix-all` | The key is removed, the report says how many repairs were applied, and the file still keeps its comments |
| Run `./doctor.sh --dry-run --fix-all` | The plan is printed and the file is unchanged |
| Keep aidir running, run `doctor` | Ports are reported as owned by the running instance, not as conflicts; the JSON has `"aidir_running": true` |
| Keep aidir running, repair a key with `doctor --fix-all` | The repair is written and the report says to reload aidir (`systemctl --user reload aidir`) |

### 7.1 Manual acceptance: the generated settings editor

The editor is always active; restart aidir, sign in, and open **Settings**.

What is already covered without a browser, and therefore needs no manual step:
that the three assets are served and loaded in dependency order, that every asset
the page names sits under a prefix nginx forwards, that every element the editor
mounts into exists, that every one of the seven documents returns a populated tree
snapshot, that a node carries its description, its comments, its `apply` policy and
its `secret` flag, and that a declared but unwritten setting is reported as absent
(`test_settings_backend.py`). The steps below are the ones a browser can answer and
a script cannot.

Open the page through nginx (`http://localhost:${NGINX_HTTP_PORT}`, port `8080` by
default), not straight at the WebUI backend: the failure that produced the asset
error of 2026-10-17 was visible only through the proxy, which answers a script
request outside the forwarded prefixes with the SPA page and status `200`. To
confirm the delivery path by hand:

```bash
curl -sI "http://localhost:${NGINX_HTTP_PORT:-8080}/api/settings/assets/aiset-tree.js" | head -2
# expect: HTTP 200 and Content-Type: application/javascript, not text/html
```

| Step | Expected |
|---|---|
| Open **Settings** | The document selector lists `main` and one entry per worker-local configuration; the tree renders with descriptions |
| Look at the tree | Every level is indented under its parent with a guide line, so what is inside what is visible; names do not touch the controls |
| Look at the badges | Each badge is a pill with a border and a colour by meaning (type, `reload`, `live`, `secret`, `dirty`); they are separated, not one run of text |
| Look at the toolbar | **Apply** is the primary control and stands out; Reload, Revert all, Expand all and Collapse all are quiet; hovering a row highlights it |
| Look at the right pane | The assistant pane shows `Model local-only · document main`; the tree and the pane scroll independently |
| Send "raise the log retention to 7 days" | A tool line appears, then an answer, then `1 change(s) staged`; the node shows a **dirty** badge and the file on disk is still unchanged |
| Press **Apply** | The staged change is committed atomically, comments and `${VAR}` placeholders intact |
| Send "print the redis password" | The assistant reports it is set but hidden; no value appears in the pane |
| Send a malformed request (empty message) | The pane reports the error; nothing is staged |
| Expand `logging` | Every node shows its type, its description, the file comments and the `apply` policy (`live`, `reload`, `restart`) |
| Change `logging.level` and press **Apply** | One atomic write; `config.json5` keeps its comments, quoting and every `${VAR}` placeholder; the status line reports the application state |
| Open the `.env` button on a node that holds `${VAR}` | The variable name and the inline fallback are editable separately, and the placeholder is not lost |
| Change a value in `redis.password` or `webui.auth.users[].password` | The value is stored as a placeholder; without the secret permission the whole subtree is denied, not blanked |
| Edit a worker document (`worker:tool:web_search`) | The same editor, the same rules, writing that worker's own `config.json5` |
| Press **Reload settings** | Every document is re-read; the effective values in the rest of the WebUI follow |
| Set a port (for example `webui.port`) and read the state | The node reports `restart_required`; the value is not live |
| Introduce a deliberate error (for example a port above 65535) and press **Apply** | The batch is refused with a path and a rule; the file is byte-for-byte unchanged |
| Press **Restart aidir** | The restart proceeds after the confirmation dialog |

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

