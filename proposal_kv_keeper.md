# Proposal: KV-Affinity Smart Routing (KV Keeper)

## Status

Implemented. The per-alias parts of this design were superseded by W15 in
`proposal_update4.md`: affinity now depends only on the request context, the
store is global, and `max_entries_per_alias` / `honor_prompt_cache_key` are
deprecated and ignored.

Date: 2026-10-22

## Summary

Smart routing already chooses a concrete provider/model from a ranked candidate
list using resource availability and queue state. That is the right policy for
capacity, but it is blind to one of the most important properties of LLM
inference: a request that shares a prompt prefix with an earlier request is much
cheaper when it is executed by the same model instance, because the runtime can
reuse the KV cache produced by prefill.

This proposal adds a small, bounded **KV-affinity store** to smart routing. The
router fingerprints the beginning of the request context, remembers which
concrete candidate served that prefix, and prefers that candidate for later
requests with the same prefix. Affinity is advisory: it is dropped when the
candidate fails, it is overridden when the candidate's queue is
disproportionately longer than the least loaded healthy alternative, and it can
wait through a short resource conflict while the candidate's KV cache is still
warm. The wait is governed by model-aware admission history, not by raw free
memory, so a resource that is occupied by the requested model itself does not
look permanently unavailable.

The proposal also adds a second, independent mechanism needed for correct
parallel admission: a per-request **session resource size**, so that a model may
serve more than one request at a time when both its configured request limit and
the free resource memory allow it.

The two mechanisms are complementary:

1. KV affinity decides **where** a request should run.
2. Session resources decide **whether** another request may run on the same
   model at all.

## Why This Matters

Prefix caching is no longer an exotic optimization; it is a standard property
of local and hosted inference:

1. **llama.cpp server** keeps the KV cache of a slot and reuses the longest
   common prefix when `cache_prompt` is enabled. It also selects the slot whose
   cached prompt has the best similarity. The `-np` flag defines how many slots
   (parallel requests) a server has.
2. **Ollama** uses llama.cpp under the hood and automatically reuses the KV
   state of a still-loaded model when a new prompt shares its prefix.
3. **vLLM** has automatic prefix caching: KV blocks are identified by a hash
   that includes the block tokens and the hash of every preceding block, so
   matching is by exact prefix, not by semantic similarity.
4. **OpenAI** routes requests by a hash of the initial prompt prefix (typically
   the first 256 tokens), caches prompts of 1024 tokens or more, and exposes a
   `prompt_cache_key` to influence routing. This is the same idea this proposal
   implements locally.
5. NVIDIA NIM documents KV-cache reuse for prompts where more than 90% of the
   initial prompt is identical and reports roughly a 2x TTFT improvement on the
   second and later requests. The gain grows with prefix length; a public
   llama.cpp/Ollama experiment with a ~2.8k-token fixed prefix reports a 7.6x
   TTFT reduction.

The practical consequence is strong: for a harness that sends many turns of one
conversation, prefill dominates time-to-first-token, and a cache miss caused by
landing on a different executor can cost more than a short queue wait. Routing
stability is therefore part of scheduling quality, not a micro-optimization.

There is one important limitation to state up front. This proposal provides
**executor-level** affinity, not slot-level affinity. Within one executor the
runtime owns slot selection: llama.cpp chooses a slot by prompt similarity, and
Ollama may run one or several slots for a loaded model. aidir cannot pin a
request to a particular slot through the current provider APIs. A hit therefore
means "same machine and model", which is necessary for cache reuse and usually
sufficient; it is not a guarantee that the runtime will select the exact slot
that holds the prefix. If a deployment needs stronger guarantees, the provider
must expose slot pinning or a cache-aware scheduler.

## Current Baseline

The code already has the necessary building blocks:

1. `core/smart_router.py` resolves a smart alias into a concrete candidate using
   local queue state, resource availability, or a remote aidir queue-state probe.
2. `core/scheduler.py` re-resolves a queued smart route before dispatch and can
   fall back to another concrete candidate after an upstream failure.
3. `core/queue_manager.py` reports queued task counts for a resource
   requirement set.
4. `core/resources.py` / `core/resource.py` track active reservations, warm
   model occupancy, observed availability, and force-unload behavior.
5. OpenAIx exposes queue-state endpoints, including a model-only route used by
   peer aidir instances.

What is missing is any memory of the relationship between a **prompt prefix**
and the **executor** that served it, and any model-level admission based on the
memory cost of an additional concurrent request.

## Goals

1. Keep requests that share a stable context prefix on the same concrete
   provider/model whenever that executor is healthy and its queue is not
   disproportionately long.
2. Accept a short queue on the preferred executor instead of paying a cold
   prefill elsewhere.
3. Forget affinity immediately when an executor fails or is known to have lost
   its cache.
4. Wait for a **warm** executor that is temporarily short of resource memory,
   but abandon a **cold** one whose cache is already gone; use availability
   history so a transient conflict does not cause a premature move.
5. Bound the affinity store in memory and in time; never store prompt text.
6. Work for local providers and for remote aidir instances, and degrade
   gracefully when only one candidate exists.
7. Add a per-request session resource size so a model can serve several
   concurrent requests without treating every request as a full model load.
8. Keep all existing non-smart routing and single-request behavior unchanged.

## Non-Goals

1. Building an actual KV-cache storage, transfer, or paging service. The
   proposal only routes requests; the provider runtime owns the cache.
2. Token-exact fingerprinting. aidir does not know the chat template of every
   upstream and cannot tokenize for an arbitrary remote model.
3. Cross-provider cache transfer. If two candidates run different weights or
   different templates, the cache is not transferable; affinity only avoids
   unnecessary movement within a compatible group.
4. Predictive latency modeling or learning queue wait times.
5. Distributed affinity state between aidir instances. Each instance keeps its
   own routing memory; remote instances receive the affinity hint in a request
   header when possible.
6. Replacing the existing smart-router candidate probing or scheduler fallback.

## What Forms the Prompt Prefix

### Chat requests

For OpenAI/Ollama chat requests the request has no `data` wrapper. The context is
the top-level `messages` array. A message contributes its `role`, its
`content`, and any tool-call fields. In this codebase the relevant payload fields
are:

