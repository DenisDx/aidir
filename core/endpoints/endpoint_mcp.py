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
from core.task import STATUS_CANCELED, STATUS_COMPLETED, STATUS_FAILED
from core.task_types.task_tool import Task_tool
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

    async def initialize(self, core: "Core") -> None:
        """Bind endpoint to core instance and write startup log."""
        self._core = core
        log("http", "info", f"Endpoint {self.id} initialized", self.id)

    def create_app(self, core: "Core") -> FastAPI:
        """Create FastAPI app for MCP methods over JSON-RPC style HTTP."""
        self._core = core
        app = FastAPI(title=f"aidir-{self.id}", docs_url=None, redoc_url=None)

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
        """Return tools catalog from endpoint config."""
        tools_cfg = self._cfg.get("tools") or {}
        tools: list[dict] = []
        for tool_id, tool_meta in tools_cfg.items():
            tool_meta = tool_meta if isinstance(tool_meta, dict) else {}
            tools.append(
                {
                    "name": tool_id,
                    "description": tool_meta.get("description", f"Tool {tool_id}"),
                    "inputSchema": tool_meta.get("inputSchema", {"type": "object"}),
                }
            )
        return tools

    async def _handle_tool_call(self, req_id, params: dict) -> JSONResponse:
        """Create tool task, wait for completion, and return MCP result."""
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        tools_cfg = self._cfg.get("tools") or {}
        tool_cfg = tools_cfg.get(tool_name)
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
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}},
                status_code=503,
            )

        timeout = task.queue_timeout + task.run_timeout
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
