# Proposal: Universal Remote OpenAI Worker and OpenCode Go Sessions

## Decision

Implement the third option: add one universal `call_openai` agent worker for
remote servers that implement the OpenAI Chat Completions protocol. OpenCode Go
is configured as a normal `api: "openai"` provider of that worker, not as a
separate worker and not as an endpoint hook.

The provider-only `force_x_opencode_session` feature makes the worker add
OpenCode Go's required `x-opencode-session` request header. It is enabled by
default for the OpenCode Go configuration example, and disabled by default for
ordinary OpenAI-compatible providers. This avoids leaking a vendor-specific
header to every remote provider while keeping OpenCode Go configuration
explicit and minimal.

`call_llama_cpp` remains separate because it owns a local llama.cpp process and
therefore has materially different lifecycle, health, and resource behavior.
The existing `call_ollama` remains separate because it speaks the incompatible
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
5. No current outbound worker speaks OpenAI Chat Completions. Adding an
   OpenCode-only worker would duplicate the future generic remote OpenAI
   transport and response conversion.

## Scope

Initial scope is the OpenAI Chat Completions protocol only:

- request: `POST {baseUrl}/v1/chat/completions`;
- normal JSON and server-sent-event streaming responses;
- OpenAI bearer authentication or an explicitly configured static header;
- configured OpenAI-compatible models, including OpenCode Go models;
- direct routes and `api: "smart"` routes;
- optional OpenCode session injection.

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
      "api": "openai",
      "baseUrl": "${OPENCODE_GO_BASE_URL}",
      "auth": {
        "type": "bearer",
        "token": "${OPENCODE_GO_API_KEY}"
      },
      // OpenCode Go-specific extension; true is the intended default here.
      "force_x_opencode_session": true,
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
    }
  }
}
```

Rules:

1. `api: "openai"` means OpenAI Chat Completions over HTTP; `baseUrl` is
   required and cannot include a request path.
2. `force_x_opencode_session` is a boolean provider option. It defaults to
   `false` when omitted. OpenCode Go configurations must set it to `true`;
   the shipped OpenCode example documents `true` as its default.
3. When the flag is `true`, `opencode_session.ttl_seconds` defaults to `86400`
   and `anonymous_ttl_seconds` defaults to `3600`. Both must be positive
   integers, and the anonymous TTL cannot exceed the normal TTL.
4. A provider's configured authentication takes priority. Forwarding a
   caller's bearer token remains only the existing explicit fallback behavior;
   it is never used as an OpenCode session value.
5. The implementation should use repository-standard snake_case JSON keys.
   The proposal's option corresponds to the requested
   `force-x-opencode-session` behavior, not a second hyphenated alias.

## Request and Routing Flow

1. The endpoint authenticates and normalizes the inbound OpenAIx or Ollama
   request as it does today.
2. Before queueing, it resolves smart routes to a concrete provider and model.
   When the resolved provider has `api: "openai"`, it selects `call_openai`.
3. The endpoint captures only the information needed for session selection:
   a caller-supplied non-empty `x-opencode-session`, or a non-secret identity
   fingerprint. It places this protected internal metadata in task config; it
   must not add the raw header to the public payload.
4. `call_openai` converts the normalized internal messages, tools, generation
   options, and stream flag to the OpenAI Chat Completions request shape.
5. If session forcing is enabled, it resolves the session described below and
   adds exactly one `x-opencode-session` upstream header.
6. The worker translates the upstream JSON or SSE response to aidir's existing
   internal result/chunk format. The public endpoint retains responsibility for
   rendering Ollama or OpenAIx responses.

The OpenAI provider needs no process manager and must use the ordinary outbound
HTTP timeout, audit, task cancellation, retry, and error conventions.

## OpenCode Session Contract

When `force_x_opencode_session` is `true`, the worker sends exactly one
`x-opencode-session` header on every inference request:

1. If the client supplied a non-empty header, forward it unchanged. Do not
   store it, rewrite it, expose it in a response, or include it in logs.
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

`api: "openai"` models become normal concrete candidates of `api: "smart"`
models. The route is resolved before session allocation, so each binding is
keyed to the actual selected provider/model and a health probe never creates a
session.

The smart router should treat a configured remote OpenAI provider as a remote
candidate, rather than its current special case for `api: "openaix"`. Initial
availability probing uses a bounded, session-free configured health URL (with
`/v1/models` as the default only where supported). A failed probe makes a
candidate ineligible; it does not send `x-opencode-session`.

## Error Handling and Observability

Errors must be explicit and follow existing endpoint compatibility envelopes:

- `OPENAI_PROVIDER_INVALID_CONFIG`
- `OPENAI_UPSTREAM_UNREACHABLE`
- `OPENAI_UPSTREAM_TIMEOUT`
- `OPENAI_UPSTREAM_ERROR`
- `OPENAI_UPSTREAM_INVALID_RESPONSE`
- `OPENCODE_SESSION_STORE_UNAVAILABLE`

Logs and metrics may include provider ID, model ID, protocol, stream mode,
identity-source label, and binding outcome (`caller`, `created`, `reused`).
They must redact `Authorization`, `x-opencode-session`, raw identity values,
and prompt content.

## Implementation Plan

1. Add configuration validation and the documented `call_openai` worker entry.
2. Add `api: "openai"` worker resolution in both endpoint route selection and
   smart-route resolution; preserve the dedicated llama.cpp mapping.
3. Capture and sanitize the incoming OpenCode session/identity context before
   task persistence.
4. Implement a small Redis-backed session-binding component with atomic
   creation, TTL refresh, redaction, and failure reporting.
5. Implement `call_openai` using existing worker logging, LLM-call history,
   timeout, cancellation, retry, and streaming conventions.
6. Extend smart-route probing to remote `api: "openai"` providers without
   allocating a session.
7. Add the provider example and user-facing configuration documentation after
   the code and tests establish the final field semantics.

## Validation

Focused tests must prove:

1. a direct and a smart route to `api: "openai"` select `call_openai`;
2. llama.cpp and Ollama retain their current workers;
3. sync and streaming OpenAI responses preserve the existing public OpenAIx and
   Ollama response contracts;
4. configured provider authentication is applied and secrets are redacted;
5. a supplied `x-opencode-session` is forwarded unchanged and never persisted;
6. one authenticated client/model reuses its managed session within TTL;
7. different authenticated clients receive different sessions;
8. anonymous identity is digested before task persistence and is scoped to the
   short anonymous TTL;
9. concurrent first requests create and use one binding;
10. Redis failure is visible as a task error, not a transient random session;
11. session-free health probes do not create Redis session bindings;
12. headers and session values never appear in task results, audit payloads, or
    ordinary logs.

## Open Question Requiring Confirmation

The target OpenCode Go deployment must provide a verified OpenAI Chat
Completions endpoint and model-listing or health endpoint. Before implementation,
capture redacted fixtures for one non-streaming and one streaming request,
including the exact response to a valid `x-opencode-session`. If it instead
requires a different upstream protocol, retain this universal OpenAI worker and
add a separate named protocol adapter rather than changing behavior based on
provider branding.
