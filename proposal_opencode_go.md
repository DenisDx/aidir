# Proposal: Remote OpenAI and Dedicated OpenCode Go Workers

## Decision

Implement two remote agent workers:

1. `call_openai` serves ordinary `api: "openai"` providers that implement
   OpenAI Chat Completions.
2. `call_opencode_go` serves only `api: "opencode-go"` providers. It implements
   OpenCode Go's documented Chat Completions endpoint, provider authentication,
   coding-agent User-Agent, and stable `x-opencode-session` behavior.

OpenCode Go is deliberately not configured as a generic OpenAI provider. Its
provider-specific requirements stay isolated in `call_opencode_go`, so a
vendor header, generated session binding, and required User-Agent can never
leak to a normal OpenAI-compatible server.

`call_openai` owns the shared OpenAI Chat Completions request conversion,
response conversion, streaming, authentication, timeout, cancellation, retry,
and observability behavior. `call_llama_cpp` subclasses it and adds only the
local llama.cpp process lifecycle before invoking that shared transport.
`call_ollama` remains separate because it speaks the incompatible
Ollama `/api/chat` protocol. The existing `openaix` worker is also not a
candidate for reuse: despite its name, it converts public OpenAIx requests to
outbound Ollama requests.

## Evidence in the Current Architecture

1. Provider selection is already based on `models.providers.<id>.api`, and the
   endpoint routes `llama-cpp` providers to `call_llama_cpp`. Extending this
   mapping to `openai -> call_openai` makes provider routing consistent.
2. Smart routing stores the resolved provider, model, and worker on the task,
   so the final remote provider is known before the outbound call starts.
3. Aidir's OpenAIx endpoint already accepts both Ollama-style and OpenAI Chat
   Completions requests, normalizing them into the internal agent task shape.
4. Redis is already the authoritative cross-process store for tasks. It is the
   correct store for a session binding that must survive a queue delay, retry,
   and service restart.
5. Existing llama.cpp OpenAI conversion and SSE handling can move into
   `call_openai` without changing their behavior. This makes `call_openai` the
   reusable transport base while llama.cpp retains only its local-server logic.

## Scope

Initial scope is the OpenAI Chat Completions protocol only:

- request: `POST {baseUrl}/v1/chat/completions`;
- normal JSON and server-sent-event streaming responses;
- OpenAI bearer authentication or an explicitly configured static header;
- configured OpenAI-compatible models, including OpenCode Go models;
- direct routes and `api: "smart"` routes;
- mandatory OpenCode Go session injection for `api: "opencode-go"` providers.

The worker must not infer Anthropic, Responses API, Ollama, or custom protocols
from model names or upstream errors. Those are separate adapters and require
their own configuration and fixtures.

## Provider Configuration

Provider configuration identifies the transport protocol. Model aliases and
resource requirements retain their current meaning.

```json5
"models": {
  "providers": {
    "opencode_go": {
      "api": "opencode-go",
      "baseUrl": "${OPENCODE_GO_BASE_URL:-https://opencode.ai/zen/go}",
      "auth": {
        "type": "bearer",
        "token": "${OPENCODE_GO_API_KEY}"
      },
      "opencode_session": {
        "ttl_seconds": 86400,
        "anonymous_ttl_seconds": 3600
      },
      "models": [
        {
          "id": "opencode-model-id",
          "alias": "opencode-model"
        }
      ]
    },
    "remote_openai": {
      "api": "openai",
      "baseUrl": "https://api.example.invalid",
      "auth": {
        "type": "bearer",
        "token": "${REMOTE_OPENAI_API_KEY}"
      },
      "models": [{"id": "remote-model"}]
    }
  }
},
"workers": {
  "items": {
    "call_openai": {
      "enabled": true,
      "request_timeout": ${REQUEST_TIMEOUT:-100}
    },
    "call_opencode_go": {
      "enabled": true,
      "request_timeout": ${REQUEST_TIMEOUT:-100},
      "user_agent": "aidir/1.0"
    }
  }
}
```

Rules:

1. `api: "openai"` means OpenAI Chat Completions over HTTP. `api:
   "opencode-go"` means OpenCode Go Chat Completions over HTTP. `baseUrl` is
   required and cannot include an endpoint suffix such as
   `/v1/chat/completions`.