| Payload field | Enters the prompt? | Notes |
|---|---|---|
| `messages` | Yes | System, developer, user, assistant, tool messages and tool results |
| `tools` | Yes | Tool definitions are rendered into the prompt by the chat template |
| `tool_choice` | Usually | May change the rendered instruction |
| `response_format` / `format` | Usually | Structured-output schema is part of the rendered context |
| `system` (top-level, where accepted) | Yes | Prepended by the template |
| `prompt` (`/api/generate`) | Yes | Raw prompt mode |
| `input` (embeddings) | Yes | Embedding input, not chat |
| `think` | Usually | Changes the template for reasoning models |
| `temperature`, `top_p`, `top_k`, `seed`, `max_tokens`, `options`, `stream` | No for prefill | Sampling does not change the KV of the prompt |
| `model`, `worker`, `priority`, `timeout`, `queue_timeout`, `log`, `envid` | No | aidir control fields |
| `context_builder` | Indirectly | Its configuration decides what the context worker injects |

`data` appears in other protocols in this repository (for example peer resource
payloads), but it is not the chat context. For chat, hash `messages` and the
prompt-shaping fields; do not look for `data.messages`.

### What happens after routing

The endpoint resolves the route before the task is queued, but the worker later
runs the context chain (`context_builder`, internal tools, rendering) and only
then calls the model. This has two consequences:

1. The fingerprint must be computed from the **incoming request**, because that
   is the only stable, cheap point before queueing.
2. Any context builder that injects volatile content at the beginning of the
   prompt defeats prefix caching regardless of routing. Builders should be
   deterministic and append volatile data after the stable prefix.

There is also an existing behavior worth calling out: the OpenAIx worker trims a
chat history longer than 20 messages to the system messages plus the last 18
messages (`workers/agent/openaix/app.py`). Dropping early messages breaks the
contiguous prefix after the system prompt. A long harness conversation
therefore loses conversational cache reuse even when routing is stable. This is
not fixed by the KV keeper; the proposal recommends making the limit
configurable (or much larger) and documenting the trade-off, because otherwise
the keeper can only preserve the system/tool prefix.

### Multiple candidates and cache compatibility

An alias may list candidates that serve the same model id on different machines,
which is the common case in the shipped configuration. Cache reuse across those
candidates is safe when the weights and the chat template are identical. When
they are not, affinity still makes routing stable, but it does not transfer a
cache.

Add an optional `cache_group` field to a candidate. Its default is the concrete
`model` id. Affinity records are only reused for candidates in the same cache
group. This prevents a prefix that was cached for one model from being used as
the reason to route to a different model behind the same alias. The default is
only a heuristic: two deployments of the same model id with different
quantization levels or different chat templates have different KV caches and
should be given explicit, distinct `cache_group` values.

## Design Overview

```text
request
  |
  v
canonical context -> anchor length -> HMAC prefix hash
  |
  v
affinity store lookup by (requested alias, anchor length, hash)
  |
  +-- no hit ----------> existing first_available policy
  |
  +-- hit -> preferred candidate
             |
             +-- preferred healthy and not overloaded -> use it (may queue)
             |
             +-- preferred overloaded -> best alternative; remember it
             |
             +-- preferred failed / removed -> forget it; normal policy
```

The store is intentionally small: per alias it keeps a bounded number of
`(anchor length, hash) -> candidate` records. The hash is computed over a prefix
of the canonical context, never over the whole request and never over the
conversation tail, which changes on every turn.

## Fingerprint Design

### Canonical context

Build a deterministic byte string from the request fields that shape the prompt:

1. Start with a version tag, for example `kv1`.
2. Include the request kind (`chat`, `generate`, `embed`) and the requested model
   alias or id.
3. Include the prompt-shaping fields in a fixed order. Include `messages`
   without reordering the array, `tools` without reordering the array, and
   `tool_choice`, `response_format`, `format`, `system`, `prompt`, `input`,
   `think`, `raw` when present.
4. Serialize with `ensure_ascii=False`, a stable key order for objects, and
   compact separators. Do not collapse whitespace inside message content.
5. Exclude fields that cannot change the prompt: sampling parameters, stream
   flags, aidir control fields, logging, envid, timeouts, `keep_alive`, and
   message fields known to be transport-only such as `cache_control`.
6. Include the context builder configuration, because it decides what will be
   injected before execution.

Message content parts need care:

- text parts are concatenated with an explicit separator;
- image/audio parts contribute a hash of their URL or inline data plus their
  position;
- tool calls and tool results contribute their full serialized structure.

The canonicalization is a **routing identity**, not a promise that the upstream
token sequence is byte-identical. A false positive only sends a request to the
same executor and loses nothing; a false negative sends it elsewhere and loses
the cache. The algorithm is therefore tuned to avoid false negatives.

### Anchor length

Let `L` be the length of the canonical context in UTF-8 bytes. Hashing all of
`L` is wrong for conversations: the context grows on every turn, so the hash
would change and affinity would never be reused. Hash only an anchor prefix.

The anchor policy follows the requested trade-off: short contexts use a large
fraction, long contexts use a small fraction.

| Canonical context length `L` | Anchor fraction | Rationale |
|---|---|---|
| `L <= 10 KiB` | 90% | Prefill is cheap; still remember the request without a large tail |
| `10 KiB < L <= 20 KiB` | 50% | Enough to identify the conversation, less sensitive to later edits |
| `20 KiB < L <= 100 KiB` | interpolate 50% -> 10% | Long prompts are dominated by a stable head |
| `L > 100 KiB` | 10% | Routing identity needs only the stable head; cache reuse still covers the head |

Apply bounds:

- `min_anchor_bytes` (default 512) so tiny contexts do not produce an empty
  anchor;
- `max_anchor_bytes` (default 32768) so hashing cost is bounded;
- quantize the resulting length to a ladder:
  `[512, 1024, 2048, 4096, 8192, 16384, 32768]` bytes.

The ladder serves two purposes. It bounds the number of records per alias and
makes it possible to compare a new, longer request against an older record at
the older record's exact length.

Why not use token counts? aidir does not have the tokenizer or chat template of
an arbitrary remote provider. UTF-8 bytes are deterministic, cheap, and
monotonic with tokens for the same content. The anchor is an identity key, not a
claim about the exact number of cached tokens.

### Hashing

Use HMAC-SHA-256 with a per-instance secret from
`AIDIR_KV_AFFINITY_SECRET`, truncated to 128 bits. Rationale:

- deterministic across requests;
- collision probability is negligible for routing;
- plain SHA-256 of a system prompt could be attacked offline by guessing the
  prompt; HMAC prevents that if Redis or memory is exposed;
- the secret never leaves the process. If it changes, old records simply stop
  matching, which is harmless.

If the secret is not configured, fall back to SHA-256 and document that the
store should then be treated as internal-only.

