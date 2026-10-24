# SPEC Overlay

Status: active. Date: 2026-10-24.

This file is the editable overlay over [SPEC.md](SPEC.md). It records the
changes and decisions that have been approved since the base specification was
last synchronized. If this file and `SPEC.md` disagree, this file wins.

The user merges this overlay into `SPEC.md` from time to time, then deletes
`SPEC_.md`. Agents must add new specification changes here, not in `SPEC.md`.
Direct edits to `SPEC.md` are forbidden unless the user explicitly approves a
narrow, one-time correction.

## 1. Documentation and proposal lifecycle

- `SPEC.md` is the base specification and is read-only for agents.
- `SPEC_.md` is the editable overlay. It is not a correction log and does not
  use the `FILENAME_.md` correction template.
- Only the highest-numbered `proposal_updateN.md` is open. Every older
  `proposal_update*` document is closed.
- A proposal may be partially closed. Unfinished items may be moved to the next
  `proposal_updateN.md` only after direct approval by the user.
- A non-update proposal (`proposal_<name>.md`) is independent. Its status is
  recorded inside the file, and closing it does not require a new
  `proposal_update`.
- `proposal_update*` is reserved for small list fixes, bugs and inaccuracies.
  Large new features belong in a separate `proposal_<name>.md`.
- Deferred work and ideas that still need discussion belong in `ROADMAP.md`.
- `DOCUMENTS.MD` must stay synchronized with every document added, renamed or
  closed.
- `SPEC_NEW.md` is not a current document name. References to it mean
  `SPEC.md`.

## 2. Configuration: `default_auth_mode`

`config.json5` gets a root key `default_auth_mode`:

```json5
{
  default_auth_mode: "disabled",
}
```

Semantics:

- If the key is absent, the value is `"endpoint"`.
- `"endpoint"` makes peer surfaces require a valid API token by default.
- `"disabled"` keeps peer surfaces public by default.
- The key is parsed as a whitespace-separated list of keywords so that future
  authentication mechanisms can be added without a format change. At present
  the only keywords are `disabled` and `endpoint`; if both are present,
  `endpoint` wins.
- An unknown or invalid value fails closed to `"endpoint"`.
- An explicit `auth_mode` in `peer_tasks` or `peer_resources` overrides this
  default for that surface.
- The provider `auth` block is not a peer authentication setting. It stays the
  credential used by the reading side when it calls a peer, and by an inference
  provider when it calls an upstream.
- The shipped `config.json5` and `config.json5.example` set
  `default_auth_mode: "disabled"` until the reading side is known to carry the
  required tokens. This preserves the behavior of existing multi-instance
  deployments.

## 3. Security decisions

### 3.1 MCP authentication (W1)

`require_auth` keeps its default of `false`, including for the `mcp` API. The
doctor's `auth` check must report every MCP endpoint that publishes its tools
without a token. The report is advisory; it does not change the default.

A separate dashboard security panel is proposed in
[proposal_security_dashboard.md](proposal_security_dashboard.md). It belongs
to the left of the system-health card and lists open security findings such as
MCP without authentication.

### 3.2 WebUI authentication (W3)

W3 is deferred to [ROADMAP.md](ROADMAP.md). The previously accepted
`sha256$<hex>` scheme is withdrawn; no interim password-hash format is currently
planned.

The preliminary direction is browser-side authentication:

- the browser derives a deterministic signing key pair from the password and a
  per-user salt;
- the server stores the login, the salt and the **public** key, never the
  password or a password hash;
- at sign-in the server sends a fresh random challenge; the browser derives the
  private key locally and signs the challenge;
- the server verifies the signature with the stored public key and then creates
  the ordinary session cookie.

The password must never be sent to the server. Signing a static salt alone is
not sufficient because it is replayable; the signed value must include a fresh
server challenge. This is a preliminary design, not a PAKE: a stolen public key
and salt still permit an offline dictionary attack. Login rate limiting remains
required.

## 4. Task persistence (W20)

A task restored from Redis must preserve its retry and fallback state. The
following fields are part of `Task.to_redis_hash()` and
`Task.from_redis_hash()`:

- `next_retry_at` (float),
- `fallbacks` (JSON list of strings),
- `on_reject` (JSON object).