2. An `opencode-go` provider always enables managed session behavior; it has
   no `force_x_opencode_session` switch. The shipped OpenCode Go example uses
   `https://opencode.ai/zen/go` as its base URL.
3. For an `opencode-go` provider, `opencode_session.ttl_seconds` defaults to `86400`
   and `anonymous_ttl_seconds` defaults to `3600`. Both must be positive
   integers, and the anonymous TTL cannot exceed the normal TTL.
4. A provider's configured authentication takes priority. Forwarding a
   caller's bearer token remains only the existing explicit fallback behavior;
   it is never used as an OpenCode session value.
5. `call_opencode_go` sends its configured non-empty `user_agent` and must
   reject an absent or generic HTTP-library User-Agent value.

## Request and Routing Flow

1. The endpoint authenticates and normalizes the inbound OpenAIx or Ollama
   request as it does today.
2. Before queueing, it resolves smart routes to a concrete provider and model.
   A resolved `api: "openai"` provider selects `call_openai`; a resolved
   `api: "opencode-go"` provider selects `call_opencode_go`.
3. The endpoint captures only the information needed for session selection:
   a caller-supplied non-empty `x-opencode-session`, or a non-secret identity
   fingerprint. It places this protected internal metadata in task config; it
   must not add the raw header to the public payload.
4. `call_openai` converts the normalized internal messages, tools, generation
   options, and stream flag to the OpenAI Chat Completions request shape.
5. For an OpenCode Go task, `call_opencode_go` resolves the session described
   below and adds exactly one `x-opencode-session` upstream header.
6. The worker translates the upstream JSON or SSE response to aidir's existing
   internal result/chunk format. The public endpoint retains responsibility for
   rendering Ollama or OpenAIx responses.

The OpenAI provider needs no process manager and must use the ordinary outbound
HTTP timeout, audit, task cancellation, retry, and error conventions.

## OpenCode Session Contract

For every `api: "opencode-go"` inference request, `call_opencode_go` sends exactly one
`x-opencode-session` header on every inference request:

1. If the client supplied a non-empty header, forward it unchanged. Do not
   place it in task metadata, rewrite it, expose it in a response, or include
   it in logs. Store it only in a separate, encrypted, single-task Redis value
   that expires after the task's permitted queue and request lifetime.
2. Otherwise, look up a server-managed session binding for the resolved
   provider, upstream model, and client identity.
3. If absent, atomically create an opaque UUID value and store it in Redis
   with the applicable TTL.
4. Reuse the stored value for later requests from that client and refresh its
   TTL only after a successful upstream response.

The binding key is namespaced by the configured aidir instance:

```text
{instance}:openai-session:v1:{provider_id}:{model_id}:{identity_digest}
```

`identity_digest` is an HMAC-SHA-256 digest with a required secret loaded from
an environment variable. Raw token values, IP addresses, User-Agent strings,
and session values must not become Redis key material, task fields, metrics, or
logs. Creation uses Redis `SET key value NX EX ttl`; on a race, the worker reads
the winning value and uses it.

### Client Identity Precedence

For a managed session, the endpoint computes a versioned HMAC digest using the
first available source:

1. the authenticated aidir principal, represented by a stable digest of the
   validated API token;
2. a validated `envid` when the endpoint's authorization policy bound it to the
   request;
3. anonymous fallback: client IP plus a normalized User-Agent family.

Only the digest and identity-source label travel with the queued task. The
endpoint must calculate the anonymous digest before queue persistence, so raw
client metadata is never persisted. Anonymous identity is intentionally best
effort: indistinguishable clients behind one NAT can share a session. Clients
that require strict conversation isolation should authenticate or supply their
own `x-opencode-session`.

If Redis cannot read or atomically create a required managed binding, the task
fails explicitly with `OPENCODE_SESSION_STORE_UNAVAILABLE`; it must not silently
generate an unpersisted session.

## Smart Routing and Health

`api: "openai"` and `api: "opencode-go"` models become normal concrete
candidates of `api: "smart"` models. The route is resolved before session
allocation, so each binding is keyed to the actual selected provider/model and
a health probe never creates a session.