### Lookup and growth

Each record stores:

```json
{
  "version": 1,
  "alias": "smart_gemma-4-26B",
  "anchor_bytes": 8192,
  "hash": "8f2c...",
  "cache_group": "juilpark/gemma-4-26B-A4B-it-heretic:q4_k_m",
  "provider": "ollama_local",
  "model": "juilpark/gemma-4-26B-A4B-it-heretic:q4_k_m",
  "last_used": 1760000000.0,
  "expires_at": 1760003600.0,
  "hits": 12,
  "last_reason": "affinity"
}
```

Lookup:

1. Compute the current anchor limit `A`.
2. For every stored anchor length `a <= A` for this alias, compute the hash of
   the first `a` bytes of the canonical context. A single pass with a copied
   hasher can produce all ladder hashes at once.
3. Collect matches. Prefer the longest matched anchor (most specific), then the
   most recently used record.
4. Validate the matched candidate: it must still exist in `items`, belong to the
   same `cache_group`, and be healthy.

This directly handles the "new context is shorter than the old one" case: longer
stored anchors are ignored, but shorter stored anchors are still compared. A
new, longer context can match an old, shorter anchor because the algorithm
recomputes the old length from the new prefix.

After a route is selected, remember the record at the current quantized anchor
length. Also keep older records until they expire or are evicted; that is what
lets a conversation match at multiple stages of growth.

### Store bounds

- maximum records overall: `max_entries` (default 20000); the store is global
  since W15, so there is no per-alias bound;
- TTL: `ttl_seconds` (default 3600);
- eviction: expired records first, then least recently used.

v1 uses a process-local store with an async lock. The router and scheduler run
in the same process. A Redis-backed mirror is a later phase; it would help new
requests after an aidir restart while the provider is still warm, but it is not
required for correctness because queued tasks already carry their resolved
route.

### Optional client hint

If a client sends a stable session identifier, it is more reliable than any
content heuristic. Support an optional header or field:

- `X-Aidir-KV-Key` header, or
- `prompt_cache_key` when present in an OpenAI-compatible request.

When present, the key is hashed together with the alias and used as an
additional lookup dimension. The content fingerprint remains the fallback. This
mirrors OpenAI's `prompt_cache_key` semantics: it influences routing, it does
not guarantee a cache hit.

Do not use `envid` or an authorization token as the key by default. Different
users can legitimately share a system prefix, and sharing an executor is not a
data leak.

## Routing Algorithm

### Candidate evaluation

Reuse the existing `SmartRouter.evaluate_candidate` results. Extend the result
with a model-level load snapshot:

- `queue_depth`: number of queued tasks for this concrete provider/model (not
  merely for the same resource requirements);
- `active_count` if available;
- `max_concurrent_requests`;
- `model_loaded`: the base weights are present and reusable on that executor;
- `admission`: `ready`, `concurrency`, `session_memory`, `base_memory`,
  `waiting_unload`, `resource_disabled`, or `provider_down`;
- `admissible_after_unload`: the model could start if idle models were evicted;
- `schedulable`: the candidate is reachable and can eventually run the model;
- `can_run_now`: it can start without waiting.

`admission` is the important field for the resource-wait policy. It separates
three conditions that all look like `can_run_now == false` today:

1. **Queue / concurrency**: the model is ready, but its concurrent request limit
   is reached. This is handled by the queue-imbalance rule, not by a resource
   timeout.
2. **Warm-model resource wait**: the model is still loaded, but there is not
   enough room for another session (or another idle model must be evicted
   first). The cache is still warm, so it is worth waiting.
3. **Cold-model resource wait**: the model is not loaded and there is not enough
   memory to load it. Its cache is already gone, so waiting has no cache
   benefit; this is the case where an immediate diversion is usually better.

The current queue state on the local side is resource-based and can count
unrelated models that happen to require the same VRAM. Add a model-aware
`get_model_queue_state(provider, model, priority)` helper, or extend the
existing queue-state calculation with a route/model filter. Remote aidir already
serves model-only queue state, which is the right granularity.

The model-aware admission helper must not count the **requested model's own
base footprint** as a blocker. If the model is loaded, the correct question is
"is there room for one more session?", not "is all of the model's VRAM free?".
This is the distinction that prevents a long-running warm model from looking
like a permanently unavailable resource.

### Affinity-first selection

When an affinity record matches:

1. If the preferred candidate no longer exists, or its `cache_group` changed, or
   its probe failed, treat it as a miss and forget the record.
2. If the preferred candidate is `ready`, select it unless the queue-imbalance
   test says otherwise.
3. If the preferred candidate is in `concurrency`, apply the queue-imbalance
   test. If the queue is acceptable, keep it and let the request wait.
4. If the preferred candidate is resource-blocked (`session_memory`,
   `base_memory`, or `waiting_unload`), apply the resource-wait policy described
   below. The request may wait for a warm model; a cold model is usually
   abandoned immediately.
5. If the preferred candidate is down (`provider_down`) or disabled, treat it as
   a miss and continue with the normal policy.
6. If the preferred candidate is the only candidate, select it under the normal
   busy-fallback rules; affinity has nothing to compare against.

### Queue imbalance test

Let:

- `Qpref` be the preferred candidate's `queue_depth`;
- `Qmin` be the minimum `queue_depth` among the other **healthy and
  schedulable** candidates for the same alias.

Divert away from the preferred candidate only when **both** conditions hold:

```text
Qpref - Qmin > max_queue_diff_len
Qpref >= Qmin * max_queue_diff_part
```

The first condition is an absolute head start in tasks. The second is a relative
factor. Requiring both avoids two failure modes:

- a relative-only rule diverts when both queues are tiny (`1` vs `0`);
- an absolute-only rule diverts on a huge but proportionally similar queue.

Special cases:

- `Qmin = 0`: the ratio test is trivially true, so the absolute threshold alone
  decides.
- `max_queue_diff_part <= 1` should be rejected by validation; the multiplier is
  only meaningful above 1.
- If there are no healthy alternatives, never divert.
- The resource-wait condition is separate: a candidate with `admission !=
  concurrency` is not compared by queue depth. See the next section.

`queue_depth` for v1 is the queued task count, as requested. `active_count` is
recorded for diagnostics and for the future `active_count + queue_depth` load
estimate, but it does not change the queue-imbalance decision in v1.

### Resource wait and model readiness

