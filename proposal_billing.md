# Proposal: Model Usage, Billing, and Speed Profiles

Status: implemented. The phases below are delivered; the deviations of the
implementation are recorded in `Implementation Notes`.

## Purpose

aidir records individual tasks, LLM-call summaries, and audit bodies, but it does
not aggregate usage per model and cannot answer the operator's basic questions:

- How many requests and tokens did this model serve today, this week, or this
  billing period?
- How much did a provider charge, and how much OpenCode Go allowance was used?
- How much of the prompt came from cache?
- How fast are prefill and generation for this model at different context
  sizes?
- Which model is failing, hitting its output limit, or becoming expensive?
- Which candidate should smart routing prefer when price and speed matter?

This proposal defines per-model metering, a local usage database, price and limit
configuration, OpenCode Go billing metadata, speed measurements, APIs, and the
future routing hook. It does not implement the feature yet.

## Goals

1. Measure model usage and speed accurately enough to compare local and remote
   models.
2. Calculate per-request, per-period, and projected cost from configured prices.
3. Read and retain OpenCode Go's provider-reported billing fields, especially
   `usage.opencode.allowance`.
4. Keep compact, durable, queryable statistics that survive restarts.
5. Expose usage through the API, Prometheus metrics, and the WebUI.
6. Make the data usable later by the smart router without changing routing
   behaviour in the first delivery.

## Non-goals

- Replacing the audit journals. Audit remains the byte-level evidence store;
  this feature stores numeric accounting and timing.
- Storing prompts, responses, file contents, credentials, or session values in
  the usage database.
- Hard-coding vendor price lists. Prices and limits are configuration.
- Implementing cost-aware routing in the first phase. The first phase records
  the data and exposes stable interfaces.
- Billing images, audio, embeddings, or non-token units before their units and
  prices are defined. Embeddings are tracked only by request/token counters
  until a separate decision is made.
- Time-of-day prices, separate cache-write prices, and image, audio, or
  embedding prices in the first release. Deferred to a future version.
- Polling the undocumented OpenCode Go usage route in the first release.
- Rebuilding past usage from existing audit journals. Statistics start at the
  deployment date.
- Budget enforcement beyond token counters and a money counter.

## Decisions Recorded

The review rounds resolved every question of the first draft. The decisions are
listed below and are implemented in the sections that follow. The deferred work
is listed in `ROADMAP.md` as B1 to B9.

- Limits and prices are configured the same way on a smart alias and on a
  concrete model. A smart alias without its own prices inherits the cost of the
  concrete models it called. This is the default and the expected mode.
- A candidate that exceeded its budget is refused with `BUDGET_EXCEEDED`. The
  model stays stopped until the operator resets the token counter, the money
  counter, or both.
- Hourly rows cover the current calendar day and the previous one. The day
  before yesterday is rolled up into a daily row after the day closes, and days
  are rolled up into wider intervals by the same rule. The WebUI opens on the
  last 24 hours.
- The money metric is selected automatically: the provider-reported allowance
  when the provider reported one, the computed cost otherwise.
- Time-of-day prices, cache-write prices, and media prices are deferred.
- Reasoning tokens are never estimated. An unknown reasoning share is stored as
  `0`.
- Tool tokens are measured from the generated tool-call text. Tool-result input
  tokens are not counted.
- A synchronous remote call without provider timings stores the total duration
  only. No prefill or generation split is invented.
- Weighted averages only; percentile histograms are deferred.
- One database file, `logs/usage.sqlite`. A separate file does not make SQLite
  queries faster, and the scan cost that matters is in Redis.
- No raw per-call table. The current hour is updated and persisted on every
  call.
- No backfill. Statistics start at the deployment date.
- A call is counted for the smart alias and for the concrete model, so the sum
  over all rows is larger than the number of client requests. An internal-call
  counter separates tool-loop calls from client-facing calls.
- Polling the undocumented OpenCode Go usage route is deferred to a future
  version.
- A smart alias shows one aggregated row. The candidate breakdown is available
  in the called models.
- External and internal calls are counted separately and summed for display.
- Embeddings and media keep request and token counters only.
- Usage is collected per envid next to the model aggregates, plus a bucket for
  calls without an envid. The first release does not expose the per-envid data
  in the WebUI or in the endpoints.
- A candidate whose limit is exhausted is not selected by smart routing. Cost
  and speed do not influence the routing yet.
- The usage store owns the failure taxonomy. An unknown provider error shape
  becomes `other_error`.
- Tokens and money are counted only when the provider returned a response with
  usage. A failed execution does not consume the budget.
- Task failures without an LLM call are counted as their own line and never
  consume the budget.
- The `/v1/models` usage block is computed on each request. A snapshot cache
  and a `usage=0` switch are deferred.

## Current State and Evidence

- `Task` persists `llm_call_count` and a bounded `llm_call_history`
  (`core/task.py:32`, `core/worker.py:173`, `core/worker.py:247`). Each entry
  already contains the provider, model, HTTP status, duration, error code, and
  a compact `response_summary`.
- `_summarize_llm_response` (`core/worker.py:343`) keeps prompt/completion/total
  tokens and tool-call counts, but drops provider timings, cached-token details,
  and provider-reported billing fields.
- The OpenAI transport keeps the raw `usage` object
  (`workers/agent/call_openai/app.py:274`), but Anthropic conversion
  (`core/messages_protocol.py:612`) keeps only a small fixed set of counters.
- Audit records preserve request and response bodies and are indexed per task
  (`core/audit_log.py`). They are not an aggregation engine.
- `/stats` and `/metrics` expose tasks, workers, resources, and uptime only
  (`core/endpoints/endpoint_openaix.py:1024`).
- `config.models.providers.<id>.models[]` is the natural per-provider and
  per-model configuration point (`config.descriptor.json5:1092`).
- OpenCode Go currently has a provider and transport integration
  (`proposal_opencode_go.md`, `workers/agent/call_opencode_go/app.py`), but no
  billing metadata is read or stored.

## Terminology and Units

| Term | Meaning |
|---|---|
| **LLM call** | One request to one concrete provider and model. A task with a tool loop can make several LLM calls. |
| **External call** | An LLM call made directly in response to a client request. |
| **Internal call** | An LLM call made by the agent tool loop of an already accepted client request. |
| **Task request** | One client task accepted by aidir. It may produce zero, one, or many LLM calls. |
| **Prefill / input tokens** | Prompt tokens processed by the model. |
| **Cached prefill tokens** | Prompt tokens served from a provider cache. |
| **Cache write tokens** | Tokens written to a provider cache when the provider reports them separately. |
| **Generation / output tokens** | Tokens produced by the model. This includes reasoning unless the provider reports reasoning separately. |
| **Reasoning tokens** | Output tokens classified as thinking/reasoning. |
| **Answer tokens** | Generation tokens that are not reasoning, when both are known or estimated. |
| **Tool argument tokens** | Estimated tokens in tool-call names and arguments emitted by the model. |
| **Allowance** | OpenCode Go plan usage, reported in `usage.opencode.allowance`. It is not the same as the charged balance amount. |
| **Cost** | Money in the configured currency, normally USD per the provider's price. |
| **Context bucket** | A prompt-size interval used for speed comparison. |
| **Limit counter** | The running token and money total of one limit rule, stored next to the rule so it can be reset. |

