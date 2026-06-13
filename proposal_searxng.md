# Proposal: SearXNG integration in web_search and web_fetch

## Status: proposal

## Summary

Add SearXNG as an alternative provider in `web_search` and `web_fetch` workers.
Workers gain a `providers` list (ordered fallback chain) in place of a single `provider` field.
On each request the worker tries providers in order, skipping any that are known to be unavailable, and falls back to the next one automatically.

## Motivation

- Brave Search API has usage quotas and requires an external API key.
- SearXNG can be self-hosted, is free, and aggregates many underlying search engines.
- Agents should not fail silently when the primary search backend is unavailable or rate-limited.
- A fallback chain allows mixing Brave (primary) and SearXNG (self-hosted fallback) or running SearXNG-only.

## Design decisions

### 1. `providers` list replaces single `provider` field

Current config:
```json5
"web_search": {
  "provider": "brave",
  "apiKey": "${BRAVE_APIKEY}",
  "request_timeout": 100
}
```

New config (backward-compatible: old single-field form is still accepted and wrapped internally):
```json5
"web_search": {
  "request_timeout": 30,
  "providers": [
    {
      "id": "brave",
      "type": "brave",
      "enabled": true,
      "apiKey": "${BRAVE_APIKEY}"
    },
    {
      "id": "local_searxng",
      "type": "searxng",
      "enabled": true,
      "host": "${SEARXNG_HOST:-127.0.0.1}",
      "port": ${SEARXNG_PORT:-18080},
      "engines": [],          // empty = SearXNG default engine set
      "categories": ["general"],
      "language": "all",
      "safesearch": 0         // 0=off, 1=moderate, 2=strict
    }
  ]
}
```

Same structure is used for `web_fetch`. The `providers` list defines the priority order.

### 2. Health check strategy

Each provider maintains a simple in-process blackout state:
- A provider is **blacklisted** for `provider_cooldown_seconds` (default 60) after a failure.
- On the first request to an instance, a lightweight health probe is sent:
  - Brave: check that the API key is non-empty; no network probe (quota cost).
  - SearXNG: `GET /healthz` (returns 200 when up); on timeout/connect error → blacklisted.
- After the cooldown expires, the provider is re-tried automatically.
- Health state is kept per-worker-instance in memory; it resets on worker restart.

### 3. Fallback logic (both workers)

```
for each provider in providers (in order):
    if provider is disabled or blacklisted → skip
    result = try_call(provider, args)
    if result.ok → return result
    log warning(provider, error)
    blacklist(provider)
return error("all providers failed", last_error)
```

Failure triggers:
- Network error / timeout.
- SearXNG HTTP 5xx.
- Brave HTTP 429 (rate limit) — treated as transient failure.
- Empty result set is NOT a fallback trigger (empty is a valid answer).

### 4. SearXNG search (`web_search`)

SearXNG JSON search endpoint: `GET /search?q=...&format=json&...`

Supported parameters mapping:

| Tool argument | SearXNG param  | Notes |
|---|---|---|
| `query`         | `q`            | normalized same as Brave |
| `count`         | `n` / `limit`  | SearXNG doesn't guarantee exact count |
| `search_lang`   | `language`     | falls back to `all` if not supported |
| `categories`    | `categories`   | from provider config |
| `engines`       | `engines`      | from provider config (comma-separated) |
| `safesearch`    | `safesearch`   | `0/1/2` |

Response normalization: SearXNG returns `results[].{title, url, content, engine, score}`.
Worker maps `content` → `description`, adds empty `extra_snippets: []` so the envelope is identical to the Brave-backed response.

### 5. SearXNG fetch (`web_fetch`)

SearXNG has no direct page-content/snippet API equivalent to Brave's LLM Context API.
Strategy for SearXNG-backed `web_fetch`:

1. Build a query from the provided `url` and optional `query` argument.
2. Call SearXNG search with `categories=general`, `engines` from provider config.
3. Find the result whose URL matches or is on the same hostname as the requested URL.
4. If found, HTTP-fetch the raw page via `httpx.AsyncClient` and extract text (strip HTML tags with a simple regex or `html.parser`).
5. Truncate to `maxChars` (same as current Brave path).
6. Return the same envelope shape as the Brave path (`text`, `snippets`, `matches`, etc.) with `provider: searxng`.

If no matching URL is found in search results, fall back to direct page fetch without search context.

Note: this fetch mode returns raw extracted text, not curated AI-ready snippets. Quality may be lower than Brave LLM Context. This is acceptable as a free fallback.

### 6. Backward compatibility

- If only `provider: "brave"` + `apiKey` are present (old config form), the worker wraps them into `[{id: "brave", type: "brave", ...}]` at init time.
- Old single-provider flow is identical to a one-entry providers list with no fallback.
- No config migration is required for existing deployments.

## Config schema additions

### `web_search` and `web_fetch` workers
```json5
{
  "request_timeout": 30,       // seconds per provider call
  "provider_cooldown_seconds": 60,  // blackout duration after provider failure
  "providers": [
    {
      "id": "string",          // unique id used in logs and error codes
      "type": "brave|searxng",
      "enabled": true,

      // — brave-specific —
      "apiKey": "...",         // or via BRAVE_APIKEY env
      "baseUrl": "https://api.search.brave.com",  // optional override

      // — searxng-specific —
      "host": "127.0.0.1",
      "port": 18080,
      "engines": [],           // [] = let SearXNG decide; or ["google","bing","duckduckgo"]
      "categories": ["general"],
      "language": "all",
      "safesearch": 0          // 0=off, 1=moderate, 2=strict
    }
  ]
}
```

## Implementation plan

### Stage 1 — provider registry + fallback loop
- Refactor `initialize()` in both workers: parse `providers` list or wrap legacy single-provider config.
- Add `_ProviderState` dataclass: `id`, `type`, `config`, `blacklisted_until: float`.
- Add `_try_next_provider(args)` dispatch loop that iterates states, skips blacklisted, calls provider-specific method, handles errors.
- Brave path stays unchanged as a provider-specific method (`_call_brave(args, provider_cfg)`).

### Stage 2 — SearXNG health check + search
- Add `_check_searxng_health(provider_cfg)` → `GET /healthz`, returns bool.
- Add `_call_searxng_search(args, provider_cfg)` → query `/search?format=json`, normalize response envelope.
- Wire into fallback loop in `WebSearchWorker`.

### Stage 3 — SearXNG fetch
- Add `_call_searxng_fetch(args, provider_cfg)` in `WebFetchWorker`:
  - SearXNG search for URL context.
  - Direct httpx page fetch + HTML text extraction.
  - Normalize to same response envelope.

### Stage 4 — config, docs, tests
- Update `config.json5` and `config.json5.example` with `providers` list.
- Update `.env.example` with `SEARXNG_HOST` / `SEARXNG_PORT`.
- Add `test_web_search_fallback.py` covering:
  - Brave primary, SearXNG fallback on Brave failure.
  - SearXNG-only config.
  - All providers down → error.
  - Blackout cooldown behavior.
- Update `README.md` worker config table.

## Out of scope for v1

- SearXNG authentication (SearXNG runs unauthenticated locally).
- Persistent health state across worker restarts (in-memory only).
- Per-request engine override by the calling agent (stays in provider config).
- SearXNG image / news / video category support (only `general` in v1).