A resource that is unavailable for a short time is not the same as a failed
executor. The preferred model may still hold a warm KV cache, and waiting for a
few seconds is often much cheaper than a cold prefill elsewhere. The policy
therefore needs to know **how long** the preferred candidate has been unable to
admit a new request, and **whether its cache can still exist**.

#### Model-aware admission

Do not base the decision on raw free VRAM alone. For each candidate compute an
admission result:

```text
active_count < max_concurrent_requests ?
    yes -> model_loaded ?
        yes -> session_resources fit ?
            yes -> ready
            no  -> session_memory
        no  -> base + session fit ?
            yes -> ready
            no  -> can unload idle models and then fit ?
                yes -> waiting_unload
                no  -> base_memory
    no  -> concurrency
```

`model_loaded` is true when the model has an active base reservation, a warm
soft consumer, or an active request on that executor. The requested model's own
base footprint is **not** counted as a blocker: a loaded model only needs room
for the incremental session. This is the rule that prevents the false
"resource was low all along" conclusion when the resource is low precisely
because the requested model is already resident.

`concurrency` is not a resource condition. It means the model is ready but has
reached `maxConcurrentRequests`; the queue-imbalance rule decides whether to
wait there. Only `session_memory`, `base_memory`, and `waiting_unload` feed the
resource-wait timer.

#### Availability history

Keep a small per-`(provider, model)` readiness tracker. Do not store raw memory
samples every second; store **transitions** with timestamps:

```json
{
  "ts": 1760000000.0,
  "admission": "session_memory",
  "model_loaded": true,
  "free": {"local_machine": {"VRAM": 600}},
  "needed": {"local_machine": {"VRAM": 1200}}
}
```

The tracker derives:

- `blocked_since`: the first transition into a contiguous resource-blocked run.
  A transition to `ready` resets it. A change between `base_memory`,
  `session_memory`, and `waiting_unload` does **not** reset it, so the candidate
  cannot evade the timeout by alternating between reasons.
- `blocked_ratio`: the fraction of the last
  `resource_history_window_seconds` spent in a resource-blocked state.
- `last_sample_at`: used to discard stale history after a long idle period.

Sampling happens on every candidate evaluation. For a queued request the
scheduler re-evaluates about once per second, so a waiting task produces enough
history. No background prober is required in v1. If the gap since the last
sample is longer than the history window, discard the old run and start a new
timer at the current time; an idle instance must not make a fresh request
believe the resource has been blocked forever.

Transitions are bounded by `resource_history_window_seconds`; after pruning,
keep the last transition before the window so the ratio can be computed.

#### Wait decision

Let:

- `blocked_for = now - blocked_since`;
- `wait_limit = resource_wait_timeout_seconds` when `model_loaded == true`, else
  `cold_model_wait_timeout_seconds`;
- `ratio = blocked_ratio(resource_history_window_seconds)`.

Divert only when the preferred candidate is resource-blocked and at least one of
these holds:

```text
blocked_for >= wait_limit
resource_blocked_ratio > 0
    and observed_span >= resource_wait_timeout_seconds
    and ratio >= resource_blocked_ratio
```

The second condition is an optional anti-flapping rule. Without it, a resource
that alternates between blocked for 5 seconds and ready for 1 second could reset
the timer forever and never trigger a diversion. The ratio rule only applies
after enough history has been observed; it is disabled by setting
`resource_blocked_ratio` to `0`.

Recommended defaults:

- `resource_wait_timeout_seconds: 60` for a warm model. The cache is still
  present, so waiting is valuable;
- `cold_model_wait_timeout_seconds: 0` for a model that is not loaded. Its cache
  is already gone, so an immediate diversion is usually better. Set it to the
  same value as the warm timeout if the deployment prefers machine stability
  over cache reuse;
- `resource_history_window_seconds: 600` (the requested "last 10 minutes");
- `resource_blocked_ratio: 0.5`.

If the wait limit is reached and no alternative can run now, keep the request on
the preferred candidate and re-evaluate on the next scheduler cycle. Record
`selection_reason: affinity_wait_no_alternative`. Do not divert to another
blocked candidate just to change machines.

When the preferred candidate is warm and the blocker is `waiting_unload`, the
scheduler's existing force-unload path can free the space. The request should
stay on the preferred candidate until the warm wait limit expires.

#### Remote candidates

The remote model-only queue-state should expose `model_loaded`, `admission`,
`active_count`, `max_concurrent_requests`, and `resource_ready`. The edge can
then run the same tracker against the remote responses.

If a remote peer does not yet expose `model_loaded`, treat the state as
**unknown and possibly warm** and use `resource_wait_timeout_seconds` rather
than the cold timeout. The conservative choice protects a remote cache that may
still exist. Once the protocol is extended, the edge gets the same precision as
for a local provider.

### Selection of the alternative

When diverting:

1. Prefer candidates that can run now.
2. Among those, choose the smallest `queue_depth`.
3. Break ties by `fallback_prio`, then by configured order.
4. If none can run now, choose the smallest queue depth and queue there.

Record the selected candidate as the new preferred one for this prefix. This is
the resolved decision for v1: the current request is about to build its cache on
the new executor, so the next request should follow it. The queue thresholds and
the resource-wait timer are the hysteresis. The route metadata keeps the
previous candidate and the diversion reason for diagnostics, but it does not
retry the old candidate on the next request.

This rule applies to both queue diversion and resource-timeout diversion. A
request that was diverted while the old model was cold has no cache to return
to, and a request diverted because of a long queue has already started building
a new cache. Moving the mapping immediately is the consistent behavior.

### No affinity record

Use the existing `first_available` policy unchanged:

1. First candidate that can run now;
2. otherwise the busy candidate with the smallest `fallback_prio`;
3. otherwise fail before queueing.

After selecting, write the affinity record. This means the very first request of
a conversation seeds the mapping for all later turns.

### Pseudocode

