"""
External MCP tool worker.
Discovers and proxies tools from remote MCP servers (HTTP only, MVP).
"""
from __future__ import annotations
import asyncio
import time
from typing import Any

import httpx

from core.worker import BaseToolWorker, WorkerResult
from core.task import Task
from core import log

class ExternalMcpWorker(BaseToolWorker):
    """Tool worker that discovers and proxies remote MCP tools."""
    id = "external_mcp"
    task_type = "tool"

    def __init__(self):
        self._core = None
        self._redis = None
        self._config = {}
        self._instance = "aidir"
        self._startup_time = 0.0
        self._mark_startup = True
        self._tools_cache: dict[str, dict[str, Any]] = {}
        self._cache_time = 0.0
        self._ttl = 300
        self._servers: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def initialize(self, config: dict) -> None:
        self._core = config.get("_core")
        self._redis = config.get("_redis") or (self._core.redis if self._core else None)
        self._config = config
        self._instance = str(
            config.get("_instance")
            or (self._core.config.get("instance", "aidir") if self._core else "aidir")
        )
        self._mark_startup = bool(config.get("_mark_startup", self._core is not None))
        self._startup_time = time.time()
        self._ttl = int(config.get("tools_ttl", 300))
        self._servers = dict(config.get("servers", {}) or {})
        self._tools_cache = {}
        self._cache_time = 0.0

        if self._mark_startup and self._redis is not None:
            await self._redis.set(self._startup_key(), str(self._startup_time))

    def _startup_key(self) -> str:
        """Return Redis key storing the last service startup time."""
        return f"{self._instance}:worker:{self.id}:startup_time"

    def _last_refresh_key(self) -> str:
        """Return Redis key storing the last successful tools refresh time."""
        return f"{self._instance}:worker:{self.id}:last_tools_refresh"

    async def _get_shared_timestamp(self, key: str) -> float:
        """Read shared timestamp from Redis, falling back to 0 when absent."""
        if self._redis is None:
            return 0.0

        raw = await self._redis.get(key)
        if raw in (None, ""):
            return 0.0

        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    async def _get_startup_time(self) -> float:
        """Return the latest known startup time for this worker."""
        if self._redis is None:
            return self._startup_time

        shared = await self._get_shared_timestamp(self._startup_key())
        return shared or self._startup_time

    async def _get_last_refresh_time(self) -> float:
        """Return the latest successful tools refresh timestamp."""
        if self._redis is None:
            return self._cache_time
        return await self._get_shared_timestamp(self._last_refresh_key())

    async def _set_last_refresh_time(self, value: float) -> None:
        """Persist the latest successful tools refresh timestamp."""
        self._cache_time = value
        if self._redis is not None:
            await self._redis.set(self._last_refresh_key(), str(value))

    async def _needs_shared_refresh(self, now: float) -> bool:
        """Return True when shared startup/TTL rules require a tools refresh."""
        last_refresh = await self._get_last_refresh_time()
        startup_time = await self._get_startup_time()

        if last_refresh <= 0:
            return True
        if startup_time > 0 and last_refresh < startup_time:
            return True
        return (now - last_refresh) >= self._ttl

    async def _needs_local_refresh(self, now: float) -> bool:
        """Return True when local execution needs a tools refresh."""
        if not self._tools_cache:
            return True
        return await self._needs_shared_refresh(now)

    async def _refresh_tools(self) -> None:
        """Fetch tool lists from all servers and update cache."""
        tools: dict[str, dict[str, Any]] = {}
        now = time.time()

        for srv_name, srv in self._servers.items():
            if srv.get("type") != "http":
                continue  # Only HTTP for MVP

            url = srv.get("url")
            if not url:
                continue

            log("worker", "info", f"Reading tools list from server={srv_name} url={url}", self.id)
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                    resp.raise_for_status()
                    data = resp.json()
                    for tool in (data.get("result", {}).get("tools", []) if isinstance(data.get("result"), dict) else []):
                        if not isinstance(tool, dict):
                            continue
                        name = tool.get("name")
                        if not name:
                            continue
                        merged = dict(tool)
                        merged["server"] = srv_name
                        merged["_server"] = srv_name
                        tool_name = str(name)
                        if tool_name not in tools:
                            tools[tool_name] = merged
                            continue

                        alias = f"{srv_name}__{tool_name}"
                        aliased = dict(merged)
                        aliased["name"] = alias
                        tools[alias] = aliased
                        log(
                            "worker",
                            "info",
                            f"Duplicate remote tool name preserved via alias original={tool_name} alias={alias} server={srv_name}",
                            self.id,
                        )
            except Exception as exc:
                details = str(exc).strip() or type(exc).__name__
                log("worker", "warn", f"Failed to read tools list from server={srv_name}: {details}", self.id)

        if not tools:
            log("worker", "warn", "Tools list build finished with no discovered tools", self.id)
            return

        self._tools_cache = tools
        await self._set_last_refresh_time(now)
        tool_names = sorted(tools.keys())
        log("worker", "info", f"Tools list build finished; tools={tool_names}", self.id)

    async def refresh_tools_if_due(self, reason: str = "manual", force: bool = False) -> bool:
        """Refresh tools only when they were not updated after startup or TTL expired."""
        async with self._lock:
            now = time.time()
            if not force and not await self._needs_shared_refresh(now):
                return False

            log("worker", "info", f"Tools refresh started reason={reason}", self.id)
            await self._refresh_tools()
            return True

    async def _get_tools(self) -> list[dict[str, Any]]:
        async with self._lock:
            if await self._needs_local_refresh(time.time()):
                await self._refresh_tools()
            return list(self._tools_cache.values())

    def get_tool_description(self) -> list[dict]:
        """Return cached tool descriptions (sync, for context injection)."""
        # For context injection, allow sync fallback (may be stale)
        return list(self._tools_cache.values())

    async def execute(self, task: Task, emit_chunk=None) -> WorkerResult:
        """Proxy tool call to remote MCP server."""
        args = (task.payload or {}).get("arguments", {})
        tool_id = (task.payload or {}).get("tool")

        await self._get_tools()  # Ensure cache is fresh
        tool = self._tools_cache.get(tool_id)
        if not tool:
            return WorkerResult(ok=False, error={"code": "TOOL_NOT_FOUND", "message": f"Tool {tool_id} not found"})

        srv_name = tool.get("_server")
        srv = self._servers.get(srv_name)
        if not srv or srv.get("type") != "http":
            return WorkerResult(ok=False, error={"code": "SERVER_NOT_FOUND", "message": f"Server for tool {tool_id} not found"})

        url = srv.get("url")
        if not url:
            return WorkerResult(ok=False, error={"code": "SERVER_URL_MISSING", "message": f"No URL for server {srv_name}"})

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool_id, "arguments": args}
                })
                data = resp.json()
                if "result" in data:
                    return WorkerResult(ok=True, data=data["result"])
                return WorkerResult(ok=False, error=data.get("error", {"code": "REMOTE_ERROR", "message": str(data)}))
        except Exception as exc:
            return WorkerResult(ok=False, error={"code": "NETWORK_ERROR", "message": str(exc)})

worker = ExternalMcpWorker()