The smart router should treat configured remote OpenAI and OpenCode Go providers
as remote candidates, rather than its current special case for `api:
"openaix"`. Initial availability probing uses a bounded, session-free
`GET {baseUrl}/v1/models`. A failed probe makes a candidate ineligible; it does
not send `x-opencode-session`.

## Error Handling and Observability

Errors must be explicit and follow existing endpoint compatibility envelopes:

- `OPENAI_PROVIDER_INVALID_CONFIG`
- `OPENAI_UPSTREAM_UNREACHABLE`
- `OPENAI_UPSTREAM_TIMEOUT`
- `OPENAI_UPSTREAM_ERROR`
- `OPENAI_UPSTREAM_INVALID_RESPONSE`
- `OPENCODE_GO_INVALID_CONFIG`
- `OPENCODE_GO_UPSTREAM_UNREACHABLE`
- `OPENCODE_GO_UPSTREAM_TIMEOUT`
- `OPENCODE_GO_UPSTREAM_ERROR`
- `OPENCODE_GO_UPSTREAM_INVALID_RESPONSE`
- `OPENCODE_SESSION_STORE_UNAVAILABLE`

Logs and metrics may include provider ID, model ID, protocol, stream mode,
identity-source label, and binding outcome (`caller`, `created`, `reused`).
They must redact `Authorization`, `x-opencode-session`, raw identity values,
and prompt content.

## Implementation Plan

1. Add configuration validation and the documented `call_openai` and
   `call_opencode_go` worker entries.
2. Add `api: "openai"` and `api: "opencode-go"` worker resolution in both
   endpoint route selection and smart-route resolution; preserve the dedicated
   llama.cpp mapping.
3. Capture and sanitize the incoming OpenCode session/identity context before
   task persistence.
4. Implement a small Redis-backed session-binding component with atomic
   creation, TTL refresh, redaction, and failure reporting.
5. Implement `call_openai` using existing worker logging, LLM-call history,
   timeout, cancellation, retry, and streaming conventions.
6. Implement `call_opencode_go` with the same response conversion plus its
   mandatory User-Agent and session policy.
7. Extend smart-route probing to remote `api: "openai"` and
   `api: "opencode-go"` providers without allocating a session.
8. Add the provider example and user-facing configuration documentation after
   the code and tests establish the final field semantics.

## Validation

Focused tests must prove:

1. a direct and a smart route to `api: "openai"` select `call_openai`;
2. a direct and a smart route to `api: "opencode-go"` select
    `call_opencode_go`;
3. llama.cpp and Ollama retain their current workers;
4. sync and streaming OpenAI responses preserve the existing public OpenAIx and
    Ollama response contracts;
5. configured provider authentication is applied and secrets are redacted;
6. an OpenCode Go request has a configured coding-agent User-Agent and a
    supplied `x-opencode-session` is forwarded unchanged and never persisted;
7. one authenticated client/model reuses its managed session within TTL;
8. different authenticated clients receive different sessions;
9. anonymous identity is digested before task persistence and is scoped to the
    short anonymous TTL;
10. concurrent first requests create and use one binding;
11. Redis failure is visible as a task error, not a transient random session;
12. session-free health probes do not create Redis session bindings;
13. headers and session values never appear in task results, audit payloads, or
    ordinary logs.

## Confirmed OpenCode Go Contract

The official OpenCode Go documentation confirms the following contract:

1. Chat Completions-capable models use
   `POST https://opencode.ai/zen/go/v1/chat/completions`.
2. Model discovery uses
   `GET https://opencode.ai/zen/go/v1/models`.
3. Requests authenticate with the OpenCode Go API key as a bearer token.
4. Clients must identify themselves with a non-generic coding-agent
   `User-Agent` and send a stable session ID using `x-opencode-session` for
   each conversation.

OpenCode Go also exposes Responses and Anthropic Messages paths for models that
require them. They are out of this initial scope: an `opencode-go` provider may
configure only models documented as Chat Completions-compatible. A future named
adapter may add the other protocols without changing the `call_openai` contract.

The session-digest secret is initially loaded from `.env` as
`AIDIR_OPENCODE_SESSION_HMAC_SECRET`. It is required whenever an
`opencode-go` provider creates a managed session. Rotation is an operational
restart that creates new managed bindings; prior bindings expire by their
existing TTL.

