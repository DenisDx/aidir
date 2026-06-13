"""
web_search tool worker.
Supports multiple search providers (brave, searxng) with automatic fallback chain.
"""
from __future__ import annotations

import os
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx

from core.task import Task
from core.worker import BaseToolWorker, WorkerResult


class _ProviderState:
    """Runtime availability state for one provider entry."""

    def __init__(self, cfg: dict) -> None:
        self.id = str(cfg.get("id") or cfg.get("type") or "unknown")
        self.type = str(cfg.get("type") or "").strip().lower()
        self.enabled = bool(cfg.get("enabled", True))
        self.cfg = cfg
        self._blacklisted_until: float = 0.0

    def is_available(self) -> bool:
        """True when provider is enabled and not in cooldown."""
        return self.enabled and time.monotonic() >= self._blacklisted_until

    def blacklist(self, cooldown: float) -> None:
        """Mark provider unavailable for cooldown seconds."""
        self._blacklisted_until = time.monotonic() + cooldown


class WebSearchWorker(BaseToolWorker):
    """Tool worker that queries web search providers with automatic fallback."""

    task_type = "tool"
    _MAX_QUERY_CHARS = 400
    _MAX_QUERY_WORDS = 50
    _ALLOWED_SEARCH_LANGS = {
        "ar", "eu", "bn", "bg", "ca", "zh-hans", "zh-hant", "hr", "cs", "da", "nl", "en", "en-gb",
        "et", "fi", "fr", "gl", "de", "el", "gu", "he", "hi", "hu", "is", "it", "jp", "kn", "ko",
        "lv", "lt", "ms", "ml", "mr", "nb", "pl", "pt-br", "pt-pt", "pa", "ro", "ru", "sr", "sk",
        "sl", "es", "sv", "ta", "te", "th", "tr", "uk", "ur", "vi",
    }

    def get_tool_description(self) -> list[dict]:
        """Return MCP-compatible tool description as a list."""
        return [{
            "name": "search",
            "description": "Search the web and return ranked results.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Query Length: Maximum 400 characters and 50 words.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results (1-20).",
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Result page offset (0-9).",
                        "minimum": 0,
                        "maximum": 9,
                    },
                    "country": {
                        "type": "string",
                        "description": "Two-letter country code (e.g. US, DE).",
                    },
                    "search_lang": {
                        "type": "string",
                        "description": "Language code for search results (e.g. en, de).",
                    },
                    "ui_lang": {
                        "type": "string",
                        "description": "UI locale for metadata (e.g. en-US).",
                    },
                    "safesearch": {
                        "type": "string",
                        "enum": ["off", "moderate", "strict"],
                        "description": "Adult-content filtering level.",
                    },
                    "freshness": {
                        "type": "string",
                        "description": "Date filter: pd, pw, pm, py, or YYYY-MM-DDtoYYYY-MM-DD.",
                    },
                    "spellcheck": {
                        "type": "boolean",
                        "description": "Enable Brave spellcheck normalization.",
                    },
                    "extra_snippets": {
                        "type": "boolean",
                        "description": "Include up to 5 alternative snippets per result.",
                    },
                    "result_filter": {
                        "type": "array",
                        "description": "Optional Brave result types (e.g. web, news, videos).",
                        "items": {"type": "string"},
                    },
                },
                "required": ["query"],
            },
        }]

    async def initialize(self, config: dict) -> None:
        """Load provider list from config; wraps legacy single-provider form."""
        self._timeout = int(config.get("request_timeout") or config.get("timeoutSeconds", 100) or 100)
        self._cooldown = float(config.get("provider_cooldown_seconds") or 60)

        providers_cfg = config.get("providers")
        if isinstance(providers_cfg, list) and providers_cfg:
            raw_list = providers_cfg
        else:
            # Legacy single-provider: wrap into one-entry list
            raw_list = [{
                "id": str(config.get("provider", "brave")),
                "type": str(config.get("provider", "brave")),
                "enabled": True,
                "apiKey": config.get("apiKey") or os.getenv("BRAVE_APIKEY") or "",
                "baseUrl": config.get("baseUrl") or "https://api.search.brave.com",
            }]

        self._providers: list[_ProviderState] = [_ProviderState(p) for p in raw_list]

    # ── utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _int_arg(value: Any, default: int, minimum: int, maximum: int) -> int:
        """Parse integer argument with bounds and fallback."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return min(maximum, max(minimum, parsed))

    @classmethod
    def _normalize_query(cls, value: str) -> str:
        """Collapse whitespace and trim queries to maximum supported size."""
        query = " ".join(str(value).split())
        if not query:
            return ""
        words = query.split(" ")
        if len(words) > cls._MAX_QUERY_WORDS:
            query = " ".join(words[: cls._MAX_QUERY_WORDS])
        if len(query) <= cls._MAX_QUERY_CHARS:
            return query
        trimmed = query[: cls._MAX_QUERY_CHARS].rstrip()
        split_at = trimmed.rfind(" ")
        if split_at >= cls._MAX_QUERY_CHARS * 3 // 4:
            trimmed = trimmed[:split_at]
        return trimmed.rstrip() or query[: cls._MAX_QUERY_CHARS].rstrip()

    @classmethod
    def _normalize_search_lang(cls, value: object, default: str = "en") -> str:
        """Map unsupported search_lang values to a safe default."""
        normalized = str(value or default).strip().lower()
        if normalized in cls._ALLOWED_SEARCH_LANGS:
            return normalized
        base = normalized.split("-", 1)[0]
        if base in cls._ALLOWED_SEARCH_LANGS:
            return base
        return default

    @staticmethod
    def _is_empty_search_result(data: Any) -> bool:
        """Return True when a search response has no items."""
        if not isinstance(data, dict):
            return True
        items = data.get("items")
        return not isinstance(items, list) or len(items) == 0

    # ── SearXNG health probe ──────────────────────────────────────────────────

    async def _check_searxng_health(self, pcfg: dict) -> bool:
        """Return True if SearXNG /healthz responds 200."""
        host = str(pcfg.get("host") or "127.0.0.1").strip()
        port = int(pcfg.get("port") or 18080)
        try:
            async with httpx.AsyncClient(timeout=min(self._timeout, 5)) as client:
                r = await client.get(f"http://{host}:{port}/healthz")
                return r.status_code == 200
        except Exception:
            return False

    # ── Brave search ──────────────────────────────────────────────────────────

    async def _call_brave_search(self, args: dict, pcfg: dict) -> WorkerResult:
        """Call Brave Web Search API and return normalized envelope."""
        api_key = str(pcfg.get("apiKey") or os.getenv("BRAVE_APIKEY") or "").strip()
        if not api_key:
            return WorkerResult(ok=False, error={"code": "MISSING_API_KEY", "message": "BRAVE_APIKEY/apiKey is not configured"})

        query = self._normalize_query(args.get("query", ""))
        if not query:
            return WorkerResult(ok=False, error={"code": "INVALID_ARGUMENT", "message": "query is required"})

        count = self._int_arg(args.get("count", 10), 10, 1, 20)
        offset = self._int_arg(args.get("offset", 0), 0, 0, 9)
        base_url = str(pcfg.get("baseUrl") or "https://api.search.brave.com").rstrip("/")

        params: dict = {
            "q": query,
            "count": count,
            "offset": offset,
            "country": str(args.get("country", "us") or "us"),
            "search_lang": self._normalize_search_lang(args.get("search_lang", "en")),
            "ui_lang": str(args.get("ui_lang", "en-US") or "en-US"),
            "safesearch": str(args.get("safesearch", "moderate") or "moderate"),
        }
        freshness = args.get("freshness")
        if isinstance(freshness, str) and freshness.strip():
            params["freshness"] = freshness.strip()
        if "spellcheck" in args:
            params["spellcheck"] = 1 if bool(args.get("spellcheck")) else 0
        if "extra_snippets" in args:
            params["extra_snippets"] = 1 if bool(args.get("extra_snippets")) else 0
        result_filter = args.get("result_filter")
        if isinstance(result_filter, list):
            cleaned = [str(v).strip() for v in result_filter if str(v).strip()]
            if cleaned:
                params["result_filter"] = ",".join(cleaned)

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }
        endpoint = f"{base_url}/res/v1/web/search"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(endpoint, params=params, headers=headers)
        except Exception as exc:
            return WorkerResult(ok=False, error={"code": "BRAVE_REQUEST_FAILED", "message": str(exc)})

        if response.status_code == 429:
            return WorkerResult(ok=False, error={"code": "BRAVE_RATE_LIMIT", "message": "Brave rate limit (429)"})
        if response.status_code != 200:
            return WorkerResult(ok=False, error={
                "code": "BRAVE_HTTP_ERROR",
                "message": f"Brave returned HTTP {response.status_code}",
                "details": response.text[:512],
            })

        body = response.json()
        web_results = ((body.get("web") or {}).get("results") or []) if isinstance(body, dict) else []
        items = [
            {
                "title": row.get("title", ""),
                "url": row.get("url", ""),
                "description": row.get("description", ""),
                "extra_snippets": row.get("extra_snippets") or [],
                "age": row.get("age"),
                "page_age": row.get("page_age"),
            }
            for row in web_results if isinstance(row, dict)
        ]
        query_meta = body.get("query") if isinstance(body, dict) else {}
        more_results = bool((query_meta or {}).get("more_results_available"))

        return WorkerResult(ok=True, data={
            "tool": "web_search",
            "provider": "brave",
            "request": {"url": f"{endpoint}?{urlencode(params, doseq=True)}", "query": query, "count": count, "offset": offset},
            "more_results_available": more_results,
            "items": items,
        })

    # ── SearXNG search ────────────────────────────────────────────────────────

    async def _call_searxng_search(self, args: dict, pcfg: dict) -> WorkerResult:
        """Call SearXNG JSON search endpoint and return normalized envelope."""
        query = self._normalize_query(args.get("query", ""))
        if not query:
            return WorkerResult(ok=False, error={"code": "INVALID_ARGUMENT", "message": "query is required"})

        host = str(pcfg.get("host") or "127.0.0.1").strip()
        port = int(pcfg.get("port") or 18080)
        count = self._int_arg(args.get("count", 10), 10, 1, 100)
        lang = str(pcfg.get("language") or args.get("search_lang") or "all").strip() or "all"
        safesearch = int(pcfg.get("safesearch") or 0)

        params: dict = {
            "q": query,
            "format": "json",
            "language": lang,
            "safesearch": safesearch,
            "categories": ",".join(pcfg.get("categories") or ["general"]),
        }
        engines = pcfg.get("engines")
        if isinstance(engines, list) and engines:
            params["engines"] = ",".join(str(e) for e in engines)

        endpoint = f"http://{host}:{port}/search"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(endpoint, params=params)
        except Exception as exc:
            return WorkerResult(ok=False, error={"code": "SEARXNG_REQUEST_FAILED", "message": str(exc)})

        if response.status_code != 200:
            return WorkerResult(ok=False, error={
                "code": "SEARXNG_HTTP_ERROR",
                "message": f"SearXNG returned HTTP {response.status_code}",
                "details": response.text[:256],
            })

        try:
            body = response.json()
        except Exception:
            return WorkerResult(ok=False, error={"code": "SEARXNG_INVALID_JSON", "message": "SearXNG returned non-JSON response"})

        raw_results = (body.get("results") or []) if isinstance(body, dict) else []
        items = [
            {
                "title": row.get("title", ""),
                "url": row.get("url", ""),
                "description": row.get("content", ""),
                "extra_snippets": [],
                "age": None,
                "page_age": None,
            }
            for row in raw_results[:count] if isinstance(row, dict)
        ]

        return WorkerResult(ok=True, data={
            "tool": "web_search",
            "provider": "searxng",
            "request": {"url": endpoint, "query": query, "count": count, "offset": 0},
            "more_results_available": len(raw_results) > count,
            "items": items,
        })

    # ── dispatch ──────────────────────────────────────────────────────────────

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Try each configured provider in order, falling back on failure."""
        payload = task.payload or {}
        args = payload.get("arguments") or {}

        configured = len(self._providers)
        enabled = 0
        tried = 0
        blacklisted_ids: list[str] = []
        empty_result: WorkerResult | None = None
        last_error: dict = {"code": "NO_PROVIDER", "message": "No search providers are configured or available"}

        for state in self._providers:
            if not state.enabled:
                continue
            enabled += 1

            if not state.is_available():
                blacklisted_ids.append(state.id)
                continue

            tried += 1

            if state.type == "searxng":
                if not await self._check_searxng_health(state.cfg):
                    state.blacklist(self._cooldown)
                    last_error = {"code": "SEARXNG_UNAVAILABLE", "message": f"SearXNG provider '{state.id}' is not reachable"}
                    continue
                result = await self._call_searxng_search(args, state.cfg)
            elif state.type == "brave":
                result = await self._call_brave_search(args, state.cfg)
            else:
                last_error = {"code": "UNSUPPORTED_PROVIDER", "message": f"Unknown provider type: {state.type}"}
                continue

            if result.ok:
                if state.type == "searxng" and bool(state.cfg.get("empty_is_error")) and self._is_empty_search_result(result.data):
                    empty_result = result
                    continue
                return result

            state.blacklist(self._cooldown)
            last_error = result.error or {"code": "PROVIDER_FAILED", "message": f"Provider '{state.id}' failed"}

        if configured == 0:
            return WorkerResult(ok=False, error={"code": "NO_PROVIDER", "message": "No search providers are configured"})
        if enabled == 0:
            return WorkerResult(ok=False, error={"code": "NO_PROVIDER_ENABLED", "message": "All search providers are disabled"})
        if tried == 0 and blacklisted_ids:
            return WorkerResult(
                ok=False,
                error={
                    "code": "PROVIDERS_COOLDOWN",
                    "message": f"All search providers are in cooldown: {', '.join(blacklisted_ids)}",
                    "details": f"Retry after ~{int(self._cooldown)}s or reduce provider_cooldown_seconds",
                },
            )

        if empty_result is not None:
            return empty_result

        return WorkerResult(ok=False, error=last_error)


worker = WebSearchWorker()