Prices are configured per one million tokens. Costs are stored with a fixed
decimal representation to avoid floating-point drift; the implementation should
store integer micro-dollars or use `Decimal` consistently.

## Requirements

### R1: Price and limit configuration

Add validated configuration for:

- prefill, cached-prefill, generation, optional cache-write, and optional
  reasoning prices;
- optional context-size price tiers;
- an array of limit rules with a duration, token limits, and money limits;
- the action taken when a limit is exceeded.

### R2: OpenCode Go billing fields

Preserve and normalize the provider-reported billing data:

- `usage.cost` (USD actually charged or estimated);
- `usage.opencode.allowance` (Go plan allowance used);
- `usage.opencode.billing_source` (`free`, `balance`, `go`, `go-plus`, `byok`);
- `usage.opencode.list_cost`;
- `usage.opencode.estimated`;
- `usage.opencode.tokens.input`, `.output`, `.reasoning`, `.cache_read`,
  `.cache_write_5m`, `.cache_write_1h`.

The same normalized fields must survive OpenAI Chat Completions, Anthropic
Messages, synchronous, and streaming responses.

### R3: Durable usage counters

Store aggregate model statistics in a local database and load the active
windows at startup. An hour with no traffic writes no row.

### R4: Per-period statistics

Support at least hourly, daily, and monthly buckets. Retain hourly data for the
current and the previous calendar day, roll it into daily data, and roll daily
data into monthly data. Archival must be idempotent and crash-safe.

### R5: Speed profiles

Store latency, time to first token, prefill throughput, generation throughput,
and reasoning share, with separate accumulators for prompt-size buckets.

### R6: Cost and allowance calculation

Use provider-reported cost when present; otherwise compute it from configured
prices and normalized token counters. Keep both values and mark which one was
authoritative. For OpenCode Go, track allowance separately from balance cost.

### R7: APIs and UI

Expose usage summaries, time series, model comparisons, limit state, and speed
profiles through authenticated WebUI APIs. Add the `LLM statistic` WebUI tab,
extend `/stats` and `/metrics`, and add the same per-model usage summary to the
OpenAI `/v1/models` and `/v1/models/{model_id}` responses under the existing
`aidir` extension.

### R8: Safe defaults

Metering is enabled without prices or limits. Cost is shown as unknown until a
model has a price. Limit rules default to warning, not blocking, unless the
configuration opts into enforcement. A configured budget that opts into `block`
stops the model until the operator resets the counters.

### R9: No sensitive content

Only counts, durations, model identifiers, status codes, and configured labels
are stored. Prompt text, response text, file content, API keys, and session
values never enter the usage database or Prometheus labels.

A client identifier such as `envid` is an identifier, not content. It is kept
in the envid rows only, it is accepted only when it is a known envid of the
registry, and it never becomes a Prometheus label.

## Proposed Configuration

### Root settings

```json5
{
  "billing": {
    "enabled": true,
    "database": "logs/usage.sqlite",
    "timezone": "UTC",
    "currency": "USD",
    "retention": {
      "hourly_days": 2,      // calendar days: the current one and the previous one
      "daily_months": 24
    },
    // Optional global limits, evaluated in addition to provider/model rules.
    "limits": []
  }
}
```

`database` is relative to the aidir root when it is not absolute. The directory
must be private (`0700`); the database file must be `0600`, matching the audit
directory policy.

`retention.hourly_days` counts calendar days, not a rolling interval: hourly
rows exist for the current day and the day before it, so the store always holds
between 24 and 47 hourly buckets and at least one full day of history.

### Prices and limits on a model

The `billing` block has the same shape for a concrete provider model and for a
smart alias. The difference is the price source: a concrete model is priced
from its own prices, a smart alias is priced from the concrete models it
called unless it declares its own prices.

```json5
{
  "models": {
    "providers": {
      "opencode_go": {
        "api": "opencode-go",
        "billing": {
          "usage_source": "opencode",   // reads usage.cost and usage.opencode.*
          "limits": [
            {
              "id": "go_monthly",
              "period": "1M",
              "mode": "calendar",
              "max_prefill_tokens": 20000000,
              "max_cached_prefill_tokens": 80000000,
              "max_generation_tokens": 3000000,
              "max_money": 60,
              "money_metric": "auto",
              "action": "warn"
            }
          ]
        },
        "models": [
          {
            "id": "glm-5.3",
            "alias": "opencode-glm-5.3",
            "billing": {
              "prices": {
                "prefill_per_million": 1.40,
                "cached_prefill_per_million": 0.26,
                "generation_per_million": 4.40
              },
              "price_tiers": [
                {
                  "max_prompt_tokens": 256000,
                  "prices": {
                    "prefill_per_million": 1.40,
                    "cached_prefill_per_million": 0.26,
                    "generation_per_million": 4.40
                  }
                },
                {
                  "min_prompt_tokens": 256001,
                  "prices": {
                    "prefill_per_million": 2.80,
                    "cached_prefill_per_million": 0.52,
                    "generation_per_million": 8.80
                  }
                }
              ],
              "limits": [
                {
                  "id": "model_5h",
                  "period": "5h",
                  "mode": "rolling",
                  "max_prefill_tokens": 1000000,
                  "max_cached_prefill_tokens": 4000000,
                  "max_generation_tokens": 150000,
                  "max_money": 12,
                  "money_metric": "auto",
                  "action": "warn"
                }
              ]
            }
          }
        ]
      }
    }
  }
}
```

### Prices and limits on a smart alias

A smart alias accepts the same `billing` block. Its limits apply to the alias
as a whole; the concrete candidates keep their own limits.

```json5
{
  "models": {
    "providers": {
      "smart": {
        "api": "smart",
        "models": [
          {
            "id": "opencode-fast",
            "alias": "fast",
            "items": [
              {"provider": "opencode_go", "model": "glm-5.3"},
              {"provider": "local", "model": "qwen3.5:14b"}
            ],
            "billing": {
              "limits": [
                {
                  "id": "alias_daily",
                  "period": "1d",
                  "mode": "calendar",
                  "max_money": 5,
                  "action": "block"
                }
              ]
            }
          }
        ]
      }
    }
  }
}
```

The alias row has no prices of its own here. Its cost is the sum of the costs
of the concrete models it actually called, so the alias always reflects what
was really spent. When an alias declares its own prices, those prices are used
instead, and the inherited candidate cost is still stored for comparison.

### Limit rule semantics

- `period`: one of `1h`, `5h`, `1d`, `1w`, `1M`, or an explicit duration. The
  parser must reject ambiguous values.
- `mode`: `rolling` or `calendar`. A rolling window looks back exactly one
  duration; a calendar window is aligned to the configured timezone.
- Any absent token or money field means that dimension is not limited.
- `money_metric`: `auto`. The allowance reported by OpenCode Go is used when
  the provider reported one, the computed cost otherwise. `list_cost` is
  informational and is never used as a limit metric.
- `action`: `warn`, `block`, or `route`.
  - `warn` records a visible warning and keeps serving.
  - `block` refuses the request with `BUDGET_EXCEEDED` and keeps the model
    stopped until the counters are reset.
  - `route` makes a smart-model candidate ineligible while the limit is
    exceeded.
- A request can be covered by several rules at once. The most restrictive action
  wins. Rules with the same id are a configuration error.
- The default is `warn`; no existing installation changes behaviour after
  upgrade.
- A rule is attached to a concrete provider model, to a smart alias, or to the
  whole instance through `billing.limits`. Rules on different levels are
  evaluated together and the most restrictive action wins.

### Limit enforcement and reset

A budget has exactly two dimensions in the first release: token counters and a
money counter. There is no request-count limit and no per-client limit.

When a limit is reached:

- a direct request for the concrete model is refused with HTTP `429` and the
  error code `BUDGET_EXCEEDED`;
- a smart alias drops the over-budget candidate from its candidate list; when
  no candidate is left, the request is refused with the same error;
- the model stays stopped until the operator resets the counters. The blocked
  state is sticky: it does not expire when the window rolls over, and it
  survives a service restart.

The routing exclusion is the behaviour of the `block` and `route` actions. The
`warn` action never changes the routing: the model keeps serving and the
candidate stays eligible.

The blocked state and the counters live in the usage database next to the rule
they protect, so a restart cannot silently re-enable a stopped model.

Error contract: an HTTP `429` in every protocol, with the OpenAI error shape,
because that is the status and the type that OpenAI-compatible clients already
treat as a quota refusal.

```json5
{
  "error": {
    "message": "Budget limit 'alias_daily' exceeded: money 5.000000/5.000000 USD",
    "type": "insufficient_quota",
    "code": "BUDGET_EXCEEDED",
    "param": null
  }
}
```

The Anthropic Messages endpoint keeps its own envelope and maps the same
condition to `429` with `type: rate_limit_error`. The Ollama and OpenAIx
endpoints return `429` with the OpenAI-shaped error object. The endpoint code
must add `BUDGET_EXCEEDED` to the `429` branch, because an unknown error code
currently falls through to `502`
(`core/endpoints/endpoint_ollama.py:1239`).

There is no single provider standard for this condition, so the aidir code is
stable and documented:

| Provider | Status | Type | Code or message |
|---|---|---|---|
| OpenAI | 429 | `insufficient_quota` | `credit_balance_exhausted`, `organization_spend_limit_exceeded`, `project_spend_limit_exceeded`, `organization_usage_limit_exceeded` |
| Anthropic | 400, documented as 402 | `invalid_request_error`, documented as `billing_error` | "Your credit balance is too low to access the Anthropic API." |
| Anthropic tier cap | 429 | `rate_limit_error` | `details.error_code: enforced_spend_limit_reached` |
| OpenRouter | 402 | `payment_required` | "Insufficient credits", `error.metadata.limit_source` |
| Google Gemini | 429 | `RESOURCE_EXHAUSTED` | quota exhausted |

A billing error reported by the upstream provider keeps the provider code and
is classified as `allowance_exhausted` in the failure taxonomy. It is never
rewritten into `BUDGET_EXCEEDED`, because it is not an aidir limit.

### Reset operations

Two reset operations exist per rule:

- `reset=tokens` clears the token counters of the rule: the prefill counter,
  which includes cached prefill, and the generation counter;
- `reset=money` clears the money counter of the rule.

The reset clears the counters of the current window and clears the blocked
state of the rule. It does not change the stored aggregates, so the history
stays intact.

```text
POST /api/usage/limits/reset
{"rule_id": "alias_daily", "counter": "tokens" | "money" | "all"}
```

The `LLM statistic` tab shows one reset button per counter of a rule, next to
the limit bar, and asks for confirmation before the reset.

## OpenCode Go Billing Metadata

### Response fields

OpenCode Console responses already carry provider-reported billing beside the
normal usage object:

```json5
"usage": {
  "prompt_tokens": 1200,
  "completion_tokens": 300,
  "cost": 0.00412,
  "opencode": {
    "billing_source": "go",
    "list_cost": 0.00412,
    "allowance": 0.00274,
    "estimated": false,
    "tokens": {
      "input": 1200,
      "output": 300,
      "reasoning": 120,
      "cache_read": 8000,
      "cache_write_5m": 0,
      "cache_write_1h": 0
    }
  }
}
```

Normalize this into an internal billing block:

```json5
"billing": {
  "reported_cost_usd": 0.00412,
  "computed_cost_usd": 0.00412,
  "allowance_usd": 0.00274,
  "list_cost_usd": 0.00412,
  "billing_source": "go",
  "estimated": false,
  "cache_read_tokens": 8000,
  "cache_write_tokens": 0,
  "reasoning_tokens": 120
}
```

### Compatibility rules

- Preserve provider billing fields through all protocol conversions, including
  `_anthropic_usage_to_internal` and `_internal_usage_to_anthropic`.
- Accept both `usage.opencode.allowance` and a top-level
  `opencode.allowance` if a gateway moves the field.
- Ignore malformed or negative values; keep the raw provider usage in the audit
  event, not in the aggregate row.
- A reported `cost: 0` is authoritative when the provider sends it. It means the
  request was free or covered by a subscription.
- For OpenCode Go, `allowance` is the metric that moves the plan window; `cost`
  may stay zero.

### Optional provider usage endpoint

OpenCode Go also exposes an undocumented read-only usage route,
`GET {baseUrl}/v1/usage`, returning rolling, weekly, and monthly percentages
with reset times. Polling it is deferred to a future version and is recorded
in `ROADMAP.md`. The route is not part of the public contract, so the poll
must stay best-effort and must never replace local counters.

The documented Go plan windows are 5-hour (20% of the model's monthly limit),
weekly (50%), and monthly (100%), so the default provider-side limit rules
should mirror those proportions when an operator configures Go budgets.

## Usage Database

### Storage choice

Use SQLite in WAL mode, following the audit-index pattern. The database is a
single local file owned by the service user. It is not a replacement for Redis:
Redis remains the task/session store; SQLite is the aggregated accounting store.

Default path: `logs/usage.sqlite`. One file is enough. A separate file per
granularity or per provider does not make SQLite faster: the query cost is
dominated by the number of scanned rows, not by the file layout, and a second
file only adds a second connection and a second lock domain. The scan cost
that matters in this project is in Redis, not in SQLite. A separate `data/`
directory can still be introduced later if stats must be backed up separately
from logs; the database path is a configuration value for exactly that reason.

The write path is one small transaction per completed LLM call. The volume is
low: one row update per model per hour, plus one limit-counter update per
matching rule. An index on
`(granularity, bucket_start, provider_id, model_id)` covers the read paths.

### Schema

#### `usage_call_ids` (idempotency)

One row per finalized LLM call, used only to detect a duplicate finalization:

```text
usage_event_id TEXT PRIMARY KEY
created_at TEXT
```

`usage_event_id` is `task_id:call_index` plus a stable suffix. The writer
inserts the id with `INSERT OR IGNORE` and updates the aggregates only when
the insert changed a row. Rows older than the hourly retention are deleted by
the daily rollup, so the table stays small.

There is no raw per-call statistics table. The counters are aggregated into the
current hour row on every call and persisted immediately, so there is nothing to
re-aggregate after a statistics bug. The audit journals keep the raw bodies, so
a manual rebuild stays possible in principle.

#### `usage_hourly`, `usage_daily`, `usage_monthly`

Aggregates with a unique key:

```text
(granularity, bucket_start, provider_id, model_id, model_alias,
 model_kind, protocol, request_kind, status_group)
```

`model_kind` is `concrete` for a provider model and `smart` for an alias. The
alias row holds the calls made through the alias; the concrete row holds the
calls that reached that model.

A third kind, `client`, holds the task outcomes that have no LLM call:
`provider_id` is empty, `model_alias` is the requested model, and the row
carries only `client_requests`, `successes`, `failures`, `canceled` and the
task failure classes. It carries no token, cost, or speed counters, so it
never affects a budget and never mixes with the model rows.

Each row stores counters and sums:

- `calls`, `external_calls`, `internal_calls`, `client_requests`, `successes`,
  `failures`, `canceled`;
- `length_finish`, `http_4xx`, `http_5xx`, `timeout`, `unreachable`,
  `invalid_response`, `rate_limited`, `allowance_exhausted`, `other_error`;
- `duration_ms_sum`, `queue_ms_sum`, `ttft_ms_sum`, `prefill_ms_sum`,
  `generation_ms_sum`;
- `prompt_tokens`, `cached_prompt_tokens`, `cache_write_tokens`,
  `completion_tokens`, `reasoning_tokens`, `answer_tokens`;
- `tool_calls`, `tool_argument_tokens_estimated`;
- `input_files`, `input_file_bytes`;
- `reported_cost`, `computed_cost`, `allowance`, `list_cost`, `cache_savings`;
- `first_seen_at`, `last_seen_at`.

Cost columns use integer micro-dollars or a fixed decimal text type. Do not use
binary floating point for durable money totals.

A call is counted twice on purpose: once against the alias the client asked
for, once against the concrete model that served it. The sum over all rows is
therefore larger than the number of client requests, and the difference is the
tool-loop traffic.

- `external_calls` counts the calls that directly serve a client request.
- `internal_calls` counts the calls made by the agent tool loop.
- `calls` is the sum of both.
- `client_requests` counts the distinct client requests that produced at least
  one call, once per request.

The API can return `calls`, `external_calls`, or `internal_calls` separately,
and the display sums them when the operator asks for all traffic.

#### `speed_buckets`

Aggregates by period and context bucket:

```text
(granularity, bucket_start, provider_id, model_id, context_bucket)
calls, prompt_tokens, generation_tokens, reasoning_tokens,
prefill_ms, generation_ms, ttft_ms, total_ms
```

Rates are computed from summed tokens and summed milliseconds, so a slow request
does not distort the average the way an average-of-ratios would.

#### `usage_limit_counters`

The running counters of every limit rule, plus the blocked state:

```text
rule_id TEXT
window_start TEXT
scope_id TEXT
prefill_tokens INTEGER
cached_prefill_tokens INTEGER
generation_tokens INTEGER
money_usd TEXT
blocked INTEGER
updated_at TEXT
PRIMARY KEY (rule_id, window_start, scope_id)
```

`window_start` is the start of the window the counters belong to: the calendar
boundary for a calendar rule, the sliding window start for a rolling rule. A
new window starts a new counter row, so a rolling window expires by itself. A
reset clears the counters of the current window and clears `blocked`.

The counters are updated on every call that matches the rule, together with the
aggregates. Limit evaluation reads this table and not the aggregates, so a
reset is immediate and does not rewrite history.

#### `usage_envid`

The same counters, split by envid:

```text
(granularity, bucket_start, provider_id, model_id, envid)
calls, external_calls, internal_calls,
prompt_tokens, cached_prompt_tokens, completion_tokens,
reported_cost, computed_cost, allowance
```

The table is written next to the model aggregates, in the same transaction, so
the two stay consistent. Only the budget-relevant numbers are kept here.

The envid is collected at the call finalization point from the task payload,
not from `Task.envid`, which is never populated at runtime. The value is
stripped and accepted only when it is a known envid of the registry;
otherwise the call is counted in the empty bucket. The empty bucket is the
"no envid" line and holds the calls of anonymous requests and of the calls
without an envid.

The registry is filled from the configuration at startup, so the number of
stored envids is bounded by the configuration and the cardinality cannot grow
with anonymous traffic. The first release stores this data and does not
expose it in the WebUI or in the endpoints.

### Bucket policy

- Buckets are keyed by UTC timestamps; display uses the configured timezone.
- A row is created only when at least one event belongs to that hour/day/month.
- The hourly rows cover the current calendar day and the previous one, so the
  store holds between 24 and 47 hourly buckets and at least one full day of
  history at all times.
- When a calendar day closes, its hourly rows are summed into one daily row.
  The rollup runs in the service process, at startup and then shortly after
  midnight in the configured timezone.
- Daily rows are rolled up into monthly rows at the month boundary, by the same
  rule.
- Rollups are idempotent upserts: rerunning a rollup must not double count.
- Hourly rows older than the retention window are deleted after a successful
  rollup, and the idempotency ids with them.
- Daily rows older than `retention.daily_months` are deleted after a successful
  monthly rollup.
- Monthly rows are retained indefinitely unless a future retention rule says
  otherwise.
- There is no raw event table and no raw retention window.

### Startup and writer

- Open and migrate the database during `Core` initialization.
- Load the active limit windows, the limit counters, and the blocked state into
  memory.
- Update and persist the aggregate row on every completed LLM call, in one
  small transaction.
- A write failure must not fail the client request. It increments a visible
  metering-health counter and is retried on the next call.
- The service process is the only writer. The external cron process must not
  open the usage database for writes while the service is running, so the
  rollup runs inside the service process.

## What a Record Contains

A record is the aggregate row of one bucket. The identity and outcome fields
below are the inputs of the aggregation, not stored per call:

- **Identity:** bucket start, provider id, upstream model id, client-facing
  alias, model kind, protocol, request kind, stream flag.
- **Outcome:** successes, failures, canceled, failure classes, finish reason
  counters, length stop counters, HTTP status classes, normalized error codes.
- **Usage:** prefill, cached prefill, cache write, generation, reasoning, answer,
  total tokens, tool calls, measured tool-call tokens.
- **Calls:** external calls, internal calls, client requests.
- **Files:** number and bytes of uploaded/attached files, never their content.
- **Timing:** queue wait, total duration, time to first token, prefill time,
  generation time, model start/load time when known, timing source.
- **Billing:** reported cost, computed cost, allowance, list cost, billing source,
  estimated flag, cache savings.
- **Speed:** prefill tokens/s, generation tokens/s, context bucket.
- **Quality signals:** empty response, truncated response, tool-loop limit,
  repeated retry.

The per-call values are added to the current bucket row on every call and are
not stored separately.

## Cost Calculation

For each LLM call:

```text
cached = cached_prompt_tokens
billable_prefill = max(0, prompt_tokens - cached)
billable_generation = completion_tokens
```

Choose the price tier by prompt size. Compute:

```text
computed =
    billable_prefill * prefill_price / 1_000_000
  + cached * cached_prefill_price / 1_000_000
  + billable_generation * generation_price / 1_000_000
  + cache_write_tokens * cache_write_price / 1_000_000
```

`cache_write_price` is optional and is `0` when the price set does not define
it, because the first release has no separate cache-write price.

Use `reported_cost` when the provider sends a valid value; keep `computed_cost`
for comparison and for dashboards that ask "what would list price be?".

The first release has one price per token kind and an optional context-size
tier. There is no time-of-day pricing, no separate cache-write price, and no
media price. The computed cost is therefore an approximation and must be
labelled as one.

The money used by a limit rule is the provider-reported allowance when the
provider reported one, and the computed cost otherwise.

For OpenCode Go:

- `cost` may be zero because the subscription covers the request;
- `allowance` moves the Go plan windows;
- `list_cost` shows what the same request would cost at list price;
- limit rules use the allowance for this provider.

A smart alias is priced from the concrete models it called: the alias cost is
the sum of the computed or reported cost of those calls. When the alias
declares its own prices, those prices are used for the alias row instead, and
the inherited candidate cost is kept for comparison.

Derived values:

- cache hit rate = cached / prompt tokens;
- effective input price = cost / billable tokens;
- cost per successful task;
- spend rate per hour/day/month;
- projected period spend from the current pace, clearly marked as a projection.

## Speed Measurement

### Sources and precedence

1. **Provider timings** when present (for example llama.cpp `timings` with
   `prompt_ms`, `prompt_per_second`, `predicted_ms`, `predicted_per_second`,
   `cache_n`).
2. **Stream timing** for remote OpenAI/Anthropic streams: measure the first
   content/reasoning/tool delta for TTFT, and the last delta for total.
3. **Total-only fallback** for synchronous remote responses: total duration and
   completion tokens produce an end-to-end estimate only.

Every stored rate carries `timing_source` so a provider-reported value is never
compared silently with an estimate.

### Metrics

- `ttft_ms`: request start to first generated content/reasoning/tool token;
- `prefill_ms` and `generation_ms` when separable;
- `prefill_tps = prompt_tokens / prefill_seconds`;
- `generation_tps = generation_tokens / generation_seconds`;
- `reasoning_share = reasoning_tokens / completion_tokens`;
- `queue_ms` separately from model execution time;
- `model_load_ms` when the local server manager reports it.

For a non-streaming remote model that returns no timings, do not invent a
prefill/generation split. Store `total_ms` and `timing_source: "total_only"`.

### Context buckets

Default intervals:

| Bucket | Prompt tokens |
|---|---|
| `0-10k` | 0 to 10,000 |
| `10-50k` | 10,001 to 50,000 |
| `50-100k` | 50,001 to 100,000 |
| `100-200k` | 100,001 to 200,000 |
| `200-350k` | 200,001 to 350,000 |
| `350-500k` | 350,001 to 500,000 |
| `500-750k` | 500,001 to 750,000 |
| `750k+` | above 750,000 |

The top bucket can be split later into `750k-1M` and `>1M` if measurements show
that models with a 1M context window behave differently. Bucket boundaries
should be configuration-defined in the implementation, with these values as the
default.

### Reasoning and tool attribution

- Prefer provider counters: OpenAI
  `usage.completion_tokens_details.reasoning_tokens`, Anthropic thinking-token
  details, and OpenCode `usage.opencode.tokens.reasoning`.
- When a provider does not report reasoning, the reasoning share is unknown and
  is stored as `0`. The reasoning text is never tokenized to guess the share,
  and the stored value is never presented as provider truth.
- `answer_tokens` is `completion_tokens - reasoning_tokens`, so an unknown
  reasoning share gives the full completion count as answer tokens.
- Tool-call count is exact.
- Tool argument tokens are measured from the generated tool-call text: the
  serialized tool name and arguments the model produced. The measurement uses a
  documented characters-to-tokens approximation and is stored as an estimate.
- Tool-result tokens are input tokens contributed by tool results in later
  turns. They are not counted, because counting them would require the tool
  output text, and tool output text never enters the usage store.

## APIs and UI

### WebUI API

Authenticated routes:

```text
GET /api/usage/summary?from=&to=&provider=&model=&alias=&request_kind=&scope=
GET /api/usage/models?from=&to=&sort=cost|tokens|speed|failures&scope=
GET /api/usage/series?metric=&interval=hour|day|month&from=&to=&scope=
GET /api/usage/speeds?provider=&model=&from=&to=
GET /api/usage/limits
POST /api/usage/limits/reset
GET /api/usage/allowance
GET /api/usage/health
```

`scope` selects the call population: `all` (default), `external`, or
`internal`. The counters are stored separately, so the scope is a choice of
column, not a different aggregation.

`GET /api/usage/allowance` returns the allowance that OpenCode Go reported in
its responses, aggregated per provider and period. The provider-side usage view
is not polled in the first release.

The summary response includes totals, per-model rows, cache hit rate, failures,
cost, allowance, and the current limit state. The series route returns only
non-empty buckets. All periods are resolved in the configured `billing.timezone`;
the default is the last 24 hours.

### OpenAI `GET /v1/models` extension

The existing OpenAI model list stays OpenAI-compatible. The added data lives
only inside the existing `aidir` extension object; no standard field changes.

Example for one model in `GET /v1/models?period=24h`:

```jsonc
{
  "id": "opencode-glm-5.3",
  "object": "model",
  "created": 1762900000,
  "owned_by": "aidir",
  "schema_version": 1,
  "aidir": {
    "schema_version": 1,
    "real_id": "glm-5.3",
    "provider": "opencode_go",
    "model": "glm-5.3",
    "resolved_provider": "opencode_go",
    "resolved_model": "glm-5.3",
    "usage": {
      "period": {"label": "24h", "from": "...", "to": "..."},
      "calls": 42,
      "external_calls": 40,
      "internal_calls": 2,
      "successes": 40,
      "failures": 2,
      "canceled": 0,
      "tokens": {
        "prompt": 125000,
        "cached_prompt": 64000,
        "cache_write": 0,
        "completion": 18000,
        "reasoning": 5000,
        "answer": 13000,
        "tool_argument_estimated": 900
      },
      "tools": {"calls": 7},
      "files": {"count": 1, "bytes": 24000},
      "cache": {"hit_rate": 0.512, "savings_usd": 0.0031},
      "speed": {
        "ttft_ms_avg": 980,
        "prefill_tokens_per_second": 4100,
        "generation_tokens_per_second": 62,
        "timing_source": "stream"
      },
      "failures_by_class": {
        "length_finish": 1,
        "http_5xx": 1,
        "timeout": 0,
        "rate_limited": 0
      }
    }
  }
}
```

Rules:

- `period` accepts the same presets as the UI (`today`, `yesterday`, `24h`,
  `7d`, `30d`, `month`) and also explicit `from`/`to` timestamps. The default is
  `24h`, the same default as the WebUI tab.
- The list endpoint returns an aggregate block per externally visible model.
  `GET /v1/models/{model_id}` returns the same block. A smart model's block is
  the aggregate for its alias; the candidate breakdown is available in the
  called models and is optional here with `include_candidates=1`.
- The full context-bucket vector is included in the single-model response by
  default and in the list only with `include_context=1`, so a large catalogue
  does not return an oversized document.
- Cost and allowance are only included when the request is authenticated with
  an existing endpoint user token. The legacy unauthenticated `/v1/models`
  response must not expose local spend or budgets. Unauthenticated callers get
  counters and speed, without `cost`, `allowance`, or limit state. This split
  is kept as it is and is not refined in the first release.
- The endpoint builds the usage block from the aggregated rows on every
  request. The calls are rare and the aggregates are already reduced, so the
  simpler implementation is used and no snapshot cache is kept. The block is
  built by one function over a time window, so a cache can be added later
  without changing the route.
- Usage data is omitted or marked unavailable when the store is unhealthy. It
  never turns a successful model listing into an error.
- No query parameter and no separate authenticated variant are added in the
  first release. A switch that turns the usage block off is deferred to a
  future version and is recorded in `ROADMAP.md`.

### WebUI API additions for the tab

The `LLM statistic` tab uses the existing `/api/usage/*` routes. The minimum
queries are:

```text
GET /api/usage/summary?from=&to=&scope=
GET /api/usage/models?from=&to=&sort=&scope=
GET /api/usage/speeds?from=&to=
GET /api/usage/limits
POST /api/usage/limits/reset
GET /api/usage/allowance
```

### Existing monitoring routes

Extend `/stats` with a bounded usage summary and `/metrics` with:

```text
aidir_model_calls_total{provider,model,status}
aidir_model_internal_calls_total{provider,model}
aidir_model_tokens_total{provider,model,kind}
aidir_model_cost_usd{provider,model,kind}
aidir_model_cache_hit_ratio{provider,model}
aidir_model_generation_tokens_per_second{provider,model,context_bucket}
aidir_model_prefill_tokens_per_second{provider,model,context_bucket}
aidir_model_limit_ratio{provider,model,limit}
```

Labels must stay bounded by configuration. Do not add task ids, client ids, or
prompt-derived labels.

### WebUI tab: `LLM statistic`

Add a new top-level tab named exactly `LLM statistic`. It is the operator-facing
view of the usage database and defaults to **the last 24 hours**; changing the
period never changes stored data, it only changes the query window.

Layout:

1. **Period bar**
   - presets: Last 24 hours (default), Today, Yesterday, Last 7 days, Last 30
     days, This month, Custom range;
   - a call-scope selector: All calls (default), External only, Internal only;
   - a visible timezone label from `billing.timezone`;
   - a refresh action and the last snapshot time.

2. **Summary cards for the selected period**
   - LLM calls and client requests, split into external and internal calls;
   - total tokens, split into prefill, cached prefill, generation, reasoning,
     answer, and estimated tool tokens;
   - total cost and OpenCode Go allowance, with `estimated` or `price unknown`
     labels;
   - cache hit rate and estimated cache savings;
   - failure rate and failure breakdown;
   - average TTFT, prefill tokens/s, and generation tokens/s.

3. **Per-model table**
   - model alias and upstream id, provider, local/remote, protocol;
   - calls, successes, failures, canceled;
   - prompt, cached prompt, cache write, generation, reasoning, answer, and tool
     token counts;
   - cost and allowance, with `unknown` when no price exists;
   - cache hit rate;
   - TTFT, prefill tokens/s, generation tokens/s;
   - failure classes (`length`, 5xx, timeout, unreachable, rate-limited,
     canceled);
   - click a row to expand speed by context bucket.
   - a smart alias is one row with the aggregated cost of the models it called;
     the candidate rows are reachable from the called models.

4. **Performance panel**
   - context-bucket table or compact bar chart;
   - provider-reported timings marked separately from estimated timings;
   - comparison of local and remote models at the same bucket;
   - a warning when a model has too few samples to compare reliably.

5. **Limits and allowance panel**
   - limit bars with used ratio and reset time;
   - a reset button for the token counters and a reset button for the money
     counter of each rule, both with a confirmation step;
   - the blocked state of a rule, shown next to the bar;
   - the OpenCode Go allowance reported in the provider responses; the
     provider-side allowance view is not polled in the first release;
   - clear labels distinguishing provider-reported allowance from local
     estimates.

6. **Empty and degraded states**
   - no traffic in the period: a single empty-state message, no fabricated zero
     rows;
   - usage database unavailable: the tab shows the error and the service keeps
     serving;
   - unpriced model: show tokens and speed, and label cost as `price unknown`.

The tab must not show prompt text or file content. It may link to Task Viewer for
task-level inspection.

## Failure Classification

The usage store owns the taxonomy. The worker reports the raw signals, and the
store maps them to a class when it writes the aggregate. One mapping table
keeps every provider consistent, and the worker does not need to know the
taxonomy.

Map outcomes to stable classes:

| Class | Examples |
|---|---|
| `length_finish` | `finish_reason=length`, output limit reached |
| `http_4xx` | invalid request, authentication, unknown model |
| `http_5xx` | provider internal error, HTTP 502 from the provider |
| `timeout` | request, stream, queue, or run timeout |
| `unreachable` | DNS, connection, TLS, provider down |
| `invalid_response` | malformed JSON, SSE, or protocol conversion |
| `rate_limited` | 429, allowance window exhausted, admission refusal |
| `canceled` | client disconnect, service restart, explicit termination |
| `other_error` | unclassified worker or internal error |

The classifier uses the existing error codes (`UPSTREAM_ERROR`,
`UPSTREAM_TIMEOUT`, `UPSTREAM_UNREACHABLE`, `UPSTREAM_INVALID_JSON`,
`QUEUE_TIMEOUT`, `CANCELLED`, and related codes) rather than inventing a
second unrelated taxonomy. An error shape that is not known becomes
`other_error`; the mapping table stays open and grows only when a new shape is
worth a class of its own.

### Additional classes worth separating

These distinctions are cheap, because the signals already exist, and each of
them answers a different operator question:

| Class | Signal | Why it is worth its own class |
|---|---|---|
| `empty_response` | the call succeeded, but the answer has no content, no reasoning, and no tool call | a silent empty answer is not a provider failure and not a length stop |
| `refused` | the provider reported a refusal or a content filter | a policy outcome is different from a broken provider |
| `budget_exceeded` | the local limit refused the call | the operator must see "we blocked it", not "the provider failed" |
| `model_unavailable` | the local model could not run: server not started, model not loaded, admission refused | a local capacity problem is different from a provider that is down |
| `tool_loop_limit` | the agent stopped on the internal tool-loop limit | a service-side stop with no provider error |

A retry is not a class. The retry attempt is a counter on the row, so the
operator sees how often a model is flaky without splitting the failure
counters.

### Signals and their priority

1. The finish reason decides `length_finish` and `refused`, and it has
   priority over the status for a successful response.
2. The error code is the primary signal for a failed call.
3. The HTTP status is the secondary signal.
4. The upstream body is inspected only when the code and the status are both
   generic, for example to separate an `invalid_request_error` caused by a
   spent balance from a malformed request.

### Tokens and money are counted only for a delivered answer

A call that produced a response with usage adds its tokens, its cost, and its
allowance to the counters. A call that failed, that never reached the
provider, or whose execution status is unknown, adds only its failure class
and its duration. Such a call never consumes the budget, because nothing is
known to have been spent.

A task failure without an LLM call is counted as its own line, in the
`client` rows of the aggregate tables. It never carries tokens and never
consumes a budget.

### Provider billing refusals

A billing refusal from the provider is `allowance_exhausted`, whatever status
the provider used for it:

| Provider | Status | Shape |
|---|---|---|
| OpenAI | 429 | `type: insufficient_quota`, `code: credit_balance_exhausted` or `organization_spend_limit_exceeded` or `project_spend_limit_exceeded` or `organization_usage_limit_exceeded` |
| Anthropic | 400 | `type: invalid_request_error`, message "Your credit balance is too low to access the Anthropic API." |
| Anthropic tier cap | 429 | `type: rate_limit_error`, `details.error_code: enforced_spend_limit_reached` |
| OpenRouter | 402 | `type: payment_required`, message "Insufficient credits" |
| Google Gemini | 429 | `RESOURCE_EXHAUSTED` |

The provider status and code are folded into the class counters. The exact
provider code stays in the audit journal, so the original cause is not lost.

## Implementation Plan

### Phase 0: Requirements and schema approval

- The price and limit field names, the limit scope, the window semantics, and
  the default action are decided and recorded in this document.
- Confirm the cost unit and the context-bucket boundaries.
- Confirm the schema of the aggregate rows, the idempotency table, and the
  limit counters.
- Record approved product decisions in `SPEC_.md` only after implementation.

### Phase 1: Normalize usage and timing

- Extend `_summarize_llm_response` and the internal response shape.
- Preserve provider usage and billing fields through OpenAI, Anthropic, and
  streaming conversions.
- Add TTFT and stream duration measurement.
- Add llama.cpp timing extraction.
- Add per-call usage extraction at `_finalize_llm_call`, including the call
  scope (external or internal), the envid, and the tool-call text measurement.
- Apply the failure taxonomy in the usage store, with the raw signals
  (status, error code, finish reason) reported by the worker.
- Tests: OpenAI JSON/SSE, Anthropic Messages, OpenCode Go allowance, llama.cpp
  timings, missing usage, malformed usage.

### Phase 2: Usage store

- Add `core/usage_store.py` with SQLite migration, aggregate upsert, the
  idempotency table, the envid table, the limit counters, startup load,
  rollup, and retention.
- Persist the aggregate update on every completed call, in one small
  transaction. No raw per-call statistics table is created.
- Run the daily rollup and the retention cleanup inside the service process, on
  an internal timer and at startup. The external cron process must not write to
  the usage database.
- Tests: restart persistence, duplicate finalization, empty-bucket suppression,
  hourly/daily/monthly rollup, retention, envid bucketing.

### Phase 3: Prices and limits

- Add descriptor and type definitions for `billing` blocks, on concrete models
  and on smart aliases.
- Add a price resolver and tier selection, and the alias price inheritance from
  the called models.
- Add cost and allowance calculation.
- Add limit evaluation, limit counters, the blocked state, the reset
  operations, and health reporting.
- Add configuration tests for invalid periods, duplicate rule ids, negative
  prices, overlapping tiers, and missing prices.

### Phase 4: Exposure

- Add `/api/usage/*` and `POST /api/usage/limits/reset`.
- Extend `/stats` and `/metrics`.
- Extend `/v1/models` and `/v1/models/{model_id}` with the bounded `aidir.usage`
  block, period parameters, authentication-aware cost fields, and the
  per-request computation of the block.
- Add the `LLM statistic` WebUI tab with the last-24-hours default, the call
  scope selector, per-model table, performance-by-context panel, limits with
  reset buttons, and allowance.
- Add a metering-health indicator when the writer or database is unhealthy.
- Update `spec/protocols.md` for the new `/v1/models` extension and the usage
  routes once the field set is approved.

### Phase 5: Routing integration

- Exclude a smart-model candidate whose limit is exhausted. The candidate
  becomes ineligible, the routing continues with the remaining candidates, and
  the request is refused with `BUDGET_EXCEEDED` only when no candidate is
  left.
- Keep resource and KV-affinity decisions authoritative.
- Cost and speed do not influence the routing yet. A cost or speed preference
  is deferred to a future version and is recorded in `ROADMAP.md`.

## Suggested Code Touch Points

- `core/worker.py`: per-call timing, response summary, billing extraction, call
  scope, envid, tool-call text measurement, raw failure signals.
- `workers/agent/call_openai/app.py`: preserve billing fields and measure TTFT.
- `workers/agent/call_anthropic/app.py`: preserve Anthropic usage details.
- `workers/agent/call_llama_cpp/app.py`: expose local server timings.
- `core/messages_protocol.py`: stop dropping provider usage fields.
- `core/queue_manager.py` / `core/scheduler.py`: terminal task classification and
  usage-event handoff.
- `core/endpoints/endpoint_ollama.py` and `core/endpoints/endpoint_openaix.py`:
  map `BUDGET_EXCEEDED` to HTTP `429`, not to the default `502`.
- `core/scheduler.py`: exclude a smart candidate whose limit is exhausted.
- New `core/usage_store.py`, `core/billing.py`, and optionally
  `core/usage_api.py`.
- `core/endpoints/endpoint_openaix.py`: `/stats`, `/metrics`, limit state in
  health.
- `webui/backend/app.py` and `webui/frontend/`: usage API and dashboard.
- `config.descriptor.json5`, `config.types.json5`, `config.json5.example`,
  `README.md`.
- Tests: `test_usage_store.py`, `test_billing.py`, `test_usage_api.py`, plus
  additions to audit, endpoint, and worker tests.

## Acceptance Criteria

1. A provider-reported `usage.cost` and `usage.opencode.allowance` survive
   synchronous and streaming OpenAI and Anthropic responses and appear in the
   stored usage counters.
2. One LLM call produces exactly one aggregate update, including retries and
   fallbacks; duplicate finalization is ignored.
3. A restart preserves all aggregates, the limit counters, and the blocked state.
4. An idle hour produces no hourly row.
5. Hourly data rolls into daily and daily into monthly without double counting.
   The hourly rows cover the current calendar day and the previous one, and the
   day before yesterday is closed into a daily row.
6. Cost is computed from configured prices when no provider cost exists, and a
   provider-reported cost is used when valid. A smart alias without its own
   prices shows the summed cost of the models it called.
7. OpenCode Go allowance is tracked separately from charged cost.
8. Speed profiles distinguish provider-reported timings from estimates and are
   available per context bucket. A synchronous remote call without provider
   timings has no prefill or generation rate.
9. Limit rules can warn, block, or remove a candidate from smart routing without
   changing default behaviour for installations without limits. A blocked model
   stays blocked until the counters are reset.
10. `/stats`, `/metrics`, the `LLM statistic` tab, and `/v1/models` expose the
    same totals for the same period.
11. The `LLM statistic` tab opens on the last 24 hours, changes period without
    restarting the service, shows per-model tokens, cost/allowance when known,
    failures, cache rate, and performance, and can switch between external and
    internal calls.
12. `GET /v1/models` and `GET /v1/models/{model_id}` include a bounded `aidir.usage`
    block for the requested period; unauthenticated callers never receive local
    cost, allowance, or budget data.
13. No prompt text, file content, credential, or session value appears in the
    usage database, logs, metrics labels, or API responses.
14. The reset operation clears the token counters, the money counter, or both,
    and clears the blocked state, without changing the stored aggregates.
15. Usage is collected per envid, and the calls without an envid are counted in
    the empty bucket. The number of stored envids stays bounded by the
    configuration.
16. A candidate whose limit is exhausted is not selected by smart routing, and
    the request is refused with `BUDGET_EXCEEDED` only when no candidate is
    left.
17. Tokens and money are counted only when the provider returned a response
    with usage. A failed call and a task failure never consume a budget.
18. A task failure without an LLM call appears as its own line and is not mixed
    with the model rows.

## TBD Questions

The first review round closed every question. The decisions are integrated in
the sections above, and the deferred work is listed in `ROADMAP.md` as B1 to
B9.

## Open Risks

- Provider-reported usage is not uniform; a field may be absent, renamed, or
  malformed. The aggregator must prefer explicit absence over guessed zero.
- Token counts and timings may mix cached and non-cached work differently across
  providers. Comparisons must keep the source labels.
- A statistics write on the hot path can become a bottleneck. The design writes
  one small transaction per completed call. If the write rate becomes a
  problem, the write path moves to a batching writer without changing the
  schema.
- Cost figures can be mistaken for invoices. The UI must label computed values
  as estimates and show the provider-reported amount separately when available.
- The computed cost is approximate: there is no time-of-day pricing, no separate
  cache-write price, and no media price in the first release.
- A blocked model stays blocked until an operator resets it, even when the
  window rolls over. An operator must know that a reset exists.
- The OpenCode Go usage route is not documented as a public contract. It must
  never be the only source of local budget state.
- An anonymous client can send an arbitrary `envid`, because the envid
  validation only runs for an authenticated request. Only a known envid is
  stored, so the cardinality stays bounded, and everything else falls into the
  empty bucket.
- The `/v1/models` usage block is computed on every request. A catalogue with
  many models and a long period can make the route slower; a snapshot cache
  is the first remedy if that becomes visible.
- The per-envid rows double the write work of the usage store. The write path
  moves to a batching writer if that becomes a problem.

## Implementation Notes

The implementation follows the phases above. The places where it differs from
the design are listed here.

- **The limit counters are keyed by the hour of the call.** The row of a rule
  is `(rule_id, window_start, scope_id)` with `window_start` the hour bucket of
  the call, so the used amount of a window is the sum of the rows inside it.
  A reset clears the rows of the current window and the blocked state. The
  effect of a rolling window is therefore exact to the hour.
- **The money counter follows the same rule** and stores the effective money of
  the call: the reported allowance when the provider reported one, the computed
  cost otherwise.
- **The three aggregate tables carry the design as written**:
  `usage_hourly`, `usage_daily`, and `usage_monthly`. The envid rows live in
  `usage_envid`, the speed profiles in `usage_speed`, the idempotency ids in
  `usage_call_ids`, and the limit counters in `usage_limit_counters`.
- **The usage block of `/v1/models` is computed per request**, as the review
  decided; the block is built by one function over a time window, so a snapshot
  cache can be added later.
- **The time to the first token is measured in the OpenAI and the Anthropic
  transports.** The Ollama NDJSON path and the llama.cpp path report their own
  timings, and a synchronous remote call without timings stores the total
  duration only.
- **The failure taxonomy is owned by the usage store.** The worker reports the
  raw signals; the store maps them. The class list carries the five classes
  the review added: `empty_response`, `refused`, `budget_exceeded`,
  `model_unavailable`, and `tool_loop_limit`.
- **The per-envid rows are written next to the model rows** and are not
  exposed by the API in the first release, as the review decided.
