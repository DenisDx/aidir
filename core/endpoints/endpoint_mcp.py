"""
MCP-like HTTP endpoint.
Provides minimal tools discovery and tools invocation routes for MVP usage.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from core.endpoint import BaseEndpoint
from core.error_logging import attach_request_id_middleware, get_or_create_request_id, log_exception
from core.task import STATUS_CANCELED, STATUS_COMPLETED, STATUS_FAILED
from core.task_types.task_tool import Task_tool
from core.worker import BaseToolWorker
from core import log

if TYPE_CHECKING:
    from core.app import Core


class Endpoint_mcp(BaseEndpoint):
    """Minimal MCP endpoint with tools/list and tools/call methods."""

    api = "mcp"

    def __init__(self, endpoint_cfg: dict) -> None:
        self.id = endpoint_cfg.get("id", "mcp")
        self._cfg = endpoint_cfg
        self._core: "Core | None" = None
        self._server_name = endpoint_cfg.get("serverName", "aidir-mcp")
        self._server_version = endpoint_cfg.get("serverVersion", "0.1.0")
        self._protocol_version = endpoint_cfg.get("protocolVersion", "2024-11-05")
        # Timeout for entire endpoint request (queue + execution)
        self._request_timeout = int(endpoint_cfg.get("request_timeout", 100))

    async def initialize(self, core: "Core") -> None:
        """Bind endpoint to core instance and write startup log."""
        self._core = core
        log("http", "info", f"Endpoint {self.id} initialized", self.id)

    def create_app(self, core: "Core") -> FastAPI:
        """Create FastAPI app for MCP methods over JSON-RPC style HTTP."""
        self._core = core
        app = FastAPI(title=f"aidir-{self.id}", docs_url=None, redoc_url=None)
        attach_request_id_middleware(app)

        @app.exception_handler(Exception)
        async def unhandled_exception_handler(request: Request, exc: Exception):
            """Log unhandled endpoint exceptions with traceback and request id."""
            request_id = get_or_create_request_id(request)
            log_exception(
                "http",
                self.id,
                f"Unhandled exception method={request.method} path={request.url.path}",
                exc,
                request_id=request_id,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Internal server error",
                        "data": {"request_id": request_id},
                    },
                },
                headers={"X-Request-ID": request_id},
            )

        @app.post("/mcp")
        async def mcp_rpc(request: Request):
            return await self._handle_rpc(request)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        return app

    async def _handle_rpc(self, request: Request) -> JSONResponse:
        """Dispatch MCP request by method name."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                status_code=400,
            )

        method = body.get("method")
        req_id = body.get("id")

        if method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": self._protocol_version,
                        "capabilities": {
                            "tools": {
                                "listChanged": False,
                            }
                        },
                        "serverInfo": {
                            "name": self._server_name,
                            "version": self._server_version,
                        },
                    },
                }
            )

        if method == "ping":
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})

        if method == "notifications/initialized":
            # Notification path: client confirms initialize handshake completion.
            return Response(status_code=204)

        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": self._build_tools_list()}})

        if method == "tools/call":
            params = body.get("params") or {}
            return await self._handle_tool_call(req_id, params)

        return JSONResponse(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}},
            status_code=404,
        )

    def _build_tools_list(self) -> list[dict]:
        """Return tools catalog from endpoint config and worker self-descriptions."""
        registry = self._resolve_tools_registry()
        return [
            {
                "name": tool_name,
                "description": spec.get("description", f"Tool {tool_name}"),
                "inputSchema": spec.get("inputSchema", {"type": "object", "properties": {}}),
            }
            for tool_name, spec in registry.items()
        ]

    def _resolve_tools_registry(self) -> dict[str, dict]:
        """Resolve published tool names to worker ids and metadata."""
        tools_cfg = self._cfg.get("tools") or {}
        registry: dict[str, dict] = {}

        for tool_name, raw_meta in tools_cfg.items():
            worker_id = None
            if isinstance(raw_meta, str):
                worker_id = raw_meta
            elif isinstance(raw_meta, dict):
                worker_id = raw_meta.get("worker")

            if not worker_id:
                continue

            spec = {
                "worker": str(worker_id),
                "description": f"Tool {tool_name}",
                "inputSchema": {"type": "object", "properties": {}},
            }

            worker = self._core.workers.get(str(worker_id)) if self._core else None
            if isinstance(worker, BaseToolWorker):
                worker_spec = worker.get_tool_description() or {}
                if isinstance(worker_spec.get("description"), str):
                    spec["description"] = worker_spec["description"]
                if isinstance(worker_spec.get("inputSchema"), dict):
                    spec["inputSchema"] = worker_spec["inputSchema"]

            registry[str(tool_name)] = spec

        return registry

    async def _handle_tool_call(self, req_id, params: dict) -> JSONResponse:
        """Create tool task, wait for completion, and return MCP result."""
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        tools_registry = self._resolve_tools_registry()
        tool_cfg = tools_registry.get(tool_name)
        if not isinstance(tool_cfg, dict):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"}},
                status_code=422,
            )

        task = Task_tool(payload={"tool": tool_name, "arguments": arguments}, external=True)
        task.worker_id = tool_cfg.get("worker")

        cfg_tasks = self._core.config.get("tasks", {}) or {}
        task.queue_timeout = int(cfg_tasks.get("queue_timeout", 300))
        task.run_timeout = int(cfg_tasks.get("run_timeout", 300))

        try:
            await self._core.on_task_added(task)
        except Exception as exc:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32000,
                        "message": str(exc),
                        "data": {"reason": getattr(exc, "code", "QUEUE_ERROR")},
                    },
                },
                status_code=503,
            )

        timeout = self._request_timeout
        try:
            await asyncio.wait_for(task._done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._core.queue.mark_canceled(task)
            asyncio.create_task(self._core.delete_task(task.id))
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "Tool call timed out"}},
                status_code=504,
            )

        asyncio.create_task(self._core.delete_task(task.id))

        if task.status == STATUS_COMPLETED:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": task.result or {}})

        if task.status == STATUS_FAILED:
            err = task.error or {"code": "TOOL_ERROR", "message": "Tool failed"}
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32002, "message": err.get("message", "Tool failed"), "data": err}},
                status_code=502,
            )

        if task.status == STATUS_CANCELED:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32003, "message": "Tool call canceled"}},
                status_code=503,
            )

        return JSONResponse(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32004, "message": "Unknown tool state"}},
            status_code=500,
        )
