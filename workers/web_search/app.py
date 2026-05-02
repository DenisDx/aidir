"""
web_search test worker.
Returns deterministic mock search results for MCP tool testing.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from core.worker import BaseWorker, WorkerResult
from core.task import Task


class WebSearchWorker(BaseWorker):
    """Test tool worker that returns fake search documents."""

    task_type = "tool"

    async def initialize(self, config: dict) -> None:
        """Store optional provider id used only for diagnostics."""
        self._provider = str(config.get("provider", "mock"))

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Return static search hits based on query text."""
        payload = task.payload or {}
        args = payload.get("arguments") or {}
        query = str(args.get("query", "")).strip()
        limit = int(args.get("limit", 3) or 3)
        if limit < 1:
            limit = 1
        if limit > 10:
            limit = 10

        results = []
        base = query or "example"
        for i in range(limit):
            idx = i + 1
            results.append(
                {
                    "title": f"{base} result {idx}",
                    "url": f"https://example.com/search/{idx}",
                    "snippet": f"Mock snippet {idx} for query '{base}'",
                }
            )

        return WorkerResult(
            ok=True,
            data={
                "tool": "web_search",
                "provider": self._provider,
                "query": query,
                "items": results,
            },
        )


worker = WebSearchWorker()
