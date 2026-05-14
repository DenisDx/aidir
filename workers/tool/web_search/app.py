"""
web_search tool worker.
Uses Brave Web Search API for real web results.
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable
from urllib.parse import urlencode

import httpx

from core.task import Task
from core.worker import BaseToolWorker, WorkerResult


class WebSearchWorker(BaseToolWorker):
    """Tool worker that queries Brave Web Search API."""

    task_type = "tool"

    def get_tool_description(self) -> list[dict]:
        """Return MCP-compatible tool description as a list."""
        return [{
            "name": "search",
            "description": "Search the web via Brave Search API and return ranked results.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (max 400 chars, 50 words).",
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
        """Read Brave API settings and request timeout."""
        self._provider = str(config.get("provider", "brave")).lower()
        self._api_key = str(config.get("apiKey") or os.getenv("BRAVE_APIKEY") or "").strip()
        # Use request_timeout (preferred) or timeoutSeconds (legacy)
        self._timeout = int(config.get("request_timeout") or config.get("timeoutSeconds", 100) or 100)
        self._base_url = str(config.get("baseUrl") or "https://api.search.brave.com").rstrip("/")

    @staticmethod
    def _int_arg(value, default: int, minimum: int, maximum: int) -> int:
        """Parse integer argument with bounds and fallback."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = int(default)
        return min(maximum, max(minimum, parsed))

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Perform Brave web search and return normalized result items."""
        if self._provider != "brave":
            return WorkerResult(
                ok=False,
                error={"code": "UNSUPPORTED_PROVIDER", "message": f"Unsupported provider: {self._provider}"},
            )

        if not self._api_key:
            return WorkerResult(
                ok=False,
                error={"code": "MISSING_API_KEY", "message": "BRAVE_APIKEY/apiKey is not configured"},
            )

        payload = task.payload or {}
        args = payload.get("arguments") or {}

        query = str(args.get("query", "")).strip()
        if not query:
            return WorkerResult(ok=False, error={"code": "INVALID_ARGUMENT", "message": "query is required"})

        count = self._int_arg(args.get("count", 10), 10, 1, 20)
        offset = self._int_arg(args.get("offset", 0), 0, 0, 9)

        params: dict[str, str | int] = {
            "q": query,
            "count": count,
            "offset": offset,
            "country": str(args.get("country", "us") or "us"),
            "search_lang": str(args.get("search_lang", "en") or "en"),
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
            "X-Subscription-Token": self._api_key,
        }

        url = f"{self._base_url}/res/v1/web/search"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params, headers=headers)
        except Exception as exc:
            return WorkerResult(ok=False, error={"code": "BRAVE_REQUEST_FAILED", "message": str(exc)})

        if response.status_code != 200:
            return WorkerResult(
                ok=False,
                error={
                    "code": "BRAVE_HTTP_ERROR",
                    "message": f"Brave returned HTTP {response.status_code}",
                    "details": response.text[:512],
                },
            )

        body = response.json()
        web_results = ((body.get("web") or {}).get("results") or []) if isinstance(body, dict) else []

        items: list[dict] = []
        for row in web_results:
            if not isinstance(row, dict):
                continue
            items.append(
                {
                    "title": row.get("title", ""),
                    "url": row.get("url", ""),
                    "description": row.get("description", ""),
                    "extra_snippets": row.get("extra_snippets") or [],
                    "age": row.get("age"),
                    "page_age": row.get("page_age"),
                }
            )

        query_meta = body.get("query") if isinstance(body, dict) else {}
        more_results = bool((query_meta or {}).get("more_results_available"))

        return WorkerResult(
            ok=True,
            data={
                "tool": "web_search",
                "provider": "brave",
                "request": {
                    "url": f"{url}?{urlencode(params, doseq=True)}",
                    "query": query,
                    "count": count,
                    "offset": offset,
                },
                "more_results_available": more_results,
                "items": items,
            },
        )


worker = WebSearchWorker()
