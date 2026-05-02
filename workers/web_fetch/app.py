"""
web_fetch test worker.
Fetches URL content preview for MCP tool testing.
"""
from __future__ import annotations

from typing import Awaitable, Callable

import httpx

from core.worker import BaseWorker, WorkerResult
from core.task import Task


class WebFetchWorker(BaseWorker):
    """Test tool worker that downloads a URL and returns a short preview."""

    task_type = "tool"

    async def initialize(self, config: dict) -> None:
        """Read timeout from worker config."""
        self._timeout = int(config.get("timeoutSeconds", 20) or 20)

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Download URL and return status code with truncated body."""
        payload = task.payload or {}
        args = payload.get("arguments") or {}
        url = str(args.get("url", "")).strip()
        max_chars = int(args.get("maxChars", 4000) or 4000)
        if max_chars < 256:
            max_chars = 256
        if max_chars > 20000:
            max_chars = 20000

        if not url:
            return WorkerResult(ok=False, error={"code": "INVALID_ARGUMENT", "message": "url is required"})

        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.get(url)
        except Exception as exc:
            return WorkerResult(ok=False, error={"code": "FETCH_FAILED", "message": str(exc)})

        body = response.text[:max_chars]
        return WorkerResult(
            ok=True,
            data={
                "tool": "web_fetch",
                "url": str(response.url),
                "status": response.status_code,
                "contentType": response.headers.get("content-type", ""),
                "body": body,
                "truncated": len(response.text) > len(body),
            },
        )


worker = WebFetchWorker()