The default session-free health check for both remote provider types is
`GET {baseUrl}/v1/models`. AIDIR already exposes `GET /v1/models` from its
OpenAIx endpoint and must retain that behavior.

## Execution Phases and Plan

### Phase 0: Resolve external contracts

Implement the confirmed contract and capture redacted synchronous and streaming
fixtures against a reachable OpenCode Go deployment. **Exit criterion:** the
configuration fields, request URL, health URL, and expected synchronous and
SSE response shapes are verified against OpenCode Go.

### Phase 1: Establish configuration and routing

Add validated `api: "openai"` and `api: "opencode-go"` provider configuration
and worker registration. Extend direct and smart routing to resolve
`call_openai` and `call_opencode_go` while preserving the existing llama.cpp
and Ollama mappings. Add route-selection and invalid-configuration tests.
**Exit criterion:** direct and smart routes select the dedicated worker for
each valid provider type.

### Phase 2: Implement secure session binding

Capture the permitted inbound session context before queue persistence and add
the Redis binding component for `call_opencode_go`, with atomic creation, TTL
handling, redaction, and explicit storage errors. Add identity-isolation,
concurrency, expiry, and Redis-failure tests. **Exit criterion:** all managed
and caller-supplied OpenCode Go session paths satisfy the session contract
without persisting secret values.

### Phase 3: Implement transport and compatibility

Implement synchronous and streaming Chat Completions transport for
`call_openai` and `call_opencode_go`. The Go worker must add the documented
User-Agent and session header; both workers must use established response
conversion, authentication, cancellation, timeout, retry, and error mapping.
Use the verified OpenCode fixtures and add generic OpenAI-compatible fixtures.
**Exit criterion:** both public OpenAIx and Ollama response contracts remain
compatible for synchronous and streaming requests.

### Phase 4: Integrate health, documentation, and release validation

Add session-free health probing for smart routes, document the provider
configuration, and execute the focused validation suite listed above.
Perform a redaction review of task records, audit payloads, logs, and metrics.
**Exit criterion:** health probes never allocate sessions, all focused tests
pass, and the released example config works against the verified OpenCode Go
deployment.

## Implementation Status and Remaining Release Work

The following implementation work is complete:

- `call_openai` is the shared OpenAI Chat Completions transport; `call_llama_cpp`
  extends it only with local server lifecycle behavior.
- Direct provider routing recognizes `api: "openai"` and `api: "opencode-go"`;
  the OpenCode Go provider and two Chat Completions model aliases are configured.
- `call_opencode_go` applies the dedicated User-Agent and session-header policy.
- The endpoint persists only encrypted caller-session references or HMAC
  identity digests; managed bindings use atomic Redis creation and successful
  response TTL refresh.
- Caller-session records are deleted on task completion, with TTL as a fallback.
- Smart routing probes `GET {baseUrl}/v1/models` for OpenAI and OpenCode Go
  candidates without allocating a session.
- Existing focused OpenAI, llama.cpp, queue-state, queue-timeout, and response
  compatibility tests pass after the transport refactor.

The following work is required before release:

1. Add configuration validation at startup for provider base URL, OpenCode Go
   Chat Completions-only models, configured authentication, session TTLs,
   `AIDIR_OPENCODE_SESSION_HMAC_SECRET`, and a non-generic User-Agent.
2. Add focused tests for direct and smart worker selection, `/v1/models`
   success/failure behavior, supplied and managed sessions, identity isolation,
   expiry, concurrent Redis creation, cleanup, Redis failure, and redaction.
3. Add a regression proving that a health probe never creates a session
   binding or caller-session record.
4. Perform an explicit audit/log/task serialization review for Authorization,
   `x-opencode-session`, identity input values, and encrypted session records.
5. Normalize all generic and OpenCode-specific upstream failures to the
   documented `OPENAI_*` and `OPENCODE_GO_*` error codes.
6. Document the provider configuration, required environment variable names,
   aliases, protocol limitation, and base URL semantics in `README.md` and
   each localized `README_*.md`.
7. Capture redacted synchronous and streaming OpenCode Go fixtures, then run a
   live smoke test for `/v1/models`, synchronous Chat Completions, and SSE.

Items 1-7 are release gates. Responses API and Anthropic Messages support are
explicitly out of scope and are not release blockers for this proposal.
