# Proposal: OpenCode Go Provider Support

## Summary

This proposal adds OpenCode Go models as concrete aidir providers and as candidates for `api: "smart"` models. It defines a provider adapter that selects the correct OpenCode Go protocol per configured model and guarantees that OpenCode's required `x-opencode-session` header is present.

The public aidir API remains Ollama and OpenAI chat compatible. Clients select an aidir model alias; they do not need to know which OpenCode Go protocol serves the selected model.

Goals:

1. Support OpenCode Go models that expose OpenAI-compatible, Anthropic-compatible, or other documented upstream protocols.
2. Allow every supported OpenCode Go model to be used directly and through `first_available` smart routing.
3. Preserve a caller-provided `x-opencode-session` without modification.
4. Generate and reuse a distinct server-managed OpenCode session for separate aidir clients that omit the header.
5. Avoid using a raw IP address as the sole durable client identity.
6. Keep OpenCode session state separate from aidir `envid`, task ID, and WebUI login sessions.

Non-goals:

1. Implement every OpenCode Go API surface on the public aidir endpoint.
2. Infer or modify a caller-supplied OpenCode conversation/session identifier.
3. Promise stable anonymous identity across NAT changes, proxy changes, restarts without Redis persistence, or deliberately indistinguishable callers.

## Provider Configuration

OpenCode Go is represented by a provider family. Each model declares the upstream protocol used for that model because models may be exposed through different APIs.

```json5
"models": {
  "providers": {
    "opencode_go": {
      "api": "opencode-go",
      "baseUrl": "https://opencode.example",
      "auth": {"type": "bearer", "key": "${OPENCODE_GO_API_KEY}"},
      "session": {
        "ttl_seconds": 2592000,
        "anonymous_ttl_seconds": 86400,
        "trust_proxy_headers": false
      },
      "models": [
        {
          "id": "provider-model-openai",
          "alias": "opencode-chat",
          "protocol": "openai-chat",
          "upstream_model": "provider-model-openai",
          "contextWindow": 128000,
          "resources": {}
        },
        {
          "id": "provider-model-anthropic",
          "alias": "opencode-reasoning",
          "protocol": "anthropic-messages",
          "upstream_model": "provider-model-anthropic",
          "contextWindow": 200000,
          "resources": {}
        }
      ]
    }
  }
}
```

Field rules:

1. `api` must be `opencode-go`.
2. `baseUrl` is the OpenCode Go server root URL.
3. `protocol` is required per model. Initial supported values are `openai-chat` and `anthropic-messages`; an unsupported value fails configuration validation or returns a clear route error.
4. `upstream_model` defaults to `id` when omitted. `alias` remains the globally unique public aidir model name.
5. Provider authentication follows the existing remote-provider rules. The adapter may forward the incoming Bearer token only when no provider authentication is configured.
6. `session.ttl_seconds` applies to authenticated or otherwise strong identities. `anonymous_ttl_seconds` limits the lifetime of weaker heuristic bindings.
7. `trust_proxy_headers` defaults to `false`. When enabled, aidir must only be reachable through a configured trusted reverse proxy which overwrites `X-Forwarded-For` or `Forwarded`.

## Worker and Protocol Adapters

Add an agent worker, `call_opencode_go`. Route resolution selects it automatically for a resolved `api: "opencode-go"` provider, as it does for the existing protocol-specific workers.

The worker receives aidir's internal Ollama-shaped task payload. It resolves the model configuration, injects the session header, and then dispatches by `protocol`:

| Model protocol | Upstream request | Adapter responsibility |
| --- | --- | --- |
| `openai-chat` | `POST /v1/chat/completions` | Reuse the OpenAI-compatible request/response and SSE conversion pattern. |
| `anthropic-messages` | `POST /v1/messages` | Convert system/messages/tools, map generation settings, and translate normal and streamed responses to aidir's internal result shape. |
| Future named protocol | Explicitly configured endpoint and adapter | Add only with fixtures from the real OpenCode Go server. |

The protocol must never be guessed from a model name or from an upstream error. A model is unavailable to routing if its configured protocol is not supported by the installed adapter.

Initial discovery may use the provider's documented model-listing endpoint where one exists. Configured model metadata remains authoritative for aliases, protocol, context window, resources, and routing eligibility. `POST /api/show` and `/v1/models` therefore remain functional even when the remote OpenCode Go service is temporarily unavailable.

## OpenCode Session Contract

OpenCode Go requires `x-opencode-session` on upstream inference calls. The worker must set exactly one value for each request:

1. When the incoming request contains a non-empty `x-opencode-session`, forward that value unchanged. Do not store, rewrite, or replace it.
2. Otherwise resolve a server-managed session binding for the caller and target OpenCode model.
3. If no binding exists, generate an opaque UUID using `uuid4`, persist it in Redis, and send it as `x-opencode-session`.
4. Reuse the stored value until its TTL expires. Refresh the TTL after a successful request.

The session value is opaque, never returned in normal aidir response bodies, never included in task logs, and redacted from error logs. It may be exposed only to an explicitly authorized debugging endpoint if such an endpoint is introduced later.

### Binding Key

The Redis key must include provider and upstream model so that unrelated OpenCode models do not unintentionally share upstream conversation state:

```text
opencode_go:session:v1:{provider_id}:{upstream_model}:{identity_hash}
```

`identity_hash` is a SHA-256 digest of a canonical, versioned identity string with a server-side secret salt. The raw identity components must not become Redis key material or logs.

### Identity Precedence

For requests without a caller-owned header, choose the first available identity source:

