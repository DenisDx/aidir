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

    def get_tool_description(self) -> list[dict]:
        """Return MCP-compatible tool description as a list."""
        return [{
            "name": "selftest",
            "description": "System health self-test tool",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        }]

    async def initialize(self, config: dict) -> None:
        """Store core reference and optional worker settings."""
        self._core = config.get("_core")
        self._include_workers = bool(config.get("includeWorkers", True))
        self._include_resources = bool(config.get("includeResources", True))
        self._include_brave_api = bool(config.get("includeBraveApi", True))
        self._include_external_mcp = bool(config.get("includeExternalMcp", True))
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

        if self._include_external_mcp:
            external_mcp_check = await self._check_external_mcp()
            checks.append(external_mcp_check)
            if not external_mcp_check.get("ok", False):
                errors += 1

        report = {
            "tool": "selftest",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ok": errors == 0,
            "errors": errors,
            "checks": checks,
        }
        return WorkerResult(ok=True, data=report)

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

    async def _check_external_mcp(self) -> dict:
        """Verify external_mcp can discover tools and proxy at least one safe call."""
        external_worker = self._core.workers.get("external_mcp") if self._core else None
        if external_worker is None:
            return {
                "name": "external_mcp",
                "ok": True,
                "details": "skipped: worker not loaded",
            }

        try:
            tools = await external_worker._get_tools()
        except Exception as exc:
            return {
                "name": "external_mcp",
                "ok": False,
                "details": f"discovery_failed: {exc}",
            }

        discovered = [f"{tool.get('name')}|{tool.get('server', '?')}" for tool in tools if isinstance(tool, dict)]
        if not tools:
            return {
                "name": "external_mcp",
                "ok": False,
                "details": "no tools discovered",
            }

        target_name = None
        target_args = None
        for tool in tools:
            tool_name = str(tool.get("name", ""))
            if tool_name.endswith("search") or tool_name.endswith("__search"):
                target_name = tool_name
                target_args = {"query": "aidir health check", "count": 1}
                break

        if target_name is None:
            return {
                "name": "external_mcp",
                "ok": True,
                "details": {
                    "discovered": discovered,
                    "proxy_call": "skipped: no safe search-like tool found",
                },
            }

        result = await external_worker.execute(
            Task(
                type="tool",
                worker_id="external_mcp",
                payload={"tool": target_name, "arguments": target_args},
            )
        )

        if not result.ok:
            return {
                "name": "external_mcp",
                "ok": False,
                "details": {
                    "discovered": discovered,
                    "proxy_call": {
                        "tool": target_name,
                        "ok": False,
                        "error": result.error,
                    },
                },
            }

        data = result.data if isinstance(result.data, dict) else {}
        preview_items = data.get("items") if isinstance(data.get("items"), list) else []
        return {
            "name": "external_mcp",
            "ok": True,
            "details": {
                "discovered": discovered,
                "proxy_call": {
                    "tool": target_name,
                    "ok": True,
                    "items": len(preview_items),
                },
            },
        }


worker = SelftestWorker()
