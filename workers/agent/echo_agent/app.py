"""
echo_agent worker.
Returns a deterministic success payload for fallback verification.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.worker import BaseWorker, WorkerResult
from core.task import Task


class EchoAgentWorker(BaseWorker):
    task_type = "agent"

    async def execute(self, task: Task, emit_chunk=None) -> WorkerResult:
        content = "fallback-success"
        return WorkerResult(
            ok=True,
            data={
                "model": (task.payload or {}).get("model", "echo-agent"),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "message": {"role": "assistant", "content": content},
                "done": True,
            },
        )


worker = EchoAgentWorker()
