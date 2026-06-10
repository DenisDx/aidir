# Proposal: Token-Authenticated HTTP API Tool Worker for aidir

## Summary

This proposal defines a lightweight built-in HTTP API tool for aidir.

The goal is to add one injectable tool worker that can call token-authenticated HTTP APIs and return normalized structured results to agents.

The tool must:

1. be implemented as a normal `tool` worker, not as a separate agent or subsystem;
2. stay minimally linked to the rest of aidir and reuse only the existing worker/tool injection path;
3. keep credentials outside prompts, context, and agent-visible tool arguments;
4. support multiple configured connectors with token-based authentication;
5. remain read-first and synchronous in v1.

This proposal is intentionally biased toward a small autonomous module rather than a broad integration framework.

## Why This Belongs Inside aidir

The same capability could exist as an external module, but embedding it into aidir is more logical for this use case.

Reasons:

1. aidir already has a native `tool worker` abstraction;
2. agents already receive injected tools through `context_add_internal_tools` and `workers.items.<agent>.tools`;
3. secret-bearing API access should live close to the runtime that already owns config, env substitution, and HTTP tool execution;
4. the required behavior is synchronous capability execution, not autonomous reasoning;
5. introducing another external service would add extra configuration, deployment, and auth surface without strong functional gain.

So v1 should be a built-in tool worker under `workers/tool/http_api/`.

## Design Decision

Implement one new tool worker, tentatively named `http_api`.

Recommended runtime shape:

1. worker path: `workers/tool/http_api/app.py`
2. worker id: `http_api`
3. task type: standard `tool`
4. injection path: existing internal-tool injection through `workers.items.<agent>.tools`
5. task representation: existing `Task_tool`

Do not add in v1:

1. a new task type;
2. a new endpoint family;
3. a new orchestrator or sync daemon;
4. a connector-specific agent;
5. mandatory cache or snapshot persistence.

This keeps the implementation small and aligned with current aidir architecture.

## Goals

1. Add one general-purpose token-authenticated HTTP API tool worker.
2. Allow several named API connectors to be configured in `config.json5`.
3. Keep connector auth and request templates in config, not in prompts.
4. Make the tool injectable into agents exactly like existing `search`, `fetch`, and `selftest` tools.
5. Return a normalized result envelope that is easy for models to consume.
6. Keep the implementation autonomous and isolated from unrelated modules.

## Non-Goals for v1

1. Browser automation.
2. HTML scraping of arbitrary websites.
3. Automatic login flows through forms or cookies captured by a browser.
4. Generic write/update/delete support across all remote APIs.
5. Background synchronization or ETL.
6. A connector SDK for arbitrary Python plugins.
7. A general workflow/orchestration engine for external APIs.

## Problem Statement

Agents sometimes need access to account-scoped or member-only content that is available through HTTP APIs protected by bearer tokens or similar token-based credentials.

Current poor options:

1. put secrets into prompts or tool arguments;
2. create one-off workers for every website;
3. use browser automation for structured API data;
4. build a separate external subsystem for a capability that fits the current tool model.

The right v1 abstraction for aidir is a configurable HTTP API tool worker.

## Architecture Position

The new component should live at the same layer as current tool workers.

Conceptually:

1. `workers_loader` loads `http_api` like any other worker;
2. `workers.items.http_api` stores its config;
3. an agent worker such as `openaix` exposes it through `workers.items.openaix.tools.http_api`;
4. `context_add_internal_tools` injects the tool schema into the model context;
5. the agent calls the tool;
6. `OpenAIxWorker` runs it via standard `Task_tool` execution;
7. the worker makes an outbound HTTP call and returns normalized data.

This means v1 needs no new scheduler rules and no changes to the task model beyond normal tool execution.

## Autonomy and Minimal Coupling Rules

The worker should own as much of its behavior as possible.

It should depend only on:

1. `BaseToolWorker` and `WorkerResult`;
2. normal worker config from `workers.items.http_api`;
3. common logging utilities if needed;
4. `httpx` for outbound requests.

It should not depend on:

1. endpoint-specific code;
2. agent-specific business logic;
3. special scheduler behavior;
4. `external_mcp` internals;
5. web UI modules;
6. connector-specific hardcoded logic spread around the repository.

If a feature would require deep cross-module integration, it should be deferred or marked `TBD`.

## Proposed Worker Identity

Suggested worker layout:

1. `workers/tool/http_api/app.py`
2. optional `workers/tool/http_api/config.json5`

Suggested tool name exposed to agents:

1. `http_api`

Recommended `get_tool_description()` contract:

```json
[
  {
    "name": "http_api",
    "description": "Call a configured token-authenticated HTTP API connector and return normalized structured data.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "connector": {"type": "string"},
        "operation": {"type": "string"},
        "params": {"type": "object"},
        "page_token": {"type": "string"},
        "limit": {"type": "integer"}
      },
      "required": ["connector", "operation"]
    }
  }
]
```

Use one tool with explicit `connector` and `operation` fields rather than dynamically creating a separate tool per connector in v1.

