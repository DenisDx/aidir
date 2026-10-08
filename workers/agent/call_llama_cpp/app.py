"""llama.cpp worker extending the shared OpenAI transport."""
from __future__ import annotations

from typing import Awaitable, Callable

from core.local_server_manager import LocalServerError
from core.task import Task
from core.task_types.task_agent import Task_agent
from core.worker import WorkerResult
from workers.agent.call_openai.app import CallOpenAIWorker


class CallLlamaCppWorker(CallOpenAIWorker):
    """Ensure a local llama.cpp server before using the OpenAI transport."""

    task_type = "agent"

    @staticmethod
    def _resolve_save_llm_request(payload: dict) -> bool:
        """Always retain exact llama.cpp exchanges for local-server diagnostics."""
        return True

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Start the selected llama.cpp server, then execute one agent task."""
        if not isinstance(task, Task_agent):
            return WorkerResult(
                ok=False,
                error={
                    "code": "WRONG_TASK_TYPE",
                    "message": f"Expected Task_agent, got {type(task).__name__}",
                },
            )

        provider_id = self._resolve_task_provider_id(task)
        try:
            await self._core.llama_cpp_server_manager.ensure_running(
                provider_id,
                str((task.payload or {}).get("model") or ""),
            )
        except LocalServerError as exc:
            return WorkerResult(ok=False, error={"code": exc.code, "message": str(exc)})

        return await super().execute(task, emit_chunk)


worker = CallLlamaCppWorker()
