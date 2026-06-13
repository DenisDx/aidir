"""
web_fetch tool worker.
Supports multiple providers (brave, searxng) with automatic fallback chain.
Brave uses LLM Context API; SearXNG falls back to direct page fetch with HTML extraction.
"""
from __future__ import annotations

import os
import re
import time
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from core.worker import BaseToolWorker, WorkerResult
from core.task import Task


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags and extract readable text."""

    _SKIP_TAGS = {"script", "style", "noscript", "head"}
    _BLOCK_TAGS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "article", "section"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS and self._skip_depth == 0:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        """Return extracted text with normalized whitespace."""
        raw = "".join(self._parts)
        # Normalize multiple blank lines
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


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


class WebFetchWorker(BaseToolWorker):
    """Tool worker that fetches URL grounding content with automatic provider fallback."""

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
            "name": "fetch",
            "description": "Fetch URL-relevant grounding snippets or page text.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Target URL to fetch context for.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional focused query. If omitted, URL-driven retrieval is used.",
                    },
                    "maximum_number_of_urls": {
                        "type": "integer",
                        "description": "Brave URL budget (1-50).",
                        "minimum": 1,
                        "maximum": 50,
                    },
                    "maximum_number_of_tokens": {
                        "type": "integer",
                        "description": "Total token budget (1024-32768).",
                        "minimum": 1024,
                        "maximum": 32768,
                    },
                    "maximum_number_of_snippets": {
                        "type": "integer",
                        "description": "Total snippet budget (1-100).",
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "maximum_number_of_tokens_per_url": {
                        "type": "integer",
                        "description": "Per-URL token budget (512-8192).",
                        "minimum": 512,
                        "maximum": 8192,
                    },
                    "maximum_number_of_snippets_per_url": {
                        "type": "integer",
                        "description": "Per-URL snippet budget (1-100).",
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "context_threshold_mode": {
                        "type": "string",
                        "enum": ["strict", "balanced", "lenient", "disabled"],
                        "description": "Brave relevance threshold mode.",
                    },
                    "freshness": {
                        "type": "string",
                        "description": "Date filter: pd, pw, pm, py, or YYYY-MM-DDtoYYYY-MM-DD.",
                    },
                    "country": {
                        "type": "string",
                        "description": "Two-letter country code (e.g. US, DE).",
                    },
                    "search_lang": {
                        "type": "string",
                        "description": "Language code for results (e.g. en, de).",
                    },
                    "maxChars": {
                        "type": "integer",
                        "description": "Truncate combined text to this length.",
                        "minimum": 256,
                        "maximum": 100000,
                    },
                },
                "required": ["url"],
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
    def _normalize_search_lang(cls, value: object, default: str = "en") -> str:
        """Map unsupported search_lang values to a safe default."""
        normalized = str(value or default).strip().lower()
        if normalized in cls._ALLOWED_SEARCH_LANGS:
            return normalized
        base = normalized.split("-", 1)[0]
        if base in cls._ALLOWED_SEARCH_LANGS:
            return base
        return default

    @classmethod
    def _normalize_query(cls, value: str, *, suffix: str = "") -> str:
        """Collapse whitespace and trim queries to maximum supported size while preserving suffixes."""
        query = " ".join(str(value).split())
        if not query:
            query = suffix.strip()

        words = query.split(" ") if query else []
        if len(words) > cls._MAX_QUERY_WORDS:
            query = " ".join(words[: cls._MAX_QUERY_WORDS])

        suffix = suffix.strip()
        suffix_part = f" {suffix}" if suffix else ""
        max_query_chars = cls._MAX_QUERY_CHARS - len(suffix_part)
        if max_query_chars < 1:
            max_query_chars = cls._MAX_QUERY_CHARS
            suffix_part = ""

        if len(query) > max_query_chars:
            trimmed = query[:max_query_chars].rstrip()
            split_at = trimmed.rfind(" ")
            if split_at >= max_query_chars * 3 // 4:
                trimmed = trimmed[:split_at]
            query = trimmed.rstrip() or query[:max_query_chars].rstrip()

        normalized = f"{query}{suffix_part}".strip()
        if len(normalized) <= cls._MAX_QUERY_CHARS:
            return normalized
        return normalized[: cls._MAX_QUERY_CHARS].rstrip()

    @staticmethod
    def _is_empty_fetch_result(data: Any) -> bool:
        """Return True when fetch response contains no useful content."""
        if not isinstance(data, dict):
            return True
        text = str(data.get("text") or "").strip()
        snippets = data.get("snippets") or []
        matches = data.get("matches") or []
        return not text and not snippets and not matches

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

    # ── Brave fetch ───────────────────────────────────────────────────────────

    async def _call_brave_fetch(self, url: str, args: dict, pcfg: dict) -> WorkerResult:
        """Fetch URL grounding snippets via Brave LLM Context endpoint."""
        api_key = str(pcfg.get("apiKey") or os.getenv("BRAVE_APIKEY") or "").strip()
        if not api_key:
            return WorkerResult(ok=False, error={"code": "MISSING_API_KEY", "message": "BRAVE_APIKEY/apiKey is not configured"})

        parsed = urlparse(url)
        max_chars = self._int_arg(args.get("maxChars", 8000), 8000, 256, 100000)
        query = str(args.get("query", "")).strip()
        q = self._normalize_query(query if query else url, suffix=f"site:{parsed.netloc}")
        base_url = str(pcfg.get("baseUrl") or "https://api.search.brave.com").rstrip("/")

        request_body: dict = {
            "q": q,
            "country": str(args.get("country", "us") or "us"),
            "search_lang": self._normalize_search_lang(args.get("search_lang", "en")),
            "maximum_number_of_urls": self._int_arg(args.get("maximum_number_of_urls", 10), 10, 1, 50),
            "maximum_number_of_tokens": self._int_arg(args.get("maximum_number_of_tokens", 8192), 8192, 1024, 32768),
            "maximum_number_of_snippets": self._int_arg(args.get("maximum_number_of_snippets", 30), 30, 1, 100),
            "maximum_number_of_tokens_per_url": self._int_arg(args.get("maximum_number_of_tokens_per_url", 2048), 2048, 512, 8192),
            "maximum_number_of_snippets_per_url": self._int_arg(args.get("maximum_number_of_snippets_per_url", 20), 20, 1, 100),
            "context_threshold_mode": str(args.get("context_threshold_mode", "balanced") or "balanced"),
        }
        freshness = args.get("freshness")
        if isinstance(freshness, str) and freshness.strip():
            request_body["freshness"] = freshness.strip()

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
            "X-Subscription-Token": api_key,
        }
        endpoint = f"{base_url}/res/v1/llm/context"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(endpoint, json=request_body, headers=headers)
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
        grounding = body.get("grounding") if isinstance(body, dict) else {}
        generic = grounding.get("generic") if isinstance(grounding, dict) else []
        sources = body.get("sources") if isinstance(body, dict) else {}

        target_host = parsed.netloc
        matches: list[dict] = []
        snippets: list[str] = []
        for row in generic or []:
            if not isinstance(row, dict):
                continue
            row_url = str(row.get("url", ""))
            row_host = urlparse(row_url).netloc if row_url else ""
            row_snippets = [s.strip() for s in (row.get("snippets") or []) if isinstance(s, str) and s.strip()]
            if row_host == target_host or not matches:
                matches.append({"url": row_url, "title": row.get("title", ""), "hostname": row_host, "snippets": row_snippets})
            if row_host == target_host:
                snippets.extend(row_snippets)

        if not snippets:
            for row in matches:
                snippets.extend(row.get("snippets") or [])

        combined_text = "\n\n".join(snippets)
        text = combined_text[:max_chars]

        return WorkerResult(ok=True, data={
            "tool": "web_fetch",
            "provider": "brave",
            "url": url,
            "query": q,
            "matches": matches,
            "snippets": snippets,
            "text": text,
            "truncated": len(combined_text) > len(text),
            "sources": sources,
        })

    # ── SearXNG fetch ─────────────────────────────────────────────────────────

    async def _call_searxng_fetch(self, url: str, args: dict, pcfg: dict) -> WorkerResult:
        """Fetch URL via SearXNG search context + direct page fetch with HTML extraction."""
        parsed = urlparse(url)
        max_chars = self._int_arg(args.get("maxChars", 8000), 8000, 256, 100000)
        query_raw = str(args.get("query", "")).strip()
        search_q = self._normalize_query(query_raw if query_raw else url, suffix=f"site:{parsed.netloc}")

        host = str(pcfg.get("host") or "127.0.0.1").strip()
        port = int(pcfg.get("port") or 18080)
        lang = str(pcfg.get("language") or args.get("search_lang") or "all").strip() or "all"
        safesearch = int(pcfg.get("safesearch") or 0)

        search_params: dict = {
            "q": search_q,
            "format": "json",
            "language": lang,
            "safesearch": safesearch,
            "categories": ",".join(pcfg.get("categories") or ["general"]),
        }
        engines = pcfg.get("engines")
        if isinstance(engines, list) and engines:
            search_params["engines"] = ",".join(str(e) for e in engines)

        search_endpoint = f"http://{host}:{port}/search"
        search_matches: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                sr = await client.get(search_endpoint, params=search_params)
                if sr.status_code == 200:
                    try:
                        sb = sr.json()
                        for row in (sb.get("results") or [])[:10]:
                            if isinstance(row, dict):
                                search_matches.append({
                                    "url": row.get("url", ""),
                                    "title": row.get("title", ""),
                                    "hostname": urlparse(row.get("url", "")).netloc,
                                    "snippets": [row.get("content", "")] if row.get("content") else [],
                                })
                    except Exception:
                        pass
        except Exception:
            pass

        # Direct page fetch (primary content source)
        page_text = ""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; aidir-fetch/1.0)"},
            ) as client:
                pr = await client.get(url)
                if pr.status_code == 200:
                    extractor = _HTMLTextExtractor()
                    extractor.feed(pr.text)
                    page_text = extractor.get_text()
        except Exception as exc:
            if not search_matches:
                return WorkerResult(ok=False, error={"code": "SEARXNG_FETCH_FAILED", "message": str(exc)})

        # Build snippets: prefer direct page text, fall back to search snippets
        if page_text:
            snippets = [page_text]
            matches = [{"url": url, "title": "", "hostname": parsed.netloc, "snippets": [page_text]}]
        else:
            snippets = []
            matches = search_matches
            for m in search_matches:
                snippets.extend(m.get("snippets") or [])

        combined_text = "\n\n".join(snippets)
        text = combined_text[:max_chars]

        return WorkerResult(ok=True, data={
            "tool": "web_fetch",
            "provider": "searxng",
            "url": url,
            "query": search_q,
            "matches": matches,
            "snippets": snippets,
            "text": text,
            "truncated": len(combined_text) > len(text),
            "sources": {},
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
        url = str(args.get("url", "")).strip()

        if not url:
            return WorkerResult(ok=False, error={"code": "INVALID_ARGUMENT", "message": "url is required"})

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return WorkerResult(ok=False, error={"code": "INVALID_ARGUMENT", "message": "url must be absolute (https://...)"})

        configured = len(self._providers)
        enabled = 0
        tried = 0
        blacklisted_ids: list[str] = []
        empty_result: WorkerResult | None = None
        last_error: dict = {"code": "NO_PROVIDER", "message": "No fetch providers are configured or available"}

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
                result = await self._call_searxng_fetch(url, args, state.cfg)
            elif state.type == "brave":
                result = await self._call_brave_fetch(url, args, state.cfg)
            else:
                last_error = {"code": "UNSUPPORTED_PROVIDER", "message": f"Unknown provider type: {state.type}"}
                continue

            if result.ok:
                if state.type == "searxng" and bool(state.cfg.get("empty_is_error")) and self._is_empty_fetch_result(result.data):
                    empty_result = result
                    continue
                return result

            state.blacklist(self._cooldown)
            last_error = result.error or {"code": "PROVIDER_FAILED", "message": f"Provider '{state.id}' failed"}

        if configured == 0:
            return WorkerResult(ok=False, error={"code": "NO_PROVIDER", "message": "No fetch providers are configured"})
        if enabled == 0:
            return WorkerResult(ok=False, error={"code": "NO_PROVIDER_ENABLED", "message": "All fetch providers are disabled"})
        if tried == 0 and blacklisted_ids:
            return WorkerResult(
                ok=False,
                error={
                    "code": "PROVIDERS_COOLDOWN",
                    "message": f"All fetch providers are in cooldown: {', '.join(blacklisted_ids)}",
                    "details": f"Retry after ~{int(self._cooldown)}s or reduce provider_cooldown_seconds",
                },
            )

        if empty_result is not None:
            return empty_result

        return WorkerResult(ok=False, error=last_error)


worker = WebFetchWorker()