```text
fp = fingerprint(payload, alias)
record = store.lookup(alias, fp)
candidates = evaluate_all(items)
tracker.record_each(candidates)

if record and record.candidate in candidates and record.group == candidates[record].group:
    preferred = candidates[record.candidate]

    if preferred.admission == "ready":
        if not overloaded(preferred, candidates):
            return select(preferred, reason="affinity")
        alternative = best_alternative(candidates, exclude=preferred)
        if alternative:
            store.remember(alias, fp, alternative)
            return select(alternative, reason="affinity_queue_diverted")

    elif preferred.admission == "concurrency":
        if not overloaded(preferred, candidates):
            return select(preferred, reason="affinity_wait_queue")
        alternative = best_alternative(candidates, exclude=preferred)
        if alternative:
            store.remember(alias, fp, alternative)
            return select(alternative, reason="affinity_queue_diverted")

    elif preferred.admission in {"session_memory", "base_memory", "waiting_unload"}:
        if tracker.should_divert(preferred):
            alternative = best_admissible_alternative(candidates, exclude=preferred)
            if alternative:
                store.remember(alias, fp, alternative)
                return select(alternative, reason="affinity_resource_timeout")
            return select(preferred, reason="affinity_wait_no_alternative")
        return select(preferred, reason="affinity_wait_resource")

    else:  # provider_down / disabled
        store.forget(alias, fp)
        # fall through to normal policy

selected = first_available(candidates)
store.remember(alias, fp, selected)
return select(selected, reason=selected.can_run_now ? "immediate" : "busy_fallback")
```

## Invalidation and Failure Semantics

Affinity is only useful while the cache is likely to exist. Forget it when the
executor or its cache is known to be gone.

### Forget everything for a candidate

Remove all records pointing to `(provider, model)` when:

1. a probe fails (connection error, timeout, unknown model);
2. execution fails with a transport or availability error, for example
   `UPSTREAM_UNREACHABLE`, `UPSTREAM_ERROR`, a provider 5xx, or a llama.cpp
   server start failure;
3. the model is force-unloaded by `Resources.force_unload_for`,
   `force_unload_all`, or `force_release`;
4. a managed llama.cpp server is stopped or restarted;
5. the provider is removed or its model list changes and the model disappears.

A transient request-level error (for example a 400 due to an oversized context)
must **not** invalidate the cache. Only failures that mean "this executor no
longer has the state" qualify.

### Forget one prefix

Remove a single record when:

1. it exceeds its TTL;
2. it is evicted by the bounded store;
3. the candidate's `cache_group` no longer matches;
4. the user explicitly asks to reset routing (a future administrative action).

### Success

On a successful completion, refresh `last_used` and `expires_at`. Do not extend
the TTL beyond the configured maximum.

### Cache lifetime versus record lifetime

The record is a routing hint, not proof that the provider still holds the KV
cache. Keeping a record after the provider has silently unloaded the model only
means that later requests still go to the same executor and rebuild the cache
there; it does not cause incorrect output. Therefore the TTL can be generous.
Known unload events are handled by the explicit invalidation above.

## Scheduler Integration

The scheduler is not a passive consumer of the endpoint's decision; it currently
re-resolves every queued smart route before dispatch and can move it to a
different candidate. That must not silently undo affinity.

### Before dispatch

`_refresh_smart_route_for_dispatch` must:

1. read the affinity metadata stored in `task.config.route`;
2. if the affined candidate is still healthy and not over the imbalance
   thresholds, keep the existing route, even if another candidate is now
   immediately available;
3. if the affined candidate failed or disappeared, forget it and resolve
   normally;
4. if the route changes, update the affinity record to the new candidate.

The simplest implementation is to pass the same `KvAffinityStore` into the
scheduler's `SmartRouter` and reuse the same selection function with an
`affinity_record` hint, rather than adding a second policy implementation.

### After execution failure

`_fallback_smart_route_after_execution_failure` already excludes the failed
candidate. Extend it to:

1. forget all affinity records for the failed candidate before resolving;
2. resolve with the remaining candidates;
3. remember the new candidate for the failed task's prefix, if the task carries
   fingerprint metadata.

### Queue timeout

A queue timeout does not mean the executor lost its cache. Keep affinity.
If the task is retried after a timeout, it should return to the same preferred
candidate when it is healthy.

## Remote aidir and Distributed Routing

When a smart candidate is another aidir instance (`api: "openaix"`), the edge
instance chooses the peer. The peer may itself have a smart alias and may route
again, which would break the edge's affinity.

v1 treatment:

1. Treat the remote aidir as one concrete executor in the edge's affinity store.
2. Forward an internal affinity hint with the request:
   - `X-Aidir-KV-Key`: the stable client/session key when present;
   - `X-Aidir-KV-Fingerprint`: the anchor length and hash, if the peer supports
     it;
   - the existing route trace headers for loop protection.
3. The peer, if it supports the header, treats the fingerprint as a hint and
   prefers the matching local candidate. If it does not support the header, it
   routes normally. The edge's affinity is then only an executor-level hint, not
   a guarantee.
4. Never trust the header for authorization or for bypassing loop checks.

Full distributed affinity (sharing the store between instances) is a non-goal.
The route trace already prevents loops; the affinity header is advisory.

For non-aidir remote providers (`openai`, `opencode-go`), queue-state may not be
available. Keep affinity sticky when the candidate is reachable, and skip the
queue-imbalance test. Hosted providers often have their own prefix routing
(OpenAI documents this); sending a stable `prompt_cache_key` when the protocol
supports it is the best available signal.

## Configuration

### KV affinity

Add a `routing.kv_affinity` block. The resolved decision is that affinity is
enabled by default (`enabled: true`) in both the code and the shipped
configuration. This is a deliberate behavior change for existing smart routes:
prefix affinity is the point of the feature, and an operator who needs the old
behavior can set `enabled: false` or opt out per alias later.

```json5
routing: {
  kv_affinity: {
    enabled: true,

    // fingerprint
    min_anchor_bytes: 512,
    max_anchor_bytes: 32768,
    anchor_ladder: [512, 1024, 2048, 4096, 8192, 16384, 32768],
    short_context_bytes: 10240,
    medium_context_bytes: 20480,
    long_context_bytes: 102400,
    short_fraction: 0.90,
    medium_fraction: 0.50,
    long_fraction: 0.10,

    // store
    ttl_seconds: 3600,
    max_entries: 20000,

    // routing policy
    prefer_affinity_over_immediate: true,
    max_queue_diff_len: 8,
    max_queue_diff_part: 3.0,

    // resource-wait policy
    resource_wait_timeout_seconds: 60,
    cold_model_wait_timeout_seconds: 0,
    resource_history_window_seconds: 600,
    resource_blocked_ratio: 0.5,

  },
}
```

Field notes:

- `max_queue_diff_len` is the absolute task threshold requested as
  `max_queue_dif_len`.
- `max_queue_diff_part` is the multiplier requested as `max_queue_dif_par`.
  Values `<= 1` are invalid for the ratio test; validate them at load time.
- `prefer_affinity_over_immediate: false` restores the old behavior where the
  router only uses an affinity candidate if it can run immediately. The default
  `true` is what allows "wait on the warm executor".
