"""
web_fetch tool worker.
Uses Brave LLM Context API to fetch machine-ready grounding snippets for a URL.
"""
from __future__ import annotations

import os
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx

from core.worker import BaseToolWorker, WorkerResult
from core.task import Task


class WebFetchWorker(BaseToolWorker):
    """Tool worker that fetches URL grounding snippets via Brave."""

    task_type = "tool"

    def get_tool_description(self) -> dict:
        """Return MCP-compatible tool description."""
        return {
            "name": "fetch",
            "description": "Fetch URL-relevant grounding snippets via Brave LLM Context API.",
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
                        "description": "Compatibility option: truncate combined snippet text to this length.",
                        "minimum": 256,
                        "maximum": 100000,
                    },
                },
                "required": ["url"],
            },
        }

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
        """Fetch URL grounding snippets via Brave LLM Context endpoint."""
        if self._provider != "brave":
            return WorkerResult(ok=False, error={"code": "UNSUPPORTED_PROVIDER", "message": f"Unsupported provider: {self._provider}"})
        if not self._api_key:
            return WorkerResult(ok=False, error={"code": "MISSING_API_KEY", "message": "BRAVE_APIKEY/apiKey is not configured"})

        payload = task.payload or {}
        args = payload.get("arguments") or {}
        url = str(args.get("url", "")).strip()
        max_chars = self._int_arg(args.get("maxChars", 8000), 8000, 256, 100000)

        if not url:
            return WorkerResult(ok=False, error={"code": "INVALID_ARGUMENT", "message": "url is required"})

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return WorkerResult(ok=False, error={"code": "INVALID_ARGUMENT", "message": "url must be absolute (https://...)"})

        query = str(args.get("query", "")).strip()
        q = query if query else url
        q = f"{q} site:{parsed.netloc}"

        request_body: dict = {
            "q": q,
            "country": str(args.get("country", "us") or "us"),
            "search_lang": str(args.get("search_lang", "en") or "en"),
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
            "X-Subscription-Token": self._api_key,
        }

        endpoint = f"{self._base_url}/res/v1/llm/context"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(endpoint, json=request_body, headers=headers)
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
        grounding = body.get("grounding") if isinstance(body, dict) else {}
        generic = grounding.get("generic") if isinstance(grounding, dict) else []
        sources = body.get("sources") if isinstance(body, dict) else {}

        matches: list[dict] = []
        snippets: list[str] = []
        target_host = parsed.netloc
        for row in generic or []:
            if not isinstance(row, dict):
                continue
            row_url = str(row.get("url", ""))
            row_host = urlparse(row_url).netloc if row_url else ""
            row_snippets = [s.strip() for s in (row.get("snippets") or []) if isinstance(s, str) and s.strip()]
            if row_host == target_host or not matches:
                matches.append(
                    {
                        "url": row_url,
                        "title": row.get("title", ""),
                        "hostname": row_host,
                        "snippets": row_snippets,
                    }
                )
            if row_host == target_host:
                snippets.extend(row_snippets)

        if not snippets:
            for row in matches:
                snippets.extend(row.get("snippets") or [])

        combined_text = "\n\n".join(snippets)
        text = combined_text[:max_chars]

        return WorkerResult(
            ok=True,
            data={
                "tool": "web_fetch",
                "provider": "brave",
                "url": url,
                "query": q,
                "matches": matches,
                "snippets": snippets,
                "text": text,
                "truncated": len(combined_text) > len(text),
                "sources": sources,
            },
        )


worker = WebFetchWorker()
