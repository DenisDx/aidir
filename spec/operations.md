# aidir — Operations: installation, deployment, diagnostics

> Satellite document of [SPEC_NEW.md](../SPEC_NEW.md). See
> [§2.2 Runtime topology and ports](../SPEC_NEW.md#22-runtime-topology-and-ports)
> there for the architecture context.
>
> This document describes the installation, the systemd and Docker deployment, the
> `doctor`, the cron cycle, the logging and audit retention, and the operational
> part of the test protocol.

---

## 1. Requirements

| Dependency | Notes |
|---|---|
| Python 3.11+ | with `python3-venv` |
| Docker + Docker Compose | for Redis and nginx |
| A `crontab` | optional — `cron.embedded` replaces it |

The prerequisites installer handles the system packages:

```bash
./install_prerequisites.sh
```

It detects the package manager (`apt`, `dnf`, `yum`, `pacman`, `zypper`, `apk`),
ensures Python and `python3-venv`, installs Docker and Docker Compose (the plugin,
then `docker-compose-v2`, then the standalone binary), and starts the Docker daemon.

The manual equivalent for Ubuntu:

```bash
sudo apt update && sudo apt install -y ca-certificates curl gnupg python3 python3-pip python3-venv
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

The last step requires a re-login.

---

## 2. Installation

### 2.1 The command

```bash
git clone --recurse-submodules https://github.com/DenisDx/aidir.git
cd aidir
./install.sh
```

`aiset` is a Git submodule. `--recurse-submodules` fetches it with the rest; if the
clone omitted the flag, `install.sh` performs `git submodule update --init
--recursive` itself.

An optional template argument selects the configuration template:

```bash
./install.sh                    # uses config.json5.example
./install.sh config             # the same
./install.sh prod               # uses prod.json5.example
./install.sh prod.json5.example # an explicit template filename
```

A name without a dot gets `.json5.example` appended; a name with a dot is used
verbatim; an absolute path is used as-is.

**Note:** `prod.json5.example` does not exist in the repository; only
`config.json5.example` is shipped. `./install.sh prod` aborts with
`require_file: ... not found`.

There are no other flags. `install.sh` has no non-interactive mode: the fresh
install asks for the WebUI password, and a re-install with an occupied port asks
for a confirmation.

### 2.2 The steps

| # | Step | What happens |
|---|---|---|
| 1 | Prerequisites | Docker, Python, `crontab`, `python3-venv` are checked; the Docker daemon is started |
| 2 | The aiset submodule | `git submodule update --init --recursive`; a missing checkout in a non-Git tree is a warning, not an abort |
| 3 | The configuration | `config.json5` is created from the template when it is missing, or replaced when a template argument is given. The backups rotate: `.bak` → `.bak.bak` → `.bak.bak.bak` (three generations) |
| 4 | `.env` | Created from `.env.example`; `AIDIR_ROOT` is set; the WebUI password is prompted for twice (or generated and printed); `chmod 600` |
| 5 | The virtual environment | `python3 -m venv venv`, then `pip install -r requirements.txt` |
| 6 | Docker | `docker compose build` (with a `--no-cache` retry), `down --remove-orphans`, `up -d` |
| 7 | The ports | Five listeners are probed; the accepted ones are reserved so two aidir listeners can never share a host port |
| 8 | Cron | The crontab entry is installed idempotently |
| 9 | systemd | The unit is written, enabled and restarted; four HTTP health checks follow |
| 10 | The report | The URLs, the `.env` values to verify, the useful paths and five recommended checks |

### 2.3 The port policy

| Listener | Variable | Default |
|---|---|---|
| Redis | `REDIS_HOST` / `REDIS_PORT` | `127.0.0.1:6379` |
| nginx | `NGINX_HTTP_PORT` | `8080` |
| The WebUI backend | `WEBUI_HOST` / `WEBUI_PORT` | `0.0.0.0:20082` |
| The OpenAIx endpoint | `OPENAIX_ENDPOINT_HOST` / `OPENAIX_ENDPOINT_PORT` | `0.0.0.0:21434` |
| The Anthropic endpoint | `ANTHROPIC_ENDPOINT_HOST` / `ANTHROPIC_ENDPOINT_PORT` | `0.0.0.0:21435` |
| The MCP endpoint | `MCP_ENDPOINT_HOST` / `MCP_ENDPOINT_PORT` | `0.0.0.0:20001` |

The availability probe binds a socket to **every** resolved address, so both IPv4
and IPv6 must be free.

| Situation | Behavior |
|---|---|
| A fresh install, the port is occupied | The next free port is chosen and written to `.env`; a warning is printed |
| A re-install, the port is occupied, a TTY is present | The suggested port is offered; `n` cancels the install |
| A re-install, the port is occupied, no TTY | The installer **fails** rather than changing a configured port without consent |
| No free port at all | The installer fails |

Before probing, the installer stops **only the current instance's** service — it
matches the unit file by the exact `WorkingDirectory=` line, so a service belonging
to another checkout is left alone. A system-scope service must be stopped with
`sudo` first.

`NGINX_HTTP_PORT` must differ from `WEBUI_PORT`. The installer does not validate
this explicitly, but the port reservation makes a collision detectable: the second
one is seen as occupied.

### 2.4 The `.env`

`.env` is git-ignored and holds the secrets and the machine-specific values. The
active variables:

| Group | Variables |
|---|---|
| Instance | `AIDIR_INSTANCE` (default `aidir1`), `AIDIR_ROOT` (required) |
| Redis | `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` |
| Endpoints | `OPENAIX_ENDPOINT_PORT` / `_HOST`, `ANTHROPIC_ENDPOINT_PORT` / `_HOST`, `MCP_ENDPOINT_PORT` / `_HOST` |
| Upstream | `OLLAMA_BASE_URL` |
| WebUI | `WEBUI_PORT`, `WEBUI_HOST`, `NGINX_HTTP_PORT`, `ROOT_USER`, `ROOT_PASSWORD` |
| Logging | `LOG_WIPE_PERIOD`, `LOG_WIPE_MAX_AGE` |
| Cron | `CRON_PERIOD` |
| Timeouts | `TASK_RESTART_WAIT_TIMEOUT_SECONDS`, `TASK_QUEUE_TIMEOUT_SECONDS`, `TASK_RUN_TIMEOUT_SECONDS`, `EXTERNAL_TASK_LIVE_SECONDS`, `REQUEST_TIMEOUT` |
| WebUI timeouts | `WEBUI_TIMEOUT_OLLAMA_CHAT`, `WEBUI_TIMEOUT_MCP_PROXY`, `WEBUI_TIMEOUT_MODEL_LIST`, `WEBUI_TIMEOUT_AGENT_TEST` |

Optional (commented out in `.env.example`): `TLS_CERT_PATH`, `TLS_KEY_PATH`,
`TLS_PORT`, `TLS_PORT_EXTERNAL`, `DASHSCOPE_API_KEY`, `BRAVE_APIKEY`,
`SEARXNG_HOST`, `SEARXNG_PORT`, `EXAMPLE_API_BASE_URL`, `EXAMPLE_API_KEY`.

---

## 3. Deployment

### 3.1 The systemd unit

`install.sh` generates the unit. In **user** mode (the default for a non-root
installation):

```ini
[Unit]
Description=AI Director core service
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/denis/aidir
ExecStartPre=/usr/bin/docker compose -f /home/denis/aidir/docker-compose.yml up -d
ExecStart=/home/denis/aidir/venv/bin/python /home/denis/aidir/core/app.py
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aidir

[Install]
WantedBy=default.target
```

In **system** mode (root): `After=network.target docker.service`,
`Requires=docker.service`, `WantedBy=multi-user.target`.

Notes:

- `TimeoutStopSec=120` matches the default
  `TASK_RESTART_WAIT_TIMEOUT_SECONDS=120`. Changing the environment variable does
  **not** change the unit — re-run `install.sh` to regenerate it.
- `ExecReload` is the SIGHUP config reload.
- In user mode the installer runs `loginctl enable-linger "$USER"` so the service
  survives logout.
- `ExecStartPre` brings the Compose stack up before the Python process starts.

### 3.2 The commands

```bash
systemctl --user status aidir
systemctl --user restart aidir
systemctl --user reload aidir          # SIGHUP: re-read the configuration
journalctl --user -u aidir -f
```

### 3.3 Docker Compose

```yaml
services:
  redis:
    image: redis:7-alpine
    container_name: aidir_redis
    restart: unless-stopped
    command: redis-server ${REDIS_PASSWORD:+--requirepass $REDIS_PASSWORD}
    ports: ["${REDIS_HOST:-127.0.0.1}:${REDIS_PORT:-6379}:6379"]
    volumes: ["./docker/volumes/redis:/data"]
    healthcheck: ["CMD", "redis-cli", "ping"]

  nginx:
    build: ./nginx
    container_name: aidir_nginx
    restart: unless-stopped
    ports: ["${NGINX_HTTP_PORT:-8080}:80"]
    volumes: ["./webui/frontend:/usr/share/nginx/html:ro"]
    environment: { WEBUI_PORT: "${WEBUI_PORT:-20082}" }
    extra_hosts: ["host.docker.internal:host-gateway"]
    depends_on: [redis]
```

The `--requirepass` flag is added only when `REDIS_PASSWORD` is non-empty. The host
bind defaults to loopback.

**The nginx container reaches the WebUI backend through `host.docker.internal`**
(the host gateway). The backend is not a container — it is a uvicorn server inside
the host-run `core/app.py`.

`WEBUI_HOST` is **not** passed to the container and `nginx.conf` does not reference
it; `nginx/entrypoint.sh` exports it as a default, but the value is inert today.

There is no healthcheck on the nginx service, and `depends_on` uses the short list
form, so nginx is not gated on Redis being healthy.

### 3.4 nginx

The image is `nginx:1.27-alpine`. The entrypoint renders
`/etc/nginx/templates/nginx.conf.template` with `envsubst` and runs `nginx -t`
before serving.

The location blocks and their rationale are in
[spec/protocols.md §7](../spec/protocols.md#7-webui-asset-and-proxy-rules). The two
load-bearing rules:

- every proxied prefix needs `^~`, because a regex location beats a plain prefix
  location;
- a missing file that names an extension must be a 404, not the SPA page.

**Check the proxy, not only the service.** The Compose file mounts
`./webui/frontend` read-only, so a JavaScript edit appears immediately, while a
configuration change inside the image needs `docker compose build nginx` first.

### 3.5 Uninstalling

```bash
./uninstall.sh           # remove the service, the cron entry and the containers
./uninstall.sh --purge   # also remove the venv, .env, the logs and the Redis dump
```

Always removed: the user service, the system service, the crontab lines carrying
the `# aidir-cron` marker, and the Compose stack.

Always kept: `config.json5` and its backup chain, the `docker/volumes/redis`
directory, the source tree and the `aiset` checkout.

`--purge` additionally removes `venv/`, `.env`, `logs/*.log`, `logs/*.jsonl` and
`docker/volumes/redis/*.rdb`. The default uninstall keeps the Redis dump, which on
a busy instance is over a gigabyte.

---

## 4. The doctor

### 4.1 The command

```bash
./doctor.sh                       # everything
./doctor.sh --config-only         # only the configuration documents
./doctor.sh --quick               # the quick subset
./doctor.sh --fix                 # the safe repairs
./doctor.sh --fix-all             # also the visible and lossy repairs
./doctor.sh --fix-all --root      # also the privileged repairs
./doctor.sh --dry-run --fix-all   # show the plan, write nothing
./doctor.sh --json                # machine-readable
./doctor.sh --no-color            # no ANSI colors
./doctor.sh --only redis,ports    # a subset by id
./doctor.sh --help                # every option, with its explanation
```

`doctor.sh` is a wrapper around `venv/bin/python core/app.py doctor` and forwards
every argument, so it works from any directory. The long form is equivalent.

**The doctor runs even when aidir refuses to start** — which is the case it exists
for. `core/app.py` therefore dispatches the `doctor` sub-command *before* importing
`core.config`, because that module raises at import when a document is invalid.

### 4.2 The checks

In execution order:

| # | Id | What it checks | Repair |
|---|---|---|---|
| 1 | `aiset` | The library is importable | `git submodule update --init --recursive` (safe) |
| 2 | `config` | Every document against its descriptor, through the `aiset` doctor | `clamp` (safe), `coerce` / `drop_unknown` (lossy) |
| 3 | `env-file` | `.env` exists and parses | — (the installer owns it) |
| 4 | `env-root` | `AIDIR_ROOT` points at this checkout | — |
| 5 | `dirs` | `logs/`, `logs/audit/` writable; `docker/volumes/redis` present | create the missing directories (safe) |
| 6 | `store-lock` | The store lock is not held by another process | — |
| 7 | `venv` | `venv/bin/python` and `pip` exist; `fastapi`, `redis`, `uvicorn` import | `pip install -r requirements.txt` (safe) |
| 8 | `docker` | `docker info` | `systemctl start docker` (privileged) |
| 9 | `compose` | `docker compose config -q` parses with the current `.env` | — |
| 10 | `redis` | The multi-layer Redis diagnosis (below) | start / recreate / `chown` / restart Docker |
| 11 | `nginx` | The nginx container runs | `docker compose up -d nginx` (safe) |
| 12 | `ports` | The WebUI / OpenAIx / MCP / nginx ports are free or owned by aidir | — |
| 13 | `logs` | `logs/` exists and accepts a write probe | — |
| 14 | `ollama` | `GET {baseUrl}/api/tags` answers | — (the doctor never starts Ollama) |
| 15 | `systemd` | The unit's state, the restart count, the crash-loop detection | stop / start (privileged) |

`--config-only` runs only `aiset` and `config`. It touches no container, port or
network, so it is safe while aidir keeps running.

### 4.3 The fix classes

| Class | Requires | Examples |
|---|---|---|
| `SAFE` | `--fix` or `--fix-all` | Create a missing directory, install the venv dependencies, start the Redis container |
| `VISIBLE` | `--fix-all` | Recreate a container |
| `LOSSY` | `--fix-all` | Drop a key the descriptor does not declare |
| `PRIVILEGED` | `--root` | Start the Docker daemon, `chown` the Redis volume, restart the unit |

A `--dry-run` writes nothing. Because the library does not build a plan with `fix`
off, the dry run derives the plan from the annotated issues.

The configuration repair classes are `clamp` (move a number to its declared
min/max), `coerce` (turn `"8080"` into `8080` for an unambiguous literal) and
`drop_unknown` (remove the keys the descriptor does not declare).

**The document is written only when the repairs make it fully valid.** A partial
repair of an invalid file is reported, not stored.

### 4.4 The Redis diagnosis

Redis is diagnosed per layer, because each layer has a different fix:

| Observation | Cause | Repair |
|---|---|---|
| Connection refused, no container | The container is not running | Start it (safe) |
| Connection refused, the container exists | Redis is down or crash-looping | Recreate it; if the log says *permission denied*, `chown` the volume (privileged) and recreate |
| `redis inside the container: pong`, but the host still times out | The host port mapping is broken (a WSL/Docker proxy problem) | Restart Docker (privileged) |
| `redis inside the container: not answering` | Redis is down | Read the log |
| `address already in use` | A **host** process owns the port | Free it, or move `REDIS_PORT` with `./install.sh` |

The report names the port holder (`docker-proxy (pid N)`, `redis-server (pid N)`, or
"pid not visible"). When a foreign process holds the port, `--fix-all` does **not**
destroy the container: recreating it cannot succeed and the container may still be
usable.

The check also reports the container state (`running`, `restarting`, `exited (1)`,
plus the health) and its last log lines. A crash loop and a broken port forward
look identical from outside — both time out — and only the log or the state tells
them apart.

### 4.5 The restart loop

When aidir itself is in a restart loop (`activating` / `auto-restart`), the report
shows the unit scope, its state and the restart count, and says so instead of
claiming the service is running.

With `--fix-all` the unit is stopped **before** the dependencies are repaired and
started again at the end — but only if nothing else is still failing, because
restarting it earlier would rebuild the same loop. Without `--fix-all` the report
prints the `systemctl stop` command to run first.

### 4.6 The output

```
<id>          ok | warn | fail   <message>
                                <detail>
applied:
  <label>
failed repairs:
  <label> -> <error>
skipped:
  <label> (<class>)
recommendations:
  <id>: <text>

RESULT: all checks passed                  green
RESULT: all checks passed, N warning(s)    amber
RESULT: N problem(s) remain                red
```

Color is used only when stdout is a terminal; `--no-color` or `NO_COLOR=1`
disables it.

Exit codes: `0` healthy or warnings only, `1` failures remain, `2` repaired but
issues remain.

`--json` emits `{schema_version, ok, checks[], aidir_running, service, applied,
failed, skipped, exit_code}`.

### 4.7 Safety while running

- The configuration read and the repair write go through the same store lock and
  revision check as any other write, so a concurrent write is reported as `skipped`
  instead of being overwritten.
- The ports already held by the running process are recognized as its own.
- **A repaired configuration is not applied by the running process.** Reload it
  (`systemctl --user reload aidir`, SIGHUP) or restart. The report says so.
- Reinstalling the virtual environment is refused while the service runs.
- `--fix-all` may recreate Redis or nginx; the running process notices and
  reconnects, and the report says that before the change.

---

## 5. The cron cycle

### 5.1 The two modes

| Mode | How |
|---|---|
| System crontab | The installer adds one line with the marker `# aidir-cron` |
| Embedded | `cron.embedded: true` in `config.json5` — the cycle runs inside the aidir process, every 60 s |

The crontab line:

```
* * * * * [ $(( $(date +\%s) / 60 % <CRON_PERIOD> )) -eq 0 ] && <venv>/bin/python <root>/core/cron.py   # aidir-cron
```

The entry fires every minute and the shell guard lets the script run only on the
period boundary, which supports any positive period without a helper script.

The WebUI's canonical form also redirects the output:
`... >> logs/cron.log 2>&1 # aidir-cron`. The WebUI can repair the crontab
safely, and the health panel distinguishes the two modes.

**WSL without cron:** set `cron.embedded: true`.

### 5.2 The jobs

| Job | What it does | Interval |
|---|---|---|
| `run_loop_workers_cycle` | Calls `loop()` on every worker that exposes it, rotating the start index (persisted in `{ns}:cron:loop_workers:start`) | Every cycle |
| `refresh_external_mcp_tools` | Re-reads the remote MCP tool list | 60 s |
| `wipe_logs` | Removes the log lines older than `wipe_max_age` | `logging.wipe_period` |
| `trim_logs_by_size` | Trims a log file to `max_log_size`, **in place** so the inode survives | `logging.trim_period` (default 60) |
| `retain_audit_logs` | Reconciles the interrupted partitions, then applies the retention | `logging.audit.retention_period` (default 3600) |
| `health_check` | `redis.ping()` | 300 s |
| `cleanup_stale_tasks` | **An empty stub** — it needs a task-worker heartbeat mechanism | — |
| `cleanup_expired_tasks` | Removes the terminal external task hashes older than `external_task_live` | `max(10, cron_period)` |
| `keep_alive_ping` | Pings the Ollama models / restarts the managed llama.cpp servers that are still wanted | Every cycle |

Each job is isolated: one failure never stops the cycle. When every job succeeds,
the cycle writes the heartbeat `{ns}:cron:last_success_at`.

### 5.3 The keep-alive ping

The ping scans `{ns}:resource:*:activity:*`.

- The record is **deleted** when `now - last_used_at >= keep_alive`: the model is
  no longer wanted.
- The ping is skipped when `now - last_ping_at < keep_alive_period`.
- For an **Ollama** provider: `POST {baseUrl}/api/generate {"model": …,
  "keep_alive": keep_alive_period * 2}` with a 15 s timeout.
- For a **llama-cpp** provider: there is no HTTP ping. Instead
  `LocalServerManager.ensure_running(provider_id)` checks `/health` and starts the
  process when it is unhealthy. A failure is logged and the record is skipped.

After a ping, `last_ping_at` is updated and **`last_used_at` is preserved** — a
ping cannot keep a model warm forever.

---

## 6. Logging

### 6.1 The files

| File | Content |
|---|---|
| `logs/all.log` | The shared aggregate |
| `logs/system.log`, `logs/core.log`, `logs/worker.log`, `logs/webui.log`, `logs/audit.log`, `logs/http.log`, `logs/hooks.log` | Per-subsystem |
| `logs/cron.log` | The cron stdout/stderr |
| `logs/<worker_id>_call_log.jsonl` | The compact per-worker LLM call summaries |
| `logs/llama_cpp_servers.json` | The managed llama-server PID records |
| `logs/local_llama_cpp.log` | The managed server's own output |

The actual logger types are `system`, `core`, `worker`, `webui`, `audit`, `http`
and `hooks`.

### 6.2 The levels

The journald scale: `0 EMERG`, `1 ALERT`, `2 CRIT`, `3 ERROR`, `4 WARN`,
`5 NOTICE`, `6 INFO`, `7 DEBUG`. `WARNING`→4, `INFORMATION`→6, `TRACE`→7; an
unknown name falls back to `INFO`.

The line format is `<iso-ts> [LEVEL] [type:tag] message`. The timestamp timezone
comes from `logging.timezone` (`local`, `utc` or an IANA name).

### 6.3 The thresholds

| File | Resolution order |
|---|---|
| `logs/<type>.log` | `workers.items.<tag>.logging.level` → `middleware.items.<tag>.logging.level` → `endpoints[].logging.level` → `logging.levels.<type>` → `logging.level` |
| `logs/all.log` | `workers.items.<tag>.logging.alllevel` → `endpoints[].logging.alllevel` → `logging.alllevels.<type>` → `logging.level` |

**The shipped example uses the plural `workers` while the logger type is the
singular `worker`.** `logging.levels.workers` and `logging.alllevels.workers`
therefore never take effect; `worker.log` falls back to the global level unless a
per-worker override exists.

### 6.4 The retention

There is no rename/size rotation. Retention is by age and by size:

- **Wipe** (age): the lines whose leading timestamp is older than the cutoff are
  removed. A `.log` line uses its first space-separated token; a `.jsonl` line
  parses the JSON and uses its `ts` field. An unparsable line is **kept**.
- **Trim** (size): the file is rewritten **in place** with its tail, so the inode
  — and any open writer — survives. The partial first line is discarded.

Per-file overrides are read from `logging.logs` (or `logging.log_overrides`), keyed
by the file name or its stem: `wipe_period`, `wipe_max_age`, `trim_period`,
`max_log_size`. **That map is not declared in the descriptor**, so it cannot be set
through the settings editor.

---

## 7. The audit storage

### 7.1 The layout

```
logs/audit/
├── audit-index.sqlite            # the disposable index (WAL)
├── raw_client_requests-YYYY-MM-DD.jsonl
├── raw_client_responses-YYYY-MM-DD.jsonl
├── raw_llm_requests-YYYY-MM-DD.jsonl
├── raw_llm_responses-YYYY-MM-DD.jsonl
├── rejected_requests-YYYY-MM-DD.jsonl
├── tasks-YYYY-MM-DD.jsonl        # the terminal task records
├── files/YYYY-MM-DD/<uuid>       # the file-backed bodies and the data-URI attachments
└── .spool/<uuid>                 # the in-flight stream spools (transient)
```

The journals are **append-only** and the index is **disposable**: `rebuild_index()`
reconstructs it from the journals, skipping the corrupt lines. The index rows are
written after the journal append, so the index never points at bytes that were not
written.

### 7.2 The limits

| Limit | Default | Meaning |
|---|---|---|
| `max_inline_body_bytes` | 1 MiB | Above this, the body is file-backed |
| `max_total_bytes` | 1 GiB | The soft total |
| `emergency_max_total_bytes` | 2 GiB | The hard total, used only when the normal retention cannot keep the storage within it |
| `retention_days` | 30 | The age of a partition |
| `max_spool_age_seconds` | 86400 | A stale spool is removed |

A partition is removed when:

- its age is at least `max(2, retention_days)`;
- the total exceeds `max_total_bytes` **and** the partition is at least two days
  old;
- the emergency limit is exceeded **and** the partition is not today.

**Today is never deleted.** The deletion state is persisted before the filesystem
is touched, so an interrupted deletion is resumed by `reconcile_partitions()` and
the events of a `deleting` partition are hidden from the lookups.

An `ENOSPC` during a write triggers the forced emergency retention immediately.

### 7.3 The writer

The writer runs on its own thread (`aidir-audit-writer`) with a bounded queue
(`logging.audit.writer_queue_size`, default 256).

| Policy | Behavior |
|---|---|
| `best_effort` (default) | A full queue **drops** the event. The drop is counted, kept in a 300-second window, and turns the health status into `warning` (or `error` from ten drops) |
| `durable` | The caller blocks until the writer persists the event; a writer exception is re-raised in the caller |

Every event gets a receipt. The terminal task record is written only after all of
that task's receipts have settled, so the terminal record never references an event
that was not persisted.

### 7.4 The SSE normalization

An audit-only transformation. A completed, error-free OpenAI SSE response is
reassembled into one final JSON completion:

```jsonc
{"body_format": "sse", "data_encoding": "json", "sse_chunk_count": 58,
 "data": { "object": "chat.completion", "choices": [ ... ] }}
```

The assembler merges the `choices` by index, concatenates the `content`, the
`reasoning` and the `reasoning_content`, merges the `tool_calls` by stream index
(concatenating the `function.arguments`), keeps the last `finish_reason`, and
merges the `usage` and the other compatible top-level fields.

The comments and the `[DONE]` sentinel are **not** counted in `sse_chunk_count`.

The normalization is refused when the terminal status is `failed`, `cancelled`,
`timeout`, `http_error`, `invalid_json` or `exception`, when an `error_code` is
present, or when the HTTP status is ≥ 400. A stream that cannot be unambiguously
assembled stays one raw string.

**Client delivery is never affected.** A large SSE body also keeps a link to its
original body file.

### 7.5 The secret scrubbing

The metadata envelope is scrubbed: a value whose key is in the secret set becomes
`"[REDACTED]"`. The built-in key set is `authorization`, `cookie`, `set-cookie`,
`token`, `api_key`, `api-key`, `x-api-key`, `x-opencode-session`, `password`,
`secret`; it is extended by `logging.audit.secret_metadata_keys`.

**The body is not scrubbed.** A secret inside a body stays in the body.

---

## 8. The operational checks

### 8.1 The listeners

```bash
ss -ltnp | egrep ':(8080|20082|21434|21435|20001|6379|11434)\b'
```

### 8.2 The endpoints

```bash
curl -s -o /dev/null -w 'webui_public:%{http_code}\n' http://127.0.0.1:${NGINX_HTTP_PORT}
curl -s -o /dev/null -w 'webui_auth_me:%{http_code}\n' http://127.0.0.1:${NGINX_HTTP_PORT}/api/auth/me   # 401
curl -s -o /dev/null -w 'openaix_health:%{http_code}\n' http://127.0.0.1:${OPENAIX_ENDPOINT_PORT}/health
curl -s -o /dev/null -w 'anthropic_health:%{http_code}\n' http://127.0.0.1:${ANTHROPIC_ENDPOINT_PORT}/health
curl -s -o /dev/null -w 'mcp_health:%{http_code}\n' http://127.0.0.1:${MCP_ENDPOINT_PORT}/health
curl -s http://127.0.0.1:${MCP_ENDPOINT_PORT}/mcp -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
redis-cli -h ${REDIS_HOST} -p ${REDIS_PORT} ping
```

### 8.3 The queue

```bash
docker exec aidir_redis redis-cli ZCARD aidir1:queue:agent
docker exec aidir_redis redis-cli ZCARD aidir1:queue:tool
```

### 8.4 The dependencies

The runtime dependencies are pinned exactly:

```
fastapi==0.115.12
uvicorn[standard]==0.34.2
redis[asyncio]==5.3.0
httpx==0.28.1
cryptography==46.0.3
json5==0.10.0
```

`json5` is **test-only**: the parity reference reader must parse the configuration
with a parser the service does not use, or the parity check would only prove that
one parser agrees with itself. The service uses the `aiset` JSON5 parser.

### 8.5 The test protocol

```bash
source venv/bin/activate
python -m unittest discover -s . -p 'test_*.py' -v
```

The static checks:

```bash
python -m compileall -q core workers webui smoke_test.py
git diff --check
```

The five levels — static, unit, smoke, regression, manual acceptance — and the
acceptance matrix are in [../TEST.md](../TEST.md).

**Two reminders from the record:**

- The browser acceptance of the settings editor must be done **through nginx** on
  `http://localhost:8080`, not at port 20082. The backend and the proxy can
  disagree about what a URL returns, and only the proxy is what the browser uses.
- A test can pass for the wrong reason. Put the value where the code actually
  reads it, and confirm every new assertion by reintroducing the defect.

---

## 9. Common failures

| Symptom | Likely cause | Where to look |
|---|---|---|
| aidir does not start | An invalid configuration document | `./doctor.sh --config-only` |
| `curl` to the WebUI returns the login page instead of JSON | The request went through nginx but the prefix is not forwarded | `nginx/nginx.conf` — the `^~` prefixes |
| A missing script fails with "the global is undefined" | The asset URL is not under a forwarded prefix, or the SPA fallback answered it | `webui/frontend/index.html` — the script URLs; `nginx/nginx.conf` — the extension rule |
| The settings editor does not mount | The editor is initialized before the authentication | `webui/frontend/app.js` — `initSettingsBackend()` must be called from `onLoggedIn()` |
| The settings document selector is empty | `/api/settings-info` answered 401, or the response has no `.data` envelope | `apiGet` returns the parsed body or `null` |
| Redis is unreachable | The container is down, or the port mapping is broken | `./doctor.sh --only redis` |
| A task sits in `queued` forever | No worker matches the task type, or the resources are insufficient | The scheduler's deferral warnings in `logs/core.log` |
| The queue-state endpoint is slow | It is `O(queued tasks)` with one `HGETALL` per task | Reduce the retained task history (`tasks.external_task_live`) |
| The worker log is missing | `logging.levels.workers` uses the plural while the logger type is `worker` | Rename the key to `worker`, or set a per-worker override |
| An envid context never expires | `envids.store_duration_minutes` is declared but read by nobody | A known gap — see [§14](../SPEC_NEW.md#14-known-problems-open-tasks-and-development-directions) |