Reason:

1. keeps worker schema stable;
2. avoids dynamic tool-name explosion;
3. minimizes code and configuration coupling;
4. makes audit/logging simpler.

## Proposed Configuration Model

To stay aligned with aidir, configuration should live under the worker config path, not in a new unrelated top-level root block.

Recommended shape:

```json5
{
  "workers": {
    "items": {
      "http_api": {
        "enabled": true,
        "request_timeout": 30,
        "max_retries": 2,
        "retry_backoff_seconds": 1,
        "user_agent": "aidir-http-api/1.0",
        "max_response_chars": 50000,
        "connectors": {
          "groupsio_cryonet3": {
            "enabled": true,
            "base_url": "https://groups.io/api/v1",
            "auth": {
              "type": "bearer_env",
              "env": "GROUPS_IO_API_KEY"
            },
            "defaults": {
              "group_name": "cryonet3"
            },
            "operations": {
              "list_topics": {
                "method": "GET",
                "path": "/gettopics",
                "query": {
                  "group_name": "{group_name}"
                },
                "pagination": {
                  "type": "cursor",
                  "request_param": "page_token",
                  "response_field": "next_page_token"
                },
                "result_path": "topics",
                "item_id_field": "id"
              },
              "get_message": {
                "method": "GET",
                "path": "/getmessage",
                "query": {
                  "group_name": "{group_name}",
                  "msg_num": "{msg_num}"
                },
                "result_path": "message",
                "mode": "item"
              }
            }
          }
        }
      },
      "openaix": {
        "tools": {
          "http_api": {
            "worker": "http_api"
          }
        }
      }
    }
  }
}
```

This keeps the entire feature inside the normal worker config tree and avoids introducing a separate subsystem root.

## Auth Model

v1 should support only token-like credential forms that fit simple autonomous configuration.

Required auth types:

1. `bearer_env`
2. `header_env`
3. `query_env`
4. `bearer_file`
5. `header_file`

Behavior:

1. env-based auth loads a secret from an environment variable;
2. file-based auth loads a secret from a local file path;
3. the tool injects the secret into headers or query params;
4. the secret is never added to returned data, task payload, or tool descriptions.

Example:

```json5
"auth": {
  "type": "header_env",
  "header": "X-API-Key",
  "env": "GROUPS_IO_API_KEY"
}
```

`TBD`: should file-based secrets be allowed to come from arbitrary absolute paths, or should v1 restrict them to a known secrets directory for safer deployments?

## Operation Model

Each connector defines a bounded set of named operations.

Each operation may define:

1. `method`
2. `path`
3. `query`
4. `headers`
5. `json`
6. `pagination`
7. `result_path`
8. `mode`
9. `allowed_params`
10. `timeout_seconds` override

Template substitution should be simple and local.

Recommended source order for template values:

1. operation call `params`
2. connector `defaults`
3. optional worker-level defaults

If a required template variable is missing, the tool must fail with a structured validation error.

`TBD`: should v1 support nested template expansion inside JSON bodies, or should it initially support only flat string substitution to keep implementation small?

## Pagination Model

v1 should support bounded pagination, but in a simple form.

Recommended supported pagination types:

1. `none`
2. `cursor`
3. `page`
4. `offset_limit`

Recommended v1 execution rule:

1. a normal call returns one page plus normalized paging metadata;
2. optional bounded auto-iteration may be controlled by `limit` or `max_pages`;
3. the worker must always enforce a hard upper bound to prevent runaway loops.

Suggested normalized paging output:

```json
{
  "paging": {
    "type": "cursor",
    "next_page_token": "abc123",
    "has_more": true,
    "pages_fetched": 1,
    "items_returned": 50
  }
}
```

`TBD`: should v1 expose auto-pagination at all, or should it always return one page and require the model to call the tool again with `page_token`?

## Response Envelope

The tool should return a normalized envelope that is predictable for agents.

Recommended success shape:

```json
{
  "ok": true,
  "connector": "groupsio_cryonet3",
  "operation": "list_topics",
  "status_code": 200,
  "item": null,
  "items": [],
  "paging": {},
  "meta": {
    "content_type": "application/json",
    "duration_ms": 123,
    "request_id": "..."
  }
}
```

Recommended failure shape:

```json
{
  "ok": false,
  "connector": "groupsio_cryonet3",
  "operation": "list_topics",
  "error": {
    "code": "HTTP_API_HTTP_ERROR",
    "message": "Remote API returned HTTP 403",
    "status_code": 403,
    "details": "..."
  }
}
```

Rules:

1. truncate oversized raw response text before returning it;
2. keep headers/body previews bounded;
3. never include auth secrets in any error details;
4. prefer structured JSON extraction when possible;
5. include both `item` and `items` fields consistently, using one and leaving the other empty.

## Read/Write Scope

v1 should be read-first.

Default rule:

1. support `GET` only in the first implementation unless a concrete must-have write case is approved.

Optional extension inside the same architecture:

1. allow `POST` for read-like search/query APIs that require request bodies;
2. continue to reject mutating operations by default.

