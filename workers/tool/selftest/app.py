"""
selftest tool worker.
Runs health checks for core runtime and returns a structured report.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from core.task import Task
from core.worker import BaseToolWorker, WorkerResult


class SelftestWorker(BaseToolWorker):
    """Tool worker that checks health of redis, queue, workers, and resources."""

    task_type = "tool"

    def get_tool_description(self) -> dict:
        """Return MCP-compatible tool description."""
        return {
            "name": "selftest",
            "description": "System health self-test tool",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        }

    async def initialize(self, config: dict) -> None:
        """Store core reference and optional worker settings."""
        self._core = config.get("_core")
        self._include_workers = bool(config.get("includeWorkers", True))
        self._include_resources = bool(config.get("includeResources", True))
        self._include_brave_api = bool(config.get("includeBraveApi", True))
        self._brave_timeout = int(config.get("braveTimeoutSeconds", 10) or 10)

    async def execute(self, task: Task, emit_chunk=None) -> WorkerResult:
        """Run health checks and return report with summary and checks list."""
        if self._core is None:
            return WorkerResult(
                ok=False,
                error={"code": "SELFTEST_NOT_INITIALIZED", "message": "Core reference is not available"},
            )

        checks: list[dict] = []
        errors = 0

        # Redis ping
        try:
            await self._core.redis.ping()
            checks.append({"name": "redis_ping", "ok": True, "details": "pong"})
        except Exception as exc:
            errors += 1
            checks.append({"name": "redis_ping", "ok": False, "details": str(exc)})

        # Queue sizes by type
        try:
            queue_stats = {}
            ns = self._core.config.get("instance", "aidir")
            for ttype in ("agent", "tool", "request"):
                key = f"{ns}:queue:{ttype}"
                queue_stats[ttype] = int(await self._core.redis.zcard(key))
            checks.append({"name": "queue_sizes", "ok": True, "details": queue_stats})
        except Exception as exc:
            errors += 1
            checks.append({"name": "queue_sizes", "ok": False, "details": str(exc)})

        # Workers registry
        if self._include_workers:
            try:
                workers = {
                    wid: {
                        "task_type": w.task_type,
                        "enabled": bool(w.enabled),
                    }
                    for wid, w in self._core.workers.items()
                }
                checks.append({"name": "workers_loaded", "ok": True, "details": workers})
            except Exception as exc:
                errors += 1
                checks.append({"name": "workers_loaded", "ok": False, "details": str(exc)})

        # Resources snapshot
        if self._include_resources:
            try:
                resources = self._core.resources.snapshot() if self._core.resources else []
                checks.append({"name": "resources", "ok": True, "details": resources})
            except Exception as exc:
                errors += 1
                checks.append({"name": "resources", "ok": False, "details": str(exc)})

        # Brave API health via web_search worker config
        if self._include_brave_api:
            brave_check = await self._check_brave_web_search()
            checks.append(brave_check)
            if not brave_check.get("ok", False):
                errors += 1

        report = {
            "tool": "selftest",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ok": errors == 0,
            "errors": errors,
            "checks": checks,
        }
        return WorkerResult(ok=(errors == 0), data=report, error=None if errors == 0 else {"code": "SELFTEST_FAILED", "message": "One or more checks failed", "report": report})

    async def _check_brave_web_search(self) -> dict:
        """Run a lightweight Brave Web Search API check using configured web_search settings."""
        ws_cfg = self._core.config.get("workers.items.web_search") or {}
        provider = str(ws_cfg.get("provider", "")).lower()
        if provider != "brave":
            return {
                "name": "brave_web_search_api",
                "ok": True,
                "details": f"skipped: workers.items.web_search.provider={provider or 'unset'}",
            }

        api_key = str(ws_cfg.get("apiKey") or os.getenv("BRAVE_APIKEY") or "").strip()
        if not api_key:
            return {
                "name": "brave_web_search_api",
                "ok": False,
                "details": "missing BRAVE_APIKEY/apiKey",
            }

        base_url = str(ws_cfg.get("baseUrl") or "https://api.search.brave.com").rstrip("/")
        url = f"{base_url}/res/v1/web/search"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }
        params = {
            "q": "aidir health check",
            "count": 1,
            "safesearch": "moderate",
        }

        try:
            async with httpx.AsyncClient(timeout=self._brave_timeout) as client:
                response = await client.get(url, params=params, headers=headers)
        except Exception as exc:
            return {
                "name": "brave_web_search_api",
                "ok": False,
                "details": f"request_failed: {exc}",
            }

        if response.status_code != 200:
            return {
                "name": "brave_web_search_api",
                "ok": False,
                "details": f"http_{response.status_code}: {response.text[:256]}",
            }

        try:
            body = response.json()
        except Exception:
            body = {}

        web_results = ((body.get("web") or {}).get("results") or []) if isinstance(body, dict) else []
        return {
            "name": "brave_web_search_api",
            "ok": True,
            "details": {
                "status": response.status_code,
                "provider": provider,
                "results": len(web_results),
            },
        }


worker = SelftestWorker()