`fallback_index` and `retry_attempt` were already persisted. After this change,
a restored task continues its fallback chain instead of starting it from the
beginning.

## 5. Closed and deferred items

- W28 (orphan count as a separate health-card row) is closed. The count is
  already exposed in the execution-health data and rendered by the existing
  health card; a separate row is not wanted.
- W3 (WebUI authentication) is deferred. The password-derived key-pair design
  is tracked in `ROADMAP.md`; the interim `sha256$` scheme is withdrawn.
- W4 (`web_fetch` SSRF guard) is in scope but deferred. It is tracked in
  `ROADMAP.md`.
- W5 (settings-editor write of `exec_cmd`) is deferred. It is tracked in
  `ROADMAP.md`.
- W7 (CSRF token) is deferred. It is tracked in `ROADMAP.md`.
- The security analysis in `_proposal_update_1.md` is historical. Its open
  findings are tracked by the current proposal ledger and `ROADMAP.md`, not by
  editing the closed security analysis in place.

## 6. Session and routing decisions (W9, W14, W15)

### 6.1 Legacy sessions (W9)

The legacy session fallback is removed. A session is valid only under the
instance-scoped key `aidir:session:<instance>:<token>`. Sessions written before
the namespace change are no longer accepted.

### 6.2 Direct smart routing (W14)

`type: "direct"` is implemented. A direct smart model must have exactly one
candidate. The router resolves to that candidate under the normal admission
rules: a candidate that cannot start is queued, not forced. A direct model with
a different candidate count fails at request time with HTTP 400 and
`INVALID_ROUTING_CONFIG`.

### 6.3 KV affinity identity (W15)

KV affinity depends only on the request context. The alias, endpoint, route and
`prompt_cache_key` do not affect the identity. The same canonical context uses
the same affinity record regardless of where the request came from.

The fingerprint hash is `HMAC-SHA256(secret, "kv2\0kind\0prefix")` and the store
is global with one LRU. `max_entries_per_alias` and `honor_prompt_cache_key` are
accepted for configuration compatibility but deprecated and ignored.

## 7. Security, observability and protocol fixes

- The log WebSocket authenticates with the `aidir_token` session cookie only.
  The query-string token is no longer accepted, so tokens cannot leak into
  access logs.
- Logout reads the session token with the same Bearer-first rule as the
  authentication dependency and deletes that session, not only the cookie one.
- `/api/test/agent` clamps a client-supplied `_timeout` to
  `[1, webui.request_timeouts.agent_test_max]`; the default maximum is 300
  seconds.
- `logs/` is created or tightened to mode `0700`. New audit journal files use
  `0600`. The doctor reports a different directory mode and can fix it.
- A Redis bind outside loopback must have a password. The installer generates
  one when the bind is widened, Compose health checks authenticate, and the
  doctor reports an unprotected widened bind.
- Audit `durable` enqueue and receipt waits are bounded. A failure raises
  `TimeoutError` and marks health unhealthy. `best_effort` still drops on queue
  overflow, and `dropped_recent` is visible in the execution-health line.
- Emergency audit retention may delete today's partition when it is the only
  daily partition left and the emergency limit is exceeded. The deletion is
  logged at warning level.
- Upstream error responses preserve `retry-after`, `request-id`, `x-request-id`
  and the `x-ratelimit-*` family. Other upstream headers are dropped.
- Anthropic signed thinking blocks keep their signature through conversion. The
  stream encoder emits `signature_delta` before closing the thinking block; a
  rewritten turn with no signature warns and omits the signature instead of
  inventing an empty one.
- `logging.logs` and its legacy alias `logging.log_overrides` are declared in
  the descriptor as open maps of bounded per-file maintenance settings.
- `messages_response_to_internal` no longer accepts an unused `streaming`
  keyword.

## 8. Test LLM endpoint selection

`POST /api/test/llm` does not require an endpoint whose `api` is exactly
`ollama`. It uses the first chat-capable endpoint (`ollama` or `openaix`) that
has a port, and prefers an endpoint whose `worker` matches the `worker` field in
the request. The request is always sent to that endpoint's `/api/chat`. This
lets the Test LLM dialog exercise the shipped `openaix` endpoint, including
`call_llama_cpp` and other workers.