`TBD`: should v1 allow `POST` at all when the remote API uses POST for safe query endpoints, or should the first implementation hard-limit itself to GET for maximum safety?

## Retry and Timeout Behavior

The worker should own its own HTTP retry behavior.

Recommended worker-level settings:

1. `request_timeout`
2. `max_retries`
3. `retry_backoff_seconds`

Retry should apply only to transport-like cases:

1. connect failures;
2. read timeouts;
3. optional 429 and 5xx when enabled.

Do not retry by default:

1. 4xx auth failures;
2. connector validation errors;
3. malformed local configuration.

`TBD`: should 429 handling be generic in v1, including optional parsing of `Retry-After`, or should that be deferred to keep the implementation minimal?

## Injection Model

The tool must be injectable through the current mechanism, not through a new endpoint or plugin layer.

Expected configuration pattern:

```json5
"workers": {
  "items": {
    "openaix": {
      "tools": {
        "http_api": {
          "worker": "http_api"
        }
      }
    }
  }
}
```

This means:

1. no new MCP protocol surface is required for internal agent use;
2. the tool can also be exposed through the existing MCP endpoint if desired, because it is still a normal tool worker;
3. tool discovery remains consistent with the rest of aidir.

`TBD`: should v1 expose this worker through MCP by default, or should it remain internal-only unless explicitly enabled in endpoint config?

## Required Code Changes

### 1. Add worker

Add a new worker:

1. `workers/tool/http_api/app.py`

This worker should:

1. inherit `BaseToolWorker`;
2. expose one tool description named `http_api`;
3. parse worker config and connector config;
4. validate connector and operation selection;
5. resolve auth secrets;
6. render request templates;
7. send the HTTP request through `httpx`;
8. normalize the response into the agreed envelope.

### 2. Optional local worker config

Optionally add:

1. `workers/tool/http_api/config.json5`

This file may contain defaults only. Environment-specific connector data should still live in root `config.json5` overrides.

### 3. Config example updates

Update example configuration to show:

1. `workers.items.http_api`
2. one sample connector
3. injection into `workers.items.openaix.tools`

### 4. Documentation updates

Update human-facing docs where appropriate:

1. `README.md` minimal mention of the new tool worker and its purpose;
2. optionally a short config example;
3. no large extra documentation set is required in v1.

## Explicitly Unnecessary Changes

The following should not be required for v1:

1. changes to scheduler resource logic;
2. changes to `Task_tool` structure;
3. changes to endpoint task routing logic;
4. changes to Redis schema;
5. new UI pages;
6. a new cross-worker coordination layer.

If implementation starts requiring these, the design is drifting away from the intended lightweight scope.

## Security Rules

The implementation must follow these rules:

1. secrets never appear in tool descriptions;
2. secrets never appear in agent-visible normalized results;
3. secrets never appear in error messages;
4. logs must redact auth values;
5. connector config must whitelist what operations are callable;
6. the model must not be allowed to supply an arbitrary URL directly in v1.

That last point is important.

The tool should call only configured connectors and operations. It must not become a generic unrestricted HTTP client.

## Suggested Validation and Tests

Minimum test scope:

1. config parsing for worker defaults and connectors;
2. env/file auth resolution;
3. missing secret failure behavior;
4. missing connector and missing operation behavior;
5. template substitution and validation;
6. HTTP success normalization for list and item modes;
7. HTTP error normalization;
8. pagination metadata extraction;
9. secret redaction in logs/errors;
10. tool description shape.

Recommended test style:

1. focused unittest module similar to current worker tests;
2. fake `httpx` client or patch-based unit tests;
3. no live external API dependency in the default test suite.

## Implementation Constraints

To preserve the intended simplicity, v1 should respect these constraints:

1. single worker;
2. single stable tool name;
3. config-driven connectors;
4. no runtime code generation;
5. no connector-specific subclass tree unless clearly required later.

If a site cannot be represented by this model, it should be called out explicitly as unsupported or deferred.

## Open Questions / TBD

1. `TBD`: should v1 support only `GET`, or also safe `POST` query endpoints?
2. `TBD`: should auto-pagination be included in v1, or should the first version always return one page and a continuation token?
3. `TBD`: should file-based secret loading be restricted to a known secrets directory?
4. `TBD`: should v1 parse `Retry-After` for 429 responses, or keep retry logic transport-only?
5. `TBD`: should MCP exposure be enabled by default or remain opt-in?
6. `TBD`: should v1 support nested JSON-body templating, or only flat string substitution?
7. `TBD`: should per-connector response normalization allow tiny optional Python hooks, or should v1 stay config-only even if that excludes some APIs?

## Acceptance Criteria

This proposal is satisfied when:

1. aidir contains a new `http_api` tool worker;
2. the worker can be injected into `openaix` using the existing internal-tools path;
3. at least one sample token-authenticated connector can be configured without changing code;
4. the worker never exposes secrets to the model;
5. the implementation does not require new scheduler, endpoint, or task abstractions;
6. tests cover the core config/auth/request/normalization behavior.