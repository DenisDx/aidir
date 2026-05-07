"""
context_builder worker.
Builds/adjusts request context before agent task execution.
"""
from __future__ import annotations

import copy
import json
from typing import Awaitable, Callable

from core.task import Task
from core.worker import BaseWorker, WorkerResult


class ContextBuilderWorker(BaseWorker):
    """Worker that applies context_builder rules to request payload."""

    task_type = "context_builder"

    async def initialize(self, config: dict) -> None:
        """Initialize worker. Inputs: config dict. Output: None."""

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Apply context modifications and return updated payload."""
        payload = task.payload or {}
        request_payload = payload.get("request_payload")
        context_builder = payload.get("context_builder")

        if not isinstance(request_payload, dict):
            return WorkerResult(
                ok=False,
                error={"code": "INVALID_PAYLOAD", "message": "request_payload must be an object"},
            )

        if not isinstance(context_builder, dict):
            return WorkerResult(ok=True, data={"payload": request_payload})

        if "tools_to_inject" not in context_builder:
            return WorkerResult(ok=True, data={"payload": request_payload})

        tools_value = context_builder.get("tools_to_inject")
        if isinstance(tools_value, str):
            tools = [tools_value]
        elif isinstance(tools_value, list):
            tools = [str(item) for item in tools_value]
        else:
            return WorkerResult(
                ok=False,
                error={"code": "INVALID_TOOLS", "message": "tools_to_inject must be string or array"},
            )

        updated = copy.deepcopy(request_payload)
        instruction = self._build_tools_instruction(tools)
        self._append_to_system_message(updated, instruction)

        return WorkerResult(ok=True, data={"payload": updated})

    @staticmethod
    def _build_tools_instruction(tools: list[str]) -> str:
        """Build compact instruction string for system context."""
        tools_json = json.dumps(tools, ensure_ascii=False)
        return f"[context_builder] tools_to_inject={tools_json}"

    def _append_to_system_message(self, payload: dict, instruction: str) -> None:
        """Append tools instruction to existing system message or create one."""
        messages = payload.get("messages")
        if not isinstance(messages, list):
            payload["messages"] = [{"role": "system", "content": instruction}]
            return

        system_msg = None
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                system_msg = msg
                break

        if system_msg is None:
            messages.insert(0, {"role": "system", "content": instruction})
            return

        content = system_msg.get("content")
        if isinstance(content, str):
            system_msg["content"] = f"{content}\n\n{instruction}" if content else instruction
            return

        if isinstance(content, list):
            content.append({"type": "text", "text": instruction})
            return

        system_msg["content"] = instruction


worker = ContextBuilderWorker()