- `resource_wait_timeout_seconds` is how long a request may wait for a **warm**
  preferred model before affinity is abandoned. `60` is a starting point; tune
  it against the measured cost of a cold prefill.
- `cold_model_wait_timeout_seconds` is how long a request may wait for a model
  that is **not loaded**. The default `0` means immediate diversion because the
  cache is already gone. Set it equal to the warm timeout if machine stability
  matters more than cache reuse.
- `resource_history_window_seconds` is the availability history window. The
  default `600` matches the requested ten-minute window.
- `resource_blocked_ratio` is the optional anti-flapping threshold over that
  window. `0` disables it; `0.5` means "divert if the preferred candidate spent
  at least half of the observed window resource-blocked and is blocked now".
- `honor_prompt_cache_key` was removed by W15; the client hint no longer affects
  the identity.

A per-alias opt-out is obsolete after W15: there is no per-alias partition.

### Session resources and parallel requests

Add two fields to a concrete model entry:

```json5
{
  id: "qwen3.8-27b-local",
  contextWindow: 150000,

  // One-time footprint of the loaded model (weights, base buffers).
  resources: {
    local_machine: { VRAM: 19000 },
  },

  // Maximum concurrent in-flight requests this model/runtime may serve.
  maxConcurrentRequests: 4,

  // Incremental resource cost of one additional in-flight request
  // (KV cache, activations, runtime overhead).
  sessionResources: {
    local_machine: { VRAM: 1200 },
  },
}
```

Naming: `sessionResources` is recommended over `sessionMemoryMB` because it
reuses the existing resource metric/unit model, supports several resources
(VRAM, RAM, ...), and avoids a second unit convention. The display name in the
settings editor can be "session size".

Semantics:

- `maxConcurrentRequests` is the provider/runtime slot limit. Default `1`
  preserves today's behavior. For Ollama this should mirror
  `OLLAMA_NUM_PARALLEL`; for llama.cpp it should not exceed `-np`.
- `sessionResources` is the **incremental** cost of one request after the model
  is loaded. It is not the model weight cost. Use the worst-case context length
  of the model, because the KV cache grows with the number of tokens; a fixed
  value is conservative and easy to reason about.
- `resources` remains the one-time model footprint. When enabling concurrency,
  set `resources` to the weights and base buffers only. The old habit of putting
  the whole per-request VRAM estimate there would reserve the weights N times and
  defeat the purpose of `sessionResources`.

Validation:

1. `maxConcurrentRequests` is an integer in `1..1024`.
2. If `maxConcurrentRequests > 1` and the model has local resource
   requirements, `sessionResources` is required for every resource key in
   `resources`. A missing value is a configuration error, not a silent zero.
3. `sessionResources` values are non-negative numbers in the same units as the
   resource limits.
4. A model with `maxConcurrentRequests <= 1` is not affected by the session
   logic at all.

### Admission algorithm

For every concrete model, keep an active count per `(provider, model)`. The
count is needed even when `maxConcurrentRequests == 1`, because "the model is
loaded and busy" is a `concurrency` condition, not a memory shortage, and the
routing policy must not apply the resource-wait timer to it.

For `maxConcurrentRequests > 1`:

1. If `active_count >= maxConcurrentRequests`, defer the task with admission
   reason `concurrency`.
2. Determine the model's base footprint:
   - if the model is warm (soft consumer or base reservation exists), the base
     is already accounted for;
   - otherwise the base must be reserved by this request.
3. Required new resources = `base (if not present) + sessionResources`.
4. Check availability atomically under the resource admission lock.
5. Reserve the base once with a stable consumer id such as
   `base:<provider>:<model>`, and reserve the session with the task consumer id.
6. Increment the active count.
7. On task end, release the session and decrement the active count. Keep the
   base as a soft consumer for `alive_time`, or as a persistent consumer for a
   managed llama.cpp server.
8. On force-unload or server stop, release the base and clear the soft
   consumer.

Effective memory cap:

```text
memory_slots = floor(available_after_base / session_size)
effective_cap = min(maxConcurrentRequests, memory_slots)
```

Example with `weights=19000`, `session=1200`, `limit=22000`, `max=4`:

```text
first request:  reserve 19000 + 1200 = 20200, free 1800
second request: reserve 1200 -> 21400, free 600
third request:  600 < 1200 -> defer
effective_cap = min(4, 2) = 2
```

If the model is already warm, the base is not reserved again; the available
memory for sessions is the free capacity after all resident model bases.

For `maxConcurrentRequests <= 1`, keep the existing behavior: one task reserves
the full `resources` footprint and no session accounting is applied. This is the
compatibility path. The active count is still maintained so that a busy loaded
model is reported as `concurrency` rather than as a memory shortage.

The admission path returns the `admission` reason used by the routing policy:
`concurrency` when the active count is the blocker, `session_memory` when a warm
model cannot fit one more session, `base_memory` when a cold model cannot be
loaded, and `waiting_unload` when force-unloading idle models would free enough
space. The readiness tracker records the same reason, so the resource-wait timer
never mistakes a concurrency cap for a memory shortage.

### Queue state

Extend the queue-state payload with:

- `active_count`;
- `max_concurrent_requests`;
- `session_resources`;
- `model_loaded`;
- `admission` (`ready`, `concurrency`, `session_memory`, `base_memory`,
  `waiting_unload`, `resource_disabled`);
- `resource_ready` and `resource_ready_after_unload`;
- `memory_slots` (when computable);
- an updated `can_run_now` that includes the concurrency and memory checks.

The model-only queue-state endpoint used by peer aidir must include the same
fields. Smart routing needs them to distinguish "busy with a short queue" from
"at the concurrency cap" and from "temporarily short of memory while warm".

## Observability

Log one structured line per smart routing decision with:

- requested alias;
- anchor length and truncated hash (for example the first 8 hex characters);
- affinity hit or miss;
- preferred candidate and its queue depth;
- selected candidate and selection reason:
  `affinity`, `affinity_wait_queue`, `affinity_wait_resource`,
  `affinity_wait_no_alternative`, `affinity_queue_diverted`,
  `affinity_resource_timeout`, `immediate`, `busy_fallback`, `routing_failed`;
- queue depths of the compared alternatives;
- admission state, `model_loaded`, `blocked_for`, `blocked_ratio`, and the
  configured wait limit when the resource-wait policy is involved;
- probe latencies for remote candidates.

Store the same metadata in `task.config.route.kv_affinity` and
`task.config.route.candidate_probes`. Never log prompt text; only lengths,
hashes, provider/model ids, and counts.

