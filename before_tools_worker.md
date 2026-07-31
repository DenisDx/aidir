# Proposal: `before_tools` — Pre-Tool-Call Context Processing Hook

## 1. Summary

Add a `before_tools` field to the OpenAIx request protocol.
It declares a list of processing steps that the `openaix` worker executes against the message
history **before** the first tool-call turn of the internal tool loop.

This solves the problem that context-level transformations (e.g., summarization/compression)
have no natural insertion point once the worker's internal tool loop starts.

**Scope of this proposal:** protocol extension + implementation of the first processor,
`context_compressor`.

---

## 2. Problem Statement

The `openaix` worker runs an internal tool loop (`_run_with_internal_tools`) that orchestrates
multi-turn LLM→tool→LLM cycles autonomously.  When the accumulated context grows large
(many tool results, long histories), subsequent LLM calls become slow, expensive, or exceed
the model's context window.

Because the loop is internal to the worker, the calling client has no way to inject a
compression step between turns.  The `before_tools` hook addresses this by running
user-defined actions right before the loop begins.

---

## 3. Protocol Extension

### 3.1 Placement in the OpenAIx request body

`before_tools` is an **optional top-level object** in the OpenAIx payload (alongside `model`,
`messages`, `tools`, etc.).

```jsonc
{
    "model": "...",
    "messages": [...],
    "tools": [...],
    "before_tools": {
        "<processor_type>": { /* processor-specific settings */ }
    }
}
```

Keys of `before_tools` are processor type identifiers (strings).
Values are per-processor configuration objects.
Processors are applied **in definition order**.

**TBD-1:** Should `before_tools` be a dict (unordered by JSON spec) or an ordered array
`[{"type": "context_compressor", ...}, ...]`?  An array preserves explicit ordering but is
less concise.  A dict is simpler but relies on Python's insertion-order dict behaviour.

### 3.2 `before_tools` vs. `before_each_turn`

The hook fires **once**, before turn 1 of the tool loop.  It does **not** fire before every
subsequent turn.

**TBD-2:** Should a `before_each_turn` variant be specified now or deferred to a future
proposal?  Per-turn compression may be useful for very long agentic runs.

---

## 4. Processor: `context_compressor`

### 4.1 Field specification

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `threshold_size` | int | yes | — | Character count of the serialised message history. If total length is below this value the processor is skipped. |
| `message` | string | yes | — | System-prompt text to pass to the compression model instead of the original system message. Should instruct the model how to summarise. |
| `provider` | string | no | task's provider | Upstream provider to use for the compression call. |
| `model` | string | no | task's model | Model for the compression call. |
| `priority` | int | no | same as parent task | Queue priority for the compression sub-task. |
| `keep_last` | int | no | 0 | Number of trailing messages to preserve verbatim after replacing history (0 = keep none; only the summary is kept). |
| `tools` | array | no | `[]` | Tool list injected into the compression call. Default empty (no tools for the compressor). |

**TBD-3:** `threshold_size` measures raw character count of the JSON-serialised messages list.
A token-based threshold would be more accurate but requires a tokenizer.  Character count is a
reasonable approximation; the name should reflect this (e.g. `threshold_chars`).  Decide
whether to rename or to keep `threshold_size` as an abstract "size" that the implementation
maps to characters.

### 4.2 Algorithm

```
1. Measure len(json.dumps(messages))
2. If length <= threshold_size → skip, return messages unchanged
3. Serialise messages into a single user message (or include as-is)
4. Backup original system message (messages[0] where role=="system"), if present
5. Replace system message with `message` field value (or prepend if none exists)
6. Remove tools injection (set tools=[]) unless processor's `tools` is non-empty
7. Dispatch compression sub-task synchronously (same as internal tool calls today)
8. Receive LLM response text
9. Attempt to parse response as JSON:
     - If valid JSON array of {role, content} objects → use those as replacement messages
     - Otherwise → wrap raw text as a single assistant message (role="assistant")
10. Restore original system message as messages[0]
11. If keep_last > 0 → append the last `keep_last` original messages after the summary
12. Return rebuilt message list
```

**TBD-4:** Step 9 fallback role: when the LLM returns plain text (not a structured JSON array),
wrapping it as `role="assistant"` is natural, but the compressor's instructions may result in
a summary that fits better as `role="user"` or as a second `role="system"` block.
Recommendation: use `role="assistant"` as default; consider an optional `fallback_role` field.

**TBD-5:** Step 7 — the sub-task is dispatched as a child `Task_agent` via core API (same
pattern as `_execute_internal_tool`).  The parent task must wait for completion.
Clarify whether the compression task must run in the **same** provider/queue, a different one,
or wherever the scheduler assigns it.  This matters for resource accounting.

### 4.3 JSON response format expected from the compressor LLM

The preferred output from the LLM is:
```json
[
    {"role": "user",      "content": "...condensed user side..."},
    {"role": "assistant", "content": "...condensed assistant side..."}
]
```

The system prompt for the compressor (the `message` field) should instruct the model to emit
this format.  The processor does a best-effort parse and falls back to plain text (TBD-4).

---

## 5. Implementation Plan

### Phase 1 — Protocol layer (no new files)

1. Document `before_tools` in `openaix_spec.md` under section 4 (request schema).
2. Add parsing/validation of `before_tools` in `_normalize_payload()` inside
   `workers/agent/openaix/app.py`.  Unknown processor types: warn and skip.

### Phase 2 — Hook invocation in the tool loop

3. In `_run_with_internal_tools()`, before the `for _ in range(max_turns)` loop, call
   `await self._apply_before_tools(payload, parent_task)`.
4. `_apply_before_tools` iterates over `before_tools` items in order, dispatches each
   processor, and returns the (potentially modified) messages list.

### Phase 3 — `context_compressor` processor

5. Implement `_processor_context_compressor(cfg, messages, parent_task)` in the same file.
6. Child sub-task creation follows the existing `_execute_internal_tool` pattern:
   create a `Task_agent`, `bind_child_task`, enqueue via `self._core`, await result.
7. Response parsing: try `json.loads`, validate list of dicts, fall back to plain text.

### Phase 4 — Tests

8. Add `test_before_tools_compressor.py` unit test:
   - threshold not exceeded → messages unchanged
   - threshold exceeded → compression sub-task fired, messages replaced
   - malformed JSON response → plain-text fallback used
   - `keep_last` > 0 → trailing messages preserved

---

## 6. Open Questions (TBD summary)

| ID | Question | Location |
|---|---|---|
| TBD-1 | `before_tools` as ordered dict vs. explicit array | §3.1 |
| TBD-2 | `before_each_turn` variant — specify now or defer? | §3.2 |
| TBD-3 | `threshold_size` unit: chars vs. tokens; rename to `threshold_chars`? | §4.1 |
| TBD-4 | Fallback role when LLM returns plain text (not JSON array) | §4.2 step 9 |
| TBD-5 | Compression sub-task queue placement and resource accounting | §4.2 step 7 |

---

## 7. Non-goals (out of scope for this proposal)

- Adding processor types other than `context_compressor`.
- Per-turn (`before_each_turn`) execution.
- A standalone new worker type — this is implemented entirely inside the existing
  `openaix` worker.