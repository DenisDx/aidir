"""
reject_agent worker.
Returns controlled errors to validate reject policies (retry/fallback/cancel).
"""
from __future__ import annotations

from core.worker import BaseWorker, WorkerResult
from core.task import Task


class RejectAgentWorker(BaseWorker):
    task_type = "agent"

    async def initialize(self, config: dict) -> None:
        self._default_code = str(config.get("defaultCode", "UPSTREAM_UNREACHABLE"))

    async def execute(self, task: Task, emit_chunk=None) -> WorkerResult:
        payload = task.payload or {}
        code = str(payload.get("reject_code", self._default_code))
        return WorkerResult(
            ok=False,
            error={
                "code": code,
                "message": f"reject_agent simulated error: {code}",
            },
        )


worker = RejectAgentWorker()
