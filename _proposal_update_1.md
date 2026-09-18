# Proposal: Security Update Round 1

Date: 2026-09-17
Scope: static security review of the aidir codebase (core, webui, workers, nginx, install, config)
Constraint: this document is analysis + proposals only; no existing files are modified.

## 1. Summary

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| F1 | Critical | LLM/MCP endpoints open to network, auth optional | core/app.py, core/endpoints/* |
| F2 | Critical | WebUI exposes full config (secrets) read/write; no permission enforcement | webui/backend/app.py |
| F3 | Critical | Config write + `exec_cmd` = arbitrary command execution chain | core/local_server_manager.py |
| F4 | High | Plaintext WebUI password, weak defaults, no login rate limiting | config.json5, webui/backend/app.py |
| F5 | High | SSRF in web_fetch (loopback/private/metadata reachable) | workers/tool/web_fetch/app.py |
| F6 | High | WebUI served over plain HTTP; cookie without `secure`; token in WS query string | nginx/nginx.conf, webui/backend/app.py |
| F7 | Medium | Redis without password by default | config.json5, docker-compose.yml |
| F8 | Medium | Path traversal in /ws/logs (file param not validated) | webui/backend/app.py:505 |
| F9 | Medium | Unbounded client-supplied timeout in /api/test/agent | webui/backend/app.py:936 |
| F10 | Medium | World-readable logs containing task payloads | logs/ (permissions) |
| F11 | Low | config.json5 present in git history | git commits 1530bc6, ce8acf2 |
| F12 | Low | external_task_live default ~29 days | config.json5:852 |
| F13 | Low (design) | Prompt-injection surface via tools/MCP (no sandbox) | workers/agent/openaix, external_mcp |

---

## 2. Findings and Proposed Modifications

### F1. Endpoints bind to 0.0.0.0 and authentication is optional (CRITICAL)

**Evidence**
- `core/app.py:484,495,507` — `bindAddress` defaults to `"0.0.0.0"` for ollama, openaix, and mcp endpoints.
- `core/endpoints/endpoint_ollama.py:160-162` — `_authorize_and_apply_envid()` returns `None` (i.e. "authorized") when no `Authorization: Bearer` header is present.
- `core/endpoints/endpoint_mcp.py:74-129` — `/mcp` (tools/list, tools/call) has no authentication at all.
- `config.json5:840` — API user token falls back to a hardcoded weak default: `${API_USER_TOKEN:-test-token-123456789}`.

**Risk**
Anyone who can reach the host (LAN/Internet if port is forwarded) can:
- consume LLM capacity (cost/DoS),
- trigger agent tasks that invoke tools (web_fetch, web_search, http_api, external_mcp),
- read MCP tool list and call tools directly.

**Proposed modification**
1. In `core/app.py`, change the default `bindAddress` for the three endpoint builders from `"0.0.0.0"` to `"127.0.0.1"`. Reason: fail-closed posture; external access becomes an explicit opt-in in config.
2. Add an endpoint-level option, e.g. `"require_auth": true/false` (default `true`) in each endpoint config section. In `_authorize_and_apply_envid()`, when `require_auth` is true and no token is supplied, return 401. Reason: keeps backward compatibility for trusted LAN deployments while making the secure default mandatory auth.
3. Add the same bearer-token check to `Endpoint_mcp._handle_rpc` (reject with JSON-RPC error `-32001` / HTTP 401 when `require_auth` is true). Reason: the MCP endpoint currently allows unauthenticated tool invocation, which is the most direct attack path.
4. Remove the weak default token: change `${API_USER_TOKEN:-test-token-123456789}` to `${API_USER_TOKEN}` (empty when unset). In `_find_api_user_by_token()`, treat an empty configured token as "user disabled" (never match). Reason: a predictable token defeats authentication entirely; an unset secret must not silently become a valid credential.

**Validation**
- Unit test: request without token returns 401 when `require_auth` is true; with valid token returns 200.
- Unit test: MCP `tools/call` without token rejected.
- Integration: after reload, `ss -ltnp` shows endpoints on 127.0.0.1 by default.

---

### F2. WebUI leaks full config (with secrets) and allows full config writes; permissions never enforced (CRITICAL)

**Evidence**
- `webui/backend/app.py:531-539` — `GET /api/config` returns `core.config.raw()` (env-substituted, i.e. real secrets) and `GET /api/config/raw` returns the raw file text; both require only a session.
- `webui/backend/app.py:541-582` — `POST /api/config/raw` and `POST /api/config/fields` rewrite the whole config file for any session.
- `core/config.py:383-389` — `raw()`/`raw_text()` do not mask anything.
- `webui/backend/app.py:181,282` — `permissions` are stored in the session payload and returned by `/api/auth/me`, but no route checks them (grep for `permission` shows only these two lines).

**Risk**
- Any logged-in user reads every API key, Redis password, and WebUI password.
- Any logged-in user edits the config, which (see F3) can lead to code execution, and can restart the service or terminate tasks.

**Proposed modification**
1. Add a masking function in `webui/backend/app.py` that returns `core.config.raw()` with secret-bearing values replaced by `"***"` (paths: `*.apiKey`, `*.token`, `*.password`, `auth.headers.*`, `auth.authorization`, `redis.password`). Apply it in `GET /api/config`. Reason: the UI needs structure, not values; raw secrets should not be served by any endpoint.
2. Remove or protect `GET /api/config/raw`: if kept, it must require a dedicated permission (e.g. `config.raw`) AND must return the text with `${VAR}` placeholders restored (read the file text from disk and do not substitute). Reason: the on-disk file already contains placeholders; serving substituted text is the leak.
3. Introduce a permission check helper (e.g. `_require_permission(session, "config.write")`) and enforce it on: `POST /api/config/raw`, `POST /api/config/fields`, `POST /api/restart`, `POST /api/tasks/{id}/terminate`. Grant `["all"]` to the root user as today. Reason: the `permissions` field currently exists but has no effect — the RBAC is dead code, which is worse than no RBAC (it creates false confidence).
4. Blocklist dangerous keys in `POST /api/config/fields`: reject changes to `models.providers.*.exec_cmd`, `models.providers.*.auth`, `webui.auth`, `redis.password` from the UI (or require `config.raw`). Reason: these keys are the RCE/credential-rotation chain (F3); structured field editing should not be able to plant an executable command.

**Validation**
- Test: `GET /api/config` contains no value from `.env` (assert `"***"` at known secret paths).
- Test: user without `config.write` gets 403 on `POST /api/config/fields`.
- Test: `POST /api/config/fields` with key `models.providers.x.exec_cmd` returns 403/400.

---

### F3. Config write + `exec_cmd` = arbitrary command execution (CRITICAL)

**Evidence**
- `core/local_server_manager.py:44-75` — `exec_cmd` from provider config is split with `shlex.split` and executed via `asyncio.create_subprocess_exec` when the provider's `baseUrl` health check fails.
- Combined with F2 (any WebUI user can write config), this is a full chain: log in → edit `models.providers.<id>.exec_cmd` → trigger a task routed to that provider → command runs as the service user.

**Risk**
Remote (authenticated) code execution on the host running aidir.

**Proposed modification**
1. Defense in depth on top of F2.4: additionally validate `exec_cmd` shape at config load — allow only an absolute path to an existing executable as the first token (e.g. must start with `/` or `~/`, resolved file exists and is executable). Log and skip providers with invalid `exec_cmd` instead of failing silently. Reason: even a legitimate operator editing config should not be able to plant `/bin/sh -c ...` style payloads; llama.cpp launch commands are always a single binary path plus args.
2. Optionally restrict provider ids that may carry `exec_cmd` to those with `api == "llama_cpp"` (or an explicit `"local_server": true` flag). Reason: limits the surface to the one feature that needs it.
3. Note: `shlex.split` already prevents shell metacharacter injection; the remaining risk is choosing *which* program runs, which items 1-2 address.

**Validation**
- Test: provider with `exec_cmd: "/bin/echo hi"` (relative/invalid) is rejected at startup and logged.
- Test: provider with absolute path to a missing binary fails with `INVALID_EXEC_CMD`.

---

### F4. Plaintext WebUI password, weak defaults, no login rate limiting (HIGH)

**Evidence**
- `config.json5:817-818` — `"login": "${ROOT_USER:-admin}", "password": "${ROOT_PASSWORD:-changeme}"`.
- `webui/backend/app.py:165-172` — `_find_user()` compares plaintext passwords from config.
- `webui/backend/app.py:253-269` — `/api/auth/login` has no rate limiting, no lockout, no delay.

**Risk**
- Offline: anyone with config access gets the password (mitigated by F2).
- Online: unthrottled brute force, especially over plain HTTP (F6). The `changeme` default is a likely real value on fresh installs.

**Proposed modification**
1. Support hashed passwords in `webui.auth.users`: if the `password` value starts with `sha256$`, compare `sha256(stored_hex) == sha256(input)` using `hmac.compare_digest`. Keep plaintext support for backward compatibility but log a startup warning when a plaintext entry is detected. Reason: passwords stop being readable from the config file and from `/api/config` (defense in depth with F2).
2. Add per-IP throttling to `/api/auth/login`: e.g. max 5 failures per 15 minutes per client IP, tracked in Redis (`aidir:login_fail:{ip}` with TTL). Return 429 when exceeded. Reason: standard anti-brute-force measure; Redis is already a dependency and provides the store.
3. Change the config default from `${ROOT_PASSWORD:-changeme}` to `${ROOT_PASSWORD}` (no default) and, at startup, fail (or log critical) when the resolved WebUI password is empty. Reason: a default credential is a guaranteed weak login.
4. (Optional, follow-up) add `secure` to the session cookie — see F6.

**Validation**
- Test: 6th failed login from the same IP returns 429.
- Test: `sha256$...` password entry authenticates correctly.

---

### F5. SSRF in web_fetch (HIGH)

**Evidence**
- `workers/tool/web_fetch/app.py:446-448` — validation only checks that the URL has a scheme and netloc.
- `workers/tool/web_fetch/app.py:392-397` — direct page fetch with `follow_redirects=True`, no host restrictions.

**Risk**
An LLM (possibly steered by prompt injection in fetched/searched content, F13) can be made to fetch `http://127.0.0.1:<redis/aidir/...>`, `http://169.254.169.254/...` (cloud metadata), or other internal hosts. The response text is returned into the task result and ultimately to the end user → data exfiltration.

**Proposed modification**
1. Add a `_is_forbidden_url(url)` guard in `WebFetchWorker.execute()` (and reuse it in web_search if it does direct fetches):
   - resolve the hostname (all A/AAAA records) and reject loopback, private (RFC1918), link-local (169.254/16, incl. 169.254.169.254), and unspecified addresses;
   - restrict schemes to `http`/`https` explicitly.
   Reason: these ranges are never legitimate "web grounding" targets for this tool.
2. Disable `follow_redirects=True` or re-check the final URL against the same guard after the request (httpx exposes `response.url`). Reason: a public URL can 302-redirect to `http://127.0.0.1/...`, bypassing a check on the initial URL only.
3. (Optional) add config `web_fetch.allowed_hosts` / `web_fetch.blocked_hosts` lists for site-specific policies. Reason: some deployments may legitimately need internal doc sites.

**Validation**
- Unit tests: `http://127.0.0.1:6379`, `http://169.254.169.254/latest/meta-data/`, `http://10.0.0.5/` all rejected with a clear error code.
- Unit test: a redirect from public host to loopback is rejected.

---

### F6. WebUI over plain HTTP; cookie without `secure`; token in WS query string (HIGH)

**Evidence**
- `nginx/nginx.conf:21` — `listen 80`, no TLS server block, no security headers, no rate limiting.
- `webui/backend/app.py:273-276` — `set_cookie(..., httponly=True, samesite="strict")` but no `secure=True`.
- `webui/backend/app.py:490-499` — `/ws/logs` accepts `token` as a query parameter.

**Risk**
Session tokens and login credentials travel in cleartext; any network observer can steal the session. Tokens in URLs leak into access logs, proxies, and browser history.

**Proposed modification**
1. Add a TLS server block to `nginx/nginx.conf` (443 with cert/key paths from env, 80 → 302 redirect to https). `.env` already has commented `TLS_CERT_PATH`/`TLS_KEY_PATH` placeholders. Reason: the intended deployment model already anticipated TLS; the config just was never implemented.
2. In `webui/backend/app.py`, set `secure=True` on the `aidir_token` cookie when the request scheme is https (detect via `X-Forwarded-Proto` from nginx). Reason: prevents cookie capture over HTTP while keeping dev mode (plain localhost) working.
3. Keep the WS query-token fallback but document it as a fallback only; primary path is the cookie (browsers send it on same-origin WS). Optionally add nginx `limit_req` to `/api/auth/login`. Reason: reduces token leakage surface without breaking token-based API clients.
4. Add basic nginx hardening: `add_header X-Content-Type-Options nosniff;`, `add_header X-Frame-Options DENY;`, `client_max_body_size` cap, `limit_req_zone` for `/api/`. Reason: low-effort, standard hardening.

**Validation**
- Manual: `curl -k https://.../api/auth/me` works; http redirects to https; cookie header shows `Secure`.
- Check nginx error log for no config warnings (`nginx -t`).

---

### F7. Redis without password by default (MEDIUM)

**Evidence**
- `config.json5:12` — `"password": "${REDIS_PASSWORD:-}"`.
- `docker-compose.yml:8` — `--requirepass` added only when `REDIS_PASSWORD` is set.
- `core/app.py:111-118`, `core/cron.py:82-90` — URL built with password only when non-empty.

**Risk**
Redis stores sessions, the task queue, and full task payloads (user prompts, results). Without auth, anyone with network reach to the Redis port (or a local user) can read/modify/delete them.

**Proposed modification**
1. Make `REDIS_PASSWORD` mandatory: at core startup, if the resolved password is empty, log `critical` and refuse to start (config change, fail-closed). Reason: sessions and task data are sensitive; an unauthenticated key-value store is the weakest link.
2. Update `install.sh` to generate a random password in `.env` when `REDIS_PASSWORD` is absent. Reason: zero-friction secure default for fresh installs.

**Validation**
- Startup test: empty `REDIS_PASSWORD` → process exits with clear log message.
- `docker compose up` with generated password: `redis-cli -a <pw> ping` OK.

---

### F8. Path traversal in /ws/logs (MEDIUM)

**Evidence**
- `webui/backend/app.py:490-508` — `file` query parameter is used as `_LOGS_DIR / f"{file}.log"` without the `_resolve_log_file()` check that the REST `/api/logs` endpoint (line 68-84) applies.
- Example: `file=../../../etc/nginx/error` → path `logs/../../../etc/nginx/error.log` resolves outside `logs/` and is streamed live to the client (auth required).

**Risk**
Any authenticated user can stream arbitrary readable `*.log`-suffixed files outside the logs directory (and on some mounts, trickier paths). Inconsistent with the REST endpoint's protection — likely an oversight.

**Proposed modification**
Reuse `_resolve_log_file(file)` in the WebSocket handler (it already returns the right 400 semantics; adapt for WS by closing with code 4001 on `HTTPException`). Reason: single source of truth for log-file resolution; the REST path already proves the pattern works.

**Validation**
- Test: `ws /ws/logs?file=../../etc/passwd` → connection closed (4001), no data.
- Test: normal `file=all` still streams.

---

### F9. Unbounded client timeout in /api/test/agent (MEDIUM)

**Evidence**
- `webui/backend/app.py:936-940` — `timeout = float(body.pop("_timeout"))` with no upper bound.

**Risk**
A client can hold a connection (and a scheduler slot indirectly) for arbitrarily long; combined with the proxy read timeout of 300s in nginx it is bounded per request but still enables resource-wasting long requests by any authenticated user.

**Proposed modification**
Clamp the value: `timeout = max(1.0, min(float(request_timeout), config webui.request_timeouts.agent_test * 2))` (or a hard cap of 600s). Reason: the endpoint's purpose is quick manual testing; unbounded client control over server-side timeouts is unnecessary.

**Validation**
- Test: `_timeout=999999` is clamped to the cap (assert via timing or by inspecting the clamped value in a unit test of the helper).

---

### F10. World-readable logs containing task payloads (MEDIUM)

**Evidence**
- `logs/` directory and files are `rw-rw-r--` (775), e.g. `openaix_call_log.jsonl` (65 MB) contains request/response data.
- `core/logger.py` creates files with default umask.

**Risk**
Any local user account can read all LLM traffic (prompts, tool arguments, possibly secrets echoed in payloads).

**Proposed modification**
1. Set `0o600` (files) / `0o700` (dir) explicitly in `core/logger.py` when creating log files (and in cron maintenance paths). Reason: logs are for the service operator, not all local users.
2. (Ops) `chmod 700 logs/` on existing installs.

**Validation**
- After restart, `stat -c %a logs/all.log` → 600.

---

### F11. config.json5 present in git history (LOW)

**Evidence**
- `git log --all -- config.json5` → present in commits `1530bc6`, `ce8acf2` (currently gitignored, but history retains it).
- The historical versions reference `${VAR}` placeholders (good), but any later value committed in another file must be assumed exposed if the repo was ever shared/pushed.

**Risk**
If the repository is mirrored or pushed to a remote, all historical config contents are recoverable.

**Proposed modification**
1. Audit history for hardcoded secrets: `git log -p --all | grep -nE "(token|password|apiKey)" | grep -v '\${'`. Reason: confirm nothing real was ever committed.
2. If anything is found: rotate that secret immediately (config change) and, if the repo is shared, consider history rewriting (BFG/filter-repo). If clean: no action beyond this note. Reason: history rewriting is disruptive; do it only when actually needed.

---

### F12. external_task_live default ~29 days (LOW)

**Evidence**
- `config.json5:852` — `"external_task_live": ${EXTERNAL_TASK_LIVE_SECONDS:-2500000}`.

**Risk**
External tasks may stay "live" in the queue for ~29 days; a misbehaving client can accumulate queue entries and Redis memory.

**Proposed modification**
Lower the default to a sane value (e.g. 3600 s) and clamp the configured value with an upper bound in `core/task.py` / queue logic. Reason: the default should protect against misconfiguration; deployments that genuinely need long-lived tasks set it explicitly.

**Validation**
- Unit test: task with external=True is expired after the configured live window.

---

### F13. Prompt-injection surface (LOW, design-level)

**Evidence**
- Tool results from `web_fetch`/`web_search` and tool names/descriptions from external MCP servers (`workers/tool/external_mcp/app.py`) are injected into the LLM context; the openaix agent then autonomously calls tools (`workers/agent/openaix/app.py`).
- `workers/agent/openaix/app.py:1081` — `ast.literal_eval` is used on model output (safe against code execution, but the parsing of model-directed tool arguments is the injection vector, not the eval).

**Risk**
A malicious web page or a malicious/compromised external MCP server can steer the agent into calling tools (e.g. fetch internal URLs — see F5 — or http_api operations with attacker-chosen params). There is no sandboxing or human-in-the-loop.

**Proposed modification**
1. Mitigate via F5 (SSRF guard) — removes the most dangerous consequence.
2. Add per-tool allowlists per envid: an envid's context should be able to declare which tools it may call (extend `envids.items.<id>.workers` override with a `tools: [...]` restriction), enforced in the agent worker before task creation. Reason: least-privilege per environment; a "docs lookup" envid should not be able to call `http_api`.
3. (Follow-up, larger) add an optional human-approval gate for tool calls flagged as sensitive (config `agents.require_approval_tools`). Reason: for high-stakes deployments; not required for MVP.

**Validation**
- Test: agent task from envid with `tools: ["fetch"]` attempting `http_api` is rejected with a clear error code.

---

## 3. Already sound (no action needed)

- `.env` and `config.json5` are gitignored; current config uses `${VAR}` placeholders.
- Constant-time comparison (`hmac.compare_digest`) for WebUI passwords and API tokens.
- Session tokens: `secrets.token_hex(32)`, stored in Redis with TTL, deleted on logout.
- Frontend escapes all interpolated task data (`escapeHtml`) — no stored XSS found in reviewed templates.
- Subprocess launch uses `shlex.split` + `create_subprocess_exec` (no shell).
- Owned-process tracking for llama.cpp servers uses `/proc/<pid>/stat` start-ticks (PID-reuse safe) and never kills unowned processes.
- REST `/api/logs` has correct path-traversal protection (only the WS path misses it — F8).

## 4. Suggested implementation order

1. F1 (endpoint auth + bind) — closes the largest external attack surface.
2. F2 + F3 (config masking, permissions, exec_cmd validation) — closes the authenticated RCE chain.
3. F4 (login hardening) and F6 (TLS) — transport and credential safety.
4. F5 (SSRF guard) — protects against the agent's most dangerous side effect.
5. F7, F8, F9, F10, F12 — medium/low hygiene.
6. F11 (history audit) and F13 (envid tool allowlists) — process + design follow-ups.

## 5. Testing plan (per AGENTS.md)

- New unit tests under `tests/` (or alongside existing `test_*.py` files) for: endpoint auth (F1), config masking + permission checks (F2), exec_cmd validation (F3), login throttling + hashed password (F4), SSRF guard (F5), WS log path (F8), timeout clamp (F9), external_task_live expiry (F12).
- Update `TEST.md` protocol with the manual checks from each section's "Validation".
- Regression: run existing `test_*.py` suite and `smoke_test.py` after each step; no behavior change is expected for correctly configured deployments except that endpoints now require a token by default.