1. Authenticated aidir principal: a stable ID derived from a validated aidir Bearer token, not the raw token.
2. Trusted `envid`: only when it was assigned or validated by authenticated aidir policy, not an arbitrary request field.
3. WebUI authenticated principal: when requests originate through the WebUI backend and it can pass an internal authenticated principal to task creation.
4. Anonymous heuristic: normalized client IP plus a conservative request fingerprint.

The anonymous fingerprint should include the resolved provider, upstream model, and stable client hints when available: a normalized User-Agent family and an optional client-supplied `X-Aidir-Client-Id`. The optional client ID must be treated as untrusted unless it is bound to authenticated identity; it improves continuity but cannot provide authorization.

Do not include prompt text, message content, full User-Agent strings, cookie values, or raw IP address in the fingerprint. Do not use port numbers. The anonymous mapping has a short TTL and must be documented as best effort.

This yields different managed sessions for two callers with distinct authenticated identities, and generally for distinct anonymous IP/fingerprint pairs. Calls from indistinguishable anonymous clients behind one NAT can still collide; callers requiring a guaranteed isolated conversation should send `x-opencode-session` themselves or authenticate with aidir.

### Concurrency and Lifecycle

Creation uses Redis `SET key value NX EX ttl` or an equivalent atomic primitive. If a concurrent request creates the binding first, the loser reads and uses the winning value. This prevents concurrent first requests from assigning multiple upstream sessions to one identity.

The session store is independent of task retries. A retry uses the same binding, while an upstream response may still fail for unrelated reasons. Deleting a binding, TTL expiry, or Redis data loss creates a new OpenCode session on the next request; this is an expected continuity boundary and should be visible through metrics.

## Smart Routing

OpenCode Go models are ordinary concrete candidates in existing smart models:

```json5
"assistant": {
  "id": "assistant",
  "type": "first_available",
  "items": [
    {"provider": "opencode_go", "model": "provider-model-openai", "fallback_prio": 10},
    {"provider": "llama_local", "model": "local-model", "fallback_prio": 20}
  ]
}
```

The smart router continues to make its choice before the task is queued. The final route must retain both the originally requested smart alias and the resolved provider/model. Session resolution occurs after this choice, in `call_opencode_go`, because the session key needs the concrete provider and upstream model.

Routing availability must use a bounded protocol-specific health or model probe when OpenCode Go offers one. A failed probe makes the candidate ineligible for immediate selection; normal busy fallback behavior remains unchanged. Probe requests do not create or consume OpenCode sessions.

## Error Handling and Observability

New errors:

1. `OPENCODE_PROTOCOL_UNSUPPORTED`: model configuration names an unavailable protocol adapter.
2. `OPENCODE_SESSION_STORE_UNAVAILABLE`: a managed session is needed but Redis cannot create or read the binding. Do not silently generate an unpersisted session because repeated calls would lose continuity.
3. `OPENCODE_UPSTREAM_ERROR`: non-success response from the upstream OpenCode Go protocol endpoint.
4. `OPENCODE_UPSTREAM_INVALID_RESPONSE`: the response cannot be converted to the internal result shape.

Metrics and structured logs should record provider, model, protocol, session source (`caller`, `authenticated`, `envid`, `webui`, `anonymous`), session-binding outcome (`reused`, `created`, `expired`), and upstream latency. They must not record the session value, bearer token, raw IP, prompt content, or raw identity string.

## Implementation Plan

1. Validate `api: "opencode-go"`, model-level `protocol`, and session settings in configuration loading.
2. Add route-to-worker resolution and the `call_opencode_go` worker skeleton.
3. Implement a Redis-backed session-binding component with identity precedence, TTLs, atomic creation, redaction helpers, and unit tests.
4. Implement the `openai-chat` adapter with sync and SSE fixtures.
5. Implement the `anthropic-messages` adapter with sync and SSE fixtures.
6. Add protocol-specific health probes to smart-routing candidate evaluation.
7. Expose configured OpenCode models through the existing model-listing and `/api/show` behavior.
8. Add observability, documentation, and a configuration example before enabling an active provider.

## Validation

Focused tests should cover:

1. Configuration rejects an OpenCode Go model without a supported `protocol`.
2. Direct and smart-routed requests select `call_opencode_go`.
3. A caller-provided `x-opencode-session` reaches each adapter unchanged.
4. The same authenticated caller and resolved model reuses one managed session.
5. Two authenticated callers receive different managed sessions.
6. Anonymous callers with different IP/fingerprint values receive different sessions; identical values reuse one within the short TTL.
7. Concurrent first calls create one Redis binding.
8. Session headers are absent from result payloads and redacted from logs/errors.
9. Each protocol adapter translates normal and streaming responses correctly.
10. Smart routing skips an unhealthy OpenCode Go candidate and does not create a session during probing.

Suggested commands after implementation:

```bash
./venv/bin/python -m unittest -v test_opencode_go.py test_smart_router.py
./venv/bin/python -m py_compile core/smart_router.py workers/agent/call_opencode_go/app.py
```

## Open Questions

1. Which exact OpenCode Go protocol endpoints and model identifiers are available in the target deployment? Capture real request/response fixtures before implementing adapters.
2. Does the target upstream require the session header on discovery and health calls, or only inference calls?
3. Can aidir map authenticated tokens to a stable non-secret principal ID centrally, rather than deriving an identity hash independently in each endpoint?
4. Which reverse proxies are trusted to supply client IP headers in deployment, and should this be configured per endpoint or globally?
5. Should a caller be offered an opt-in response header indicating that aidir created a managed session, without disclosing the session ID?