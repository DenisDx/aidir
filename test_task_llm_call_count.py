"""Regression tests for task-level LLM call counters in queue and WebUI APIs."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


redis_module = types.ModuleType("redis")
redis_asyncio = types.ModuleType("redis.asyncio")


class _Redis:
    """Stub redis client type for import-time compatibility."""


redis_asyncio.Redis = _Redis
redis_module.asyncio = redis_asyncio
sys.modules.setdefault("redis", redis_module)
sys.modules.setdefault("redis.asyncio", redis_asyncio)

from core.queue_manager import QueueManager
from core.task_types.task_agent import Task_agent
from webui.backend.app import create_app


class _FakeRedisCounter:
    """Tiny redis stub supporting the subset used by QueueManager."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        item = self.hashes.setdefault(key, {})
        next_value = int(item.get(field) or 0) + int(amount)
        item[field] = str(next_value)
        return next_value

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.hashes.setdefault(key, {}).update(mapping)


class _FakeTaskQueue:
    """In-memory queue stub for WebUI task endpoints."""

    def __init__(self, task: Task_agent) -> None:
        self._task = task

    def list_tasks(self):
        return [self._task]

    def get_task(self, task_id: str):
        if task_id == self._task.id:
            return self._task
        return None


class _FakeCore:
    """Minimal core stub for WebUI task endpoint tests."""

    def __init__(self, task: Task_agent) -> None:
        self.config = MagicMock()
        self.config.get.return_value = {}
        self.queue = _FakeTaskQueue(task)
        self.redis = MagicMock()
        self.workers = {}
        self.envid_registry = None


class TestTaskLlmCallCount(unittest.IsolatedAsyncioTestCase):
    """Validate task-level LLM call counting and API exposure."""

    async def test_queue_manager_increment_llm_call_count_persists_value(self) -> None:
        """Queue manager should persist and mirror llm_call_count increments."""
        task = Task_agent(payload={"model": "qwen3.5:9b"})
        redis = _FakeRedisCounter()
        queue = QueueManager(redis)

        first = await queue.increment_llm_call_count(task)
        second = await queue.increment_llm_call_count(task)

        self.assertEqual(first, 1)
        self.assertEqual(second, 2)
        self.assertEqual(task.llm_call_count, 2)
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["llm_call_count"], "2")

    def test_webui_task_endpoints_return_llm_call_count(self) -> None:
        """Dashboard and task viewer APIs should expose llm_call_count."""
        task = Task_agent(id="task-1", payload={"model": "qwen3.5:9b"}, stream=False)
        task.status = "running"
        task.worker_id = "openaix"
        task.llm_call_count = 4
        task.llm_call_history = [{"call_index": 4, "status": "timeout", "url_path": "/api/chat"}]
        core = _FakeCore(task)

        with patch("webui.backend.app._get_session", return_value={"permissions": ["all"], "login": "tester"}):
            client = TestClient(create_app(core=core))

            tasks_response = client.get("/api/tasks")
            self.assertEqual(tasks_response.status_code, 200)
            self.assertEqual(tasks_response.json()["tasks"][0]["llm_call_count"], 4)

            task_response = client.get(f"/api/tasks/viewer/{task.id}")
            self.assertEqual(task_response.status_code, 200)
            self.assertEqual(task_response.json()["task"]["llm_call_count"], 4)
            self.assertEqual(task_response.json()["task"]["llm_call_history"][0]["status"], "timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)