Useful counters for the WebUI (optional, later phase):

- affinity hits, misses, diversions, invalidations;
- average queue depth of preferred and selected candidates at diversion time;
- cache-miss rate by selected candidate if the provider exposes it.

## Compatibility and Migration

1. Non-smart routing is unchanged.
2. Smart routing with `routing.kv_affinity.enabled: false` behaves exactly as
   today. Affinity is enabled by default, so an upgrade changes smart-route
   stickiness intentionally; the opt-out is one key.
3. `maxConcurrentRequests` defaults to `1`; existing models keep the current
   full-footprint reservation behavior.
4. A warm-model resource wait is bounded by `resource_wait_timeout_seconds`; a
   cold model uses `cold_model_wait_timeout_seconds`, default `0`. Neither
   setting changes the admission decision itself; they only decide when
   affinity gives up.
5. A task always carries its resolved route. A lost affinity store does not move
   an already queued task; it only affects new requests.
6. The existing 20-message trim in the OpenAIx worker remains a limitation for
   long conversations. The proposal recommends a separate follow-up to make it
   configurable; the KV keeper alone cannot restore a prefix that the worker
   removes.
7. The existing `task.config.route` schema gains optional fields; consumers that
   ignore them continue to work.

## Test Plan

### Unit: fingerprint

1. Object key order and JSON whitespace do not change the hash.
2. Reordering `messages` changes the hash; reordering `tools` changes the hash.
3. Changing sampling parameters does not change the hash.
4. Changing a tool definition, a system message, or a tool result changes the
   hash.
5. Anchor length follows the short/medium/long fractions and the byte bounds.
6. Ladder quantization chooses the expected length.
7. A longer new context matches an older, shorter stored anchor.
8. A shorter new context does not match a longer stored anchor.
9. HMAC output is stable for the same secret and changes when the secret
   changes.

### Unit: store

1. TTL expiry removes a record.
2. Per-alias and global limits evict the least recently used record.
3. `forget_candidate` removes every record for one provider/model.
4. Concurrent lookup/remember calls do not corrupt the store.
5. Records for different `cache_group` values do not match.

### Unit: routing policy

1. A first request selects `first_available` and remembers it.
2. A second request with the same prefix selects the remembered candidate even
   when another is immediately available.
3. A preferred candidate with a short queue is kept, so the request waits.
4. Diversion requires both the absolute and the relative queue conditions.
5. `Qmin = 0` uses the absolute condition only.
6. A failed probe drops affinity and selects a healthy alternative.
7. A warm resource-blocked preferred candidate is kept until
   `resource_wait_timeout_seconds`, then diverted to a candidate that can run.
8. A cold resource-blocked preferred candidate with
   `cold_model_wait_timeout_seconds: 0` is diverted immediately.
9. A cold candidate with a non-zero cold timeout is kept for that duration.
10. A resource that becomes ready before the timeout resets the timer and keeps
    affinity.
11. The requested model's own loaded base does not count as a blocker: a loaded
    model with enough room for one session is `ready`, even when raw free VRAM
    is small.
12. A model that is loaded but has no room for another session is
    `session_memory`, not `base_memory`.
13. `concurrency` does not feed the resource-wait timer; it is handled by the
    queue rule.
14. The anti-flapping ratio diverts when the candidate is repeatedly blocked,
    and is disabled by `resource_blocked_ratio: 0`.
15. A blocked run is not reset by switching between `base_memory`,
    `session_memory`, and `waiting_unload`.
16. If the timeout is reached and no alternative is admissible, the preferred
    candidate is kept with `affinity_wait_no_alternative`.
17. A single candidate never diverts.
18. Ties in queue depth use `fallback_prio` and configuration order.

### Integration: smart routing

1. Two fake candidates: first request goes to A, the next ten identical-prefix
   requests go to A.
2. Make A's queue longer than the thresholds; the next request goes to B and
   the mapping moves to B.
3. Make A fail; the next request goes to B and all A records are gone.
4. A queued task with an affined route is not moved by
   `_refresh_smart_route_for_dispatch` while A is healthy.
5. An execution failure invalidates A and the fallback task is routed to B.
6. A remote aidir candidate receives the affinity headers; a peer that supports
   them keeps the request on the hinted local candidate.
7. `prompt_cache_key`/`X-Aidir-KV-Key` overrides the content fingerprint.
8. A preferred warm model blocked for less than the wait timeout stays selected
   across several scheduler passes; after the timeout it moves to an admissible
   alternative and the mapping moves with it.
9. A preferred model that was blocked at the last observation and is still
   blocked after a long idle period is diverted without waiting a fresh timeout,
   while a stale history older than the window is discarded.
10. A remote peer that reports only `can_run_now` is treated as possibly warm and
    uses the warm wait timeout.

### Integration: session resources

1. `maxConcurrentRequests: 1` reproduces the old single-request behavior and
   reports `concurrency` (not a memory shortage) while its one request is active.
2. With enough memory, two requests run concurrently on the same model.
3. When memory is insufficient for a second session, the task is deferred and
   later admitted after the first finishes.
4. The base footprint is reserved once, not once per request.
5. A warm model reuses the base and reserves only the session.
6. Force-unload releases the base and the next request reloads the model.
7. Queue-state reports `active_count`, `max_concurrent_requests`, and
   `can_run_now` consistently.
8. A model with `maxConcurrentRequests > 1` but no `sessionResources` fails
   configuration validation when it has local resources.

### Regression

1. Existing smart-router tests pass unchanged with affinity disabled.
2. Existing queue-state tests pass unchanged for non-smart models.
3. Existing resource-reservation and warm-reuse tests pass for
   `maxConcurrentRequests <= 1`.
4. Existing scheduler fallback behavior is not changed for non-smart routes.

## Implementation Plan

### Phase 1: fingerprint and store

1. Add `core/kv_affinity.py` with canonicalization, anchor selection, HMAC
   hashing, and a bounded in-process store.
2. Add config parsing and validation for `routing.kv_affinity`.
3. Unit-test canonicalization, length policy, lookup, TTL, and eviction.

### Phase 2: smart routing

1. Pass the store into `SmartRouter`.
2. Add affinity lookup before candidate selection.
3. Implement the affinity-first and diversion rules.
4. Remember the selected candidate and expose the metadata in
   `task.config.route`.
5. Add model-level queue depth to the candidate evaluation.
6. Add the model readiness tracker and the resource-wait policy.
7. Unit-test warm/cold waits, reset behavior, anti-flapping, and stale history.

