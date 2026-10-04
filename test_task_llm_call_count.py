"""Regression tests for task-level LLM call counters in queue and WebUI APIs."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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
from core.audit_log import AuditLog
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

    async def extend_active_timeout(self, task_id: str, seconds: int = 60):
        if task_id != self._task.id or self._task.status not in {"queued", "running"}:
            return None
        self._task.queue_timeout += seconds
        self._task.run_timeout += seconds
        return self._task


class _FakeCore:
    """Minimal core stub for WebUI task endpoint tests."""

    def __init__(self, task: Task_agent, audit_log: AuditLog | None = None) -> None:
        self.config = MagicMock()
        self.config.get.return_value = {}
        self.queue = _FakeTaskQueue(task)
        self.audit_log = audit_log
        self.redis = MagicMock(
            get=AsyncMock(return_value=None),
            scan=AsyncMock(return_value=(0, [])),
        )
        self.workers = {}
        self.resources = None
        self.envid_registry = None

    def get_runtime_status(self):
        return {}


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

    async def test_queue_manager_serializes_bounded_diagnostics_off_event_loop(self) -> None:
        """Queue manager should offload compact diagnostics without raw stream bodies."""
        task = Task_agent(payload={"model": "qwen3.5:9b"})
        task.llm_call_history = [{"raw_sse": ["data: chunk"] * 100}]
        redis = _FakeRedisCounter()
        queue = QueueManager(redis)

        with patch(
            "core.queue_manager.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value="[\"serialized\"]",
        ) as serialize:
            await queue.persist_llm_call_diagnostics(task)

        serialize.assert_awaited_once()
        self.assertEqual(serialize.await_args.args[0], json.dumps)
        self.assertEqual(serialize.await_args.args[1], [{}])
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["llm_call_history"], "[\"serialized\"]")

    async def test_queue_manager_extends_active_timeout_and_persists_it(self) -> None:
        """Queue manager should extend the timeout currently governing a live task."""
        task = Task_agent(payload={"model": "qwen3.5:9b"})
        redis = _FakeRedisCounter()
        queue = QueueManager(redis)
        queue._tasks[task.id] = task

        task.status = "queued"
        updated = await queue.extend_active_timeout(task.id)
        self.assertIs(updated, task)
        self.assertEqual(task.queue_timeout, 360)
        self.assertEqual(task.run_timeout, 360)
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["queue_timeout"], "360")
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["run_timeout"], "360")

        task.status = "running"
        updated = await queue.extend_active_timeout(task.id)
        self.assertIs(updated, task)
        self.assertEqual(task.queue_timeout, 420)
        self.assertEqual(task.run_timeout, 420)
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["queue_timeout"], "420")
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["run_timeout"], "420")

    async def test_queue_manager_clears_errors_when_task_resumes_or_completes(self) -> None:
        """Remove stale failure data when a task starts or completes successfully."""
        task = Task_agent(payload={"model": "qwen3.5:9b"})
        task.error = {"code": "SERVICE_RESTARTED", "message": "Task interrupted by service restart"}
        redis = _FakeRedisCounter()
        queue = QueueManager(redis)
        queue._tasks[task.id] = task

        await queue.mark_running(task.id, "call_ollama")
        self.assertEqual(task.status, "running")
        self.assertIsNone(task.error)
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["error"], "")
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["error_code"], "")

        task.error = {"code": "EXCEPTION", "message": "old failure"}
        await queue.mark_completed(task)
        self.assertEqual(task.status, "completed")
        self.assertIsNone(task.error)
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["error"], "")
        self.assertEqual(redis.hashes[f"aidir:task:{task.id}"]["error_code"], "")

    def test_webui_task_endpoints_return_llm_call_count(self) -> None:
        """Dashboard and task viewer APIs should expose llm_call_count."""
        task = Task_agent(id="task-1", payload={"model": "qwen3.5:9b"}, stream=False)
        task.status = "running"
        task.worker_id = "openaix"
        task.llm_call_count = 4
        task.llm_call_history = [{
            "call_index": 4,
            "status": "started",
            "url_path": "/api/chat",
            "request_text": "user: Diagnose the last hanging step in full detail",
            "request_preview": "user: Diagnose the last hanging step",
        }]
        core = _FakeCore(task)

        with patch("webui.backend.app._get_session", return_value={"permissions": ["all"], "login": "tester"}):
            client = TestClient(create_app(core=core))

            tasks_response = client.get("/api/tasks")
            self.assertEqual(tasks_response.status_code, 200)
            self.assertEqual(tasks_response.json()["tasks"][0]["llm_call_count"], 4)

            task_response = client.get(f"/api/tasks/viewer/{task.id}")
            self.assertEqual(task_response.status_code, 200)
            self.assertEqual(task_response.json()["task"]["llm_call_count"], 4)
            self.assertEqual(task_response.json()["task"]["llm_call_history"][0]["status"], "started")
            self.assertEqual(task_response.json()["task"]["llm_call_history"][0]["request_text"], "user: Diagnose the last hanging step in full detail")
            self.assertEqual(task_response.json()["task"]["llm_call_history"][0]["request_preview"], "user: Diagnose the last hanging step")

    def test_webui_extends_the_timeout_for_the_active_task_status(self) -> None:
        """WebUI should extend the timeout applicable to queued and running tasks."""
        task = Task_agent(id="task-1", payload={"model": "qwen3.5:9b"}, stream=False)
        core = _FakeCore(task)

        with patch("webui.backend.app._get_session", return_value={"permissions": ["all"], "login": "tester"}):
            client = TestClient(create_app(core=core))

            task.status = "queued"
            queued_response = client.post(f"/api/tasks/{task.id}/extend-timeout")
            self.assertEqual(queued_response.status_code, 200)
            self.assertEqual(queued_response.json()["task"]["queue_timeout"], 360)
            self.assertEqual(queued_response.json()["task"]["run_timeout"], 360)

            task.status = "running"
            running_response = client.post(f"/api/tasks/{task.id}/extend-timeout")
            self.assertEqual(running_response.status_code, 200)
            self.assertEqual(running_response.json()["task"]["queue_timeout"], 420)
            self.assertEqual(running_response.json()["task"]["run_timeout"], 420)

    def test_task_viewer_keeps_detail_compact_and_serves_attachment_by_id(self) -> None:
        """Expose only audit manifest metadata and resolve an image body through its opaque ID."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            task = Task_agent(id="task-1", payload={"model": "example"}, stream=False)
            audit_log = AuditLog(temporary_directory)
            event = audit_log.record_body_event(
                "client_response",
                b"image-bytes",
                content_type="image/png",
                task_id=task.id,
            )
            core = _FakeCore(task, audit_log)

            with patch("webui.backend.app._get_session", return_value={"permissions": ["all"], "login": "tester"}):
                client = TestClient(create_app(core=core))
                detail = client.get(f"/api/tasks/viewer/{task.id}/detail")
                self.assertEqual(detail.status_code, 200)
                self.assertNotIn("data", detail.json()["audit_events"][0])

                unsupported = client.get(f"/api/tasks/viewer/{task.id}/raw?type=task&event_id={event['event_id']}")
                self.assertEqual(unsupported.status_code, 400)

                file_id = event["body_file"]["file_id"]
                response = client.get(f"/api/tasks/viewer/audit-files/{file_id}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-disposition"], f'inline; filename="{file_id}"')
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.content, b"image-bytes")

    def test_webui_status_reports_cron_health(self) -> None:
        """Dashboard status should report fresh and missing cron heartbeats."""
        task = Task_agent(id="task-1", payload={"model": "qwen3.5:9b"}, stream=False)
        core = _FakeCore(task)
        core.config.get.side_effect = lambda key, default=None: {
            "instance": "aidir",
            "webui.health.cron_max_age": 180,
        }.get(key, default)

        with patch("webui.backend.app._get_session", return_value={"permissions": ["all"], "login": "tester"}):
            client = TestClient(create_app(core=core))

            core.redis.get = AsyncMock(return_value=str(time.time() - 30))
            healthy_response = client.get("/api/status")
            self.assertEqual(healthy_response.status_code, 200)
            self.assertTrue(healthy_response.json()["health"]["cron"]["healthy"])

            core.redis.get = AsyncMock(return_value=None)
            stale_response = client.get("/api/status")
            self.assertEqual(stale_response.status_code, 200)
            self.assertFalse(stale_response.json()["health"]["cron"]["healthy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)