"""
External MCP tool worker.
Discovers and proxies tools from remote MCP servers (HTTP only, MVP).
"""
from __future__ import annotations
import asyncio
import base64
import copy
import time
from typing import Any

import httpx

from core.worker import BaseToolWorker, WorkerResult
from core.task import Task
from core.config_merger import update_config
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
        self._worker_auth: dict[str, Any] = {}
        self._base_worker_config: dict[str, Any] = {}
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
        self._base_worker_config = {
            k: v for k, v in dict(config).items() if not str(k).startswith("_")
        }
        self._ttl = int(self._base_worker_config.get("tools_ttl", 300))
        self._servers = dict(self._base_worker_config.get("servers", {}) or {})
        self._worker_auth = dict(self._base_worker_config.get("auth", {}) or {})
        self._tools_cache = {}
        self._cache_time = 0.0

        # Build the initial tool list before any context injection runs.
        await self.refresh_tools_if_due(reason="startup", force=True)

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
                auth_cfg = self._resolve_auth_config(self._worker_auth, srv.get("auth"))
                headers = self._build_auth_headers(auth_cfg)
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers=headers,
                    )
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

    @staticmethod
    def _resolve_auth_config(worker_auth: Any, server_auth: Any) -> dict[str, Any]:
        """Merge worker-level auth with per-server auth (server auth wins)."""
        merged: dict[str, Any] = {}
        if isinstance(worker_auth, dict):
            update_config(merged, copy.deepcopy(worker_auth))
        if isinstance(server_auth, dict):
            update_config(merged, copy.deepcopy(server_auth))
        return merged

    @staticmethod
    def _build_auth_headers(auth_cfg: dict[str, Any]) -> dict[str, str]:
        """Build HTTP headers from auth config.

        Supported formats:
          - {token: "..."} -> Bearer
          - {type: "bearer", token: "..."}
          - {type: "basic", username: "...", password: "..."}
          - {authorization: "<raw value>"}
          - {headers: {"X-...": "..."}}
        """
        if not isinstance(auth_cfg, dict):
            return {}

        headers: dict[str, str] = {}
        raw_headers = auth_cfg.get("headers")
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                if isinstance(key, str) and isinstance(value, str):
                    headers[key] = value

        raw_authorization = auth_cfg.get("authorization")
        if isinstance(raw_authorization, str) and raw_authorization.strip():
            headers["Authorization"] = raw_authorization.strip()

        auth_type = str(auth_cfg.get("type", "bearer")).strip().lower()
        token = auth_cfg.get("token")
        if isinstance(token, str) and token.strip() and auth_type in {"bearer", "token", ""}:
            headers["Authorization"] = f"Bearer {token.strip()}"

        if auth_type == "basic":
            username = auth_cfg.get("username")
            password = auth_cfg.get("password")
            if isinstance(username, str) and isinstance(password, str):
                raw = f"{username}:{password}".encode("utf-8")
                headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"

        return headers

    def _effective_worker_config_for_task(self, task: Task) -> dict[str, Any]:
        """Resolve effective worker config for task envid using core-provided merge logic."""
        payload = task.payload if isinstance(task.payload, dict) else {}
        envid_id = payload.get("envid")
        if not isinstance(envid_id, str) or not envid_id.strip() or self._core is None:
            return copy.deepcopy(self._base_worker_config)

        getter = getattr(self._core, "get_effective_worker_config", None)
        if not callable(getter):
            return copy.deepcopy(self._base_worker_config)

        merged = getter(self.id, envid_id.strip())
        if not isinstance(merged, dict):
            return copy.deepcopy(self._base_worker_config)
        return copy.deepcopy(merged)

    def _effective_servers_for_task(self, task: Task) -> dict[str, dict[str, Any]]:
        """Resolve servers map for current task, including envid worker overrides."""
        effective_cfg = self._effective_worker_config_for_task(task)
        merged = copy.deepcopy(self._base_worker_config)
        update_config(merged, effective_cfg)
        servers = merged.get("servers", {})
        return dict(servers) if isinstance(servers, dict) else {}

    async def execute(self, task: Task, emit_chunk=None) -> WorkerResult:
        """Proxy tool call to remote MCP server."""
        args = (task.payload or {}).get("arguments", {})
        tool_id = (task.payload or {}).get("tool")
        effective_cfg = self._effective_worker_config_for_task(task)
        worker_auth = effective_cfg.get("auth") if isinstance(effective_cfg, dict) else {}
        servers = self._effective_servers_for_task(task)

        await self._get_tools()  # Ensure cache is fresh
        tool = self._tools_cache.get(tool_id)
        if not tool:
            return WorkerResult(ok=False, error={"code": "TOOL_NOT_FOUND", "message": f"Tool {tool_id} not found"})

        srv_name = tool.get("_server")
        srv = servers.get(srv_name) or self._servers.get(srv_name)
        if not srv or srv.get("type") != "http":
            return WorkerResult(ok=False, error={"code": "SERVER_NOT_FOUND", "message": f"Server for tool {tool_id} not found"})

        url = srv.get("url")
        if not url:
            return WorkerResult(ok=False, error={"code": "SERVER_URL_MISSING", "message": f"No URL for server {srv_name}"})

        remote_tool_name = str(tool.get("name") or tool_id)

        try:
            auth_cfg = self._resolve_auth_config(worker_auth, srv.get("auth"))
            headers = self._build_auth_headers(auth_cfg)
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": remote_tool_name, "arguments": args}
                }, headers=headers)

                try:
                    data = resp.json()
                except ValueError:
                    body_preview = (resp.text or "")[:500]
                    log(
                        "worker",
                        "warn",
                        f"Remote MCP returned non-JSON response server={srv_name} tool={remote_tool_name} status={resp.status_code}",
                        self.id,
                    )
                    return WorkerResult(
                        ok=False,
                        error={
                            "code": "REMOTE_INVALID_RESPONSE",
                            "message": "Remote server returned non-JSON response",
                            "status": resp.status_code,
                            "body": body_preview,
                        },
                    )

                if not isinstance(data, dict):
                    return WorkerResult(
                        ok=False,
                        error={
                            "code": "REMOTE_INVALID_RESPONSE",
                            "message": "Remote server returned invalid JSON payload",
                            "status": resp.status_code,
                        },
                    )

                if "error" in data:
                    remote_error = data.get("error")
                    normalized_error = remote_error if isinstance(remote_error, dict) else {"message": str(remote_error)}
                    normalized_error.setdefault("code", "REMOTE_ERROR")
                    normalized_error.setdefault("message", "Remote MCP server returned an error")
                    normalized_error["status"] = resp.status_code
                    return WorkerResult(ok=False, error=normalized_error)

                if resp.status_code >= 400:
                    return WorkerResult(
                        ok=False,
                        error={
                            "code": "REMOTE_HTTP_ERROR",
                            "message": f"Remote server returned HTTP {resp.status_code}",
                            "status": resp.status_code,
                            "body": str(data)[:500],
                        },
                    )

                if "result" in data:
                    return WorkerResult(ok=True, data=data["result"])

                return WorkerResult(
                    ok=False,
                    error={
                        "code": "REMOTE_INVALID_RESPONSE",
                        "message": "Remote server response has no result or error",
                        "status": resp.status_code,
                        "body": str(data)[:500],
                    },
                )
        except httpx.RequestError as exc:
            return WorkerResult(ok=False, error={"code": "NETWORK_ERROR", "message": str(exc)})
        except Exception as exc:
            return WorkerResult(ok=False, error={"code": "REMOTE_CALL_ERROR", "message": str(exc)})

worker = ExternalMcpWorker()