### Phase 3: scheduler and invalidation

1. Make `_refresh_smart_route_for_dispatch` honor affinity.
2. Invalidate on execution failure, force-unload, and server stop.
3. Update the execution fallback to remember the new candidate.
4. Integration tests for refresh, fallback, and invalidation.
5. Integration tests for a warm blocked model that recovers before the timeout
   and for one that does not.

### Phase 4: session resources

1. Add `maxConcurrentRequests` and `sessionResources` to the model descriptor.
2. Add base/session reservation accounting to `Resources`/`Resource`.
3. Add the active-count registry and admission logic to the scheduler.
4. Extend queue-state with active count, caps, and memory slots.
5. Tests for concurrency, memory caps, and warm reuse.

### Phase 5: remote and observability

1. Propagate affinity headers to remote aidir candidates.
2. Add structured logs and route metadata.
3. Optionally add a Redis mirror for restart survival.
4. Documentation and test protocol updates.

## Alternatives Considered

1. **Consistent hashing of the prefix to a candidate, with no state.**
   Simple and stateless, like OpenAI's edge routing, but it cannot account for
   health, queue imbalance, or one candidate being much faster. A stateful
   mapping is a better fit for a small local candidate set.

2. **Explicit session IDs only.**
   More accurate than content fingerprinting, but requires every harness to
   send a stable key. It is supported as an optional hint, not as the only
   mechanism.

3. **Tokenizing the prompt with the model tokenizer.**
   Exact, but aidir does not know the chat template of every remote provider,
   and tokenization adds a dependency and latency. Byte prefixes are a good
   proxy for routing identity.

4. **Hashing the whole context.**
   Correctly identifies an exact request, but every conversational turn changes
   the hash, so affinity would almost never be reused. The anchor prefix is the
   point of the design.

5. **External KV cache service or KV transfer between machines.**
   Potentially more powerful, but far outside the scope of aidir and not
   supported by the current providers. Routing is the part aidir can control.

6. **Relying on provider-side prompt caching only.**
   Hosted providers can route by prefix themselves, but local Ollama and
   llama.cpp instances cannot coordinate across machines. aidir is the only
   component that sees all candidates.

7. **Tracking raw resource free-memory history.**
   Simple to collect, but it answers the wrong question: a warm model's own
   weights make free memory low for as long as the model stays loaded, so the
   history would report a permanent shortage even when the model can serve
   another session. The proposal tracks model-level admission instead, which
   subtracts the reusable base and only asks for the incremental session cost.

8. **Waiting indefinitely for the preferred resource.**
   Maximizes cache retention but can strand a request behind a model that will
   not fit for hours. The bounded warm wait plus the cold-model shortcut keeps
   the latency risk under control.

## Resolved Decisions

1. **Diversion migration:** the mapping moves to the alternative immediately,
   for both queue diversion and resource timeout. The route metadata keeps the
   previous candidate for diagnostics.
2. **Queue depth:** queued tasks only. `active_count` is recorded for
   diagnostics and future use, but does not change the v1 queue rule.
3. **Session field:** `sessionResources`, a map with the same shape and units as
   `resources`.
4. **Default state:** `routing.kv_affinity.enabled` is `true` in code and in the
   shipped configuration.
5. **Store backend:** in-process only in v1. A Redis mirror is a later option.
6. **Resource wait:** model-aware admission plus a transition history. Warm
   models wait `resource_wait_timeout_seconds` (configurable, default 60); cold
   models use `cold_model_wait_timeout_seconds` (default 0, immediate
   diversion). The requested model's own base footprint is not a blocker.
7. **Sampling:** availability transitions are recorded on candidate evaluation
   only. No background sampler in v1.
8. **Anti-flapping:** enabled by default at `0.5` over the 10-minute history
   window.
9. **Threshold scope:** global for v1. Per-alias overrides are a later addition.

## Open Questions and TBD

1. **Exact meaning of the existing concurrency limit.** The current tree has no
   explicit per-model `maxConcurrentRequests`; the closest existing behavior is
   the full-VRAM reservation. This proposal introduces the field. If the target
   deployment already has a provider-side limit under another name, it should be
   mapped to this key rather than duplicated.
2. **Remote protocol fields.** The peer must expose `model_loaded` and
   `admission` for a precise warm/cold decision. Until then the edge treats an
   unknown remote state as possibly warm and uses the warm timeout. Confirm that
   the extra fields are acceptable in the public model-only queue-state response.
3. **How to invalidate on silent provider unload.** A TTL and known unload
   events cover the common cases. A provider that unloads without an event is
   indistinguishable from a cache rebuild on the same executor; routing there is
   still correct, only the speed benefit is lost.
4. **Cross-instance affinity.** The header hint is advisory. A full shared store
   would require coordination and is deferred.
5. **The 20-message trim in the OpenAIx worker.** It should be made
   configurable or removed for long harness conversations; otherwise only the
   system/tool prefix is stable.
6. **Measured tuning.** The defaults (`60s` warm, `0s` cold, `0.5` anti-flap
   ratio) are policy starting points, not measured optima. They are configurable
   globally; tune them against real cold-prefill and queue-wait data.

## References

- OpenAI, "Prompt caching": prefix hashing, 1024-token minimum, 128-token
  increments, `prompt_cache_key`.
  <https://platform.openai.com/docs/guides/prompt-caching>
- vLLM, "Automatic Prefix Caching": hash of block tokens chained with the parent
  block hash, exact prefix matching, SHA-256 option.
  <https://docs.vllm.ai/en/latest/design/prefix_caching>
- llama.cpp server documentation: `cache_prompt`, slot selection by prompt
  similarity, `-np` parallel slots, slot save/restore.
  <https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>
- llama.cpp discussion, "KV cache reuse with llama-server": LCP-based slot
  reuse and the `slot_prompt_similarity` threshold.
  <https://github.com/ggml-org/llama.cpp/discussions/13606>
- Ollama `kvcache` package: prefix reuse and checkpoint restore behavior.
  <https://pkg.go.dev/github.com/ollama/ollama/kvcache>
- NVIDIA NIM, "KV Cache Reuse (a.k.a. prefix caching)": >90% identical prefix,
  approximately 2x TTFT improvement.
  <https://docs.nvidia.com/nim/large-language-models/latest/kv-cache-reuse.html>
- Ollama prefill KV-cache reuse experiment: measured 7.6x TTFT improvement with
  a ~2.8k-token fixed prefix.
  <https://github.com/ai-systems-notes/ollama-prefill-kv-cache-reuse>
