"""Regression tests for bounded, summary-only Task Viewer search."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from webui.backend.app import create_app


class _Pipeline:
    """Collect batched hash reads for the viewer search fake."""

    def __init__(self, redis):
        """Keep the Redis fake and requested keys."""
        self.redis = redis
        self.keys = []

    def hgetall(self, key):
        """Queue one hash read."""
        self.keys.append(key)
        return self

    async def execute(self):
        """Return all requested hash values together."""
        self.redis.executed_batches.append(list(self.keys))
        return [dict(self.redis.hashes.get(key, {})) for key in self.keys]


class _Redis:
    """Redis fake that exposes one SCAN page and pipeline reads."""

    def __init__(self, hashes):
        """Store task hashes keyed by Redis key name."""
        self.hashes = hashes
        self.scan_calls = 0
        self.executed_batches = []

    async def scan(self, cursor, match, count):
        """Return all seeded task keys in one bounded page."""
        self.scan_calls += 1
        return 0, list(self.hashes)

    def pipeline(self, transaction=False):
        """Create a single batched hash-read pipeline."""
        return _Pipeline(self)


class _Core:
    """Minimal Core surface needed by the Viewer route."""

    def __init__(self, redis):
        """Initialize request dependencies with the supplied Redis fake."""
        self.config = MagicMock()
        self.config.get.side_effect = lambda key, default=None: "aidir" if key == "instance" else default
        self.redis = redis
        self.queue = MagicMock()
        self.queue.get_task.return_value = None
        self.workers = {}
        self.envid_registry = None


def _summary(task_id: str, timestamp: str, model: str) -> dict[str, str]:
    """Build one task hash with malformed heavy fields that search must not decode."""
    return {
        "id": task_id,
        "type": "agent",
        "status": "completed",
        "worker_id": "call_ollama",
        "created_at": timestamp,
        "updated_at": timestamp,
        "finished_at": timestamp,
        "priority": "5",
        "llm_call_count": "1",
        "queue_timeout": "300",
        "run_timeout": "300",
        "external": "1",
        "request_preview": f"type=agent model={model} messages=1",
        "route_provider_id": "local",
        "model_id": model,
        "envid": "test-envid",
        "error_code": "",
        "payload": "not-json",
        "result": "not-json",
        "config": "not-json",
        "context": "not-json",
        "llm_call_history": "not-json",
    }


class ViewerSearchTests(unittest.TestCase):
    """Verify the Viewer search contract against summary-only Redis hashes."""

    def test_search_uses_batched_summary_hashes_only(self):
        """Return top-k summary rows without decoding any heavy Redis task field."""
        hashes = {
            "aidir:task:old": _summary("old", "2026-10-01T10:00:00+00:00", "old-model"),
            "aidir:task:middle": _summary("middle", "2026-10-01T11:00:00+00:00", "middle-model"),
            "aidir:task:new": _summary("new", "2026-10-01T12:00:00+00:00", "new-model"),
        }
        redis = _Redis(hashes)
        core = _Core(redis)

        async def session(*args, **kwargs):
            """Provide an authenticated Viewer session."""
            return {"permissions": ["all"], "login": "test"}

        with patch("webui.backend.app._get_session", session):
            response = TestClient(create_app(core)).get("/api/tasks/viewer/search?limit=2&envid=test-envid")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual([task["id"] for task in body["tasks"]], ["new", "middle"])
        self.assertEqual(body["tasks"][0]["request_preview"], "type=agent model=new-model messages=1")
        self.assertEqual(body["tasks"][0]["model_id"], "new-model")
        self.assertEqual(redis.scan_calls, 1)
        self.assertEqual(redis.executed_batches, [list(hashes)])

    def test_search_prefers_live_status_over_stale_redis_status(self):
        """Expose an active task's live status while its Redis hash is stale."""
        task_id = "active-task"
        hashes = {
            f"aidir:task:{task_id}": {
                **_summary(task_id, "2026-10-01T12:00:00+00:00", "live-model"),
                "status": "failed",
                "error_code": "UPSTREAM_TIMEOUT",
            },
        }
        redis = _Redis(hashes)
        core = _Core(redis)
        live_task = MagicMock()
        live_task.to_redis_hash.return_value = {
            **hashes[f"aidir:task:{task_id}"],
            "status": "running",
            "error_code": "",
            "updated_at": "2026-10-01T12:00:01+00:00",
            "started_at": "2026-10-01T12:00:01+00:00",
            "finished_at": "",
        }
        core.queue.get_task.side_effect = lambda candidate_id: live_task if candidate_id == task_id else None

        async def session(*args, **kwargs):
            """Provide an authenticated Viewer session."""
            return {"permissions": ["all"], "login": "test"}

        with patch("webui.backend.app._get_session", session):
            response = TestClient(create_app(core)).get("/api/tasks/viewer/search")

        self.assertEqual(response.status_code, 200)
        task = response.json()["tasks"][0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["error_code"], "")

    def test_search_exposes_persisted_error_message(self):
        """Return a bounded failure explanation with the task summary."""
        task_id = "failed-task"
        hashes = {
            f"aidir:task:{task_id}": {
                **_summary(task_id, "2026-10-01T12:00:00+00:00", "failed-model"),
                "status": "failed",
                "error_code": "EXCEPTION",
                "error": '{"code":"EXCEPTION","message":"Server disconnected without sending a response."}',
            },
        }
        redis = _Redis(hashes)
        core = _Core(redis)

        async def session(*args, **kwargs):
            """Provide an authenticated Viewer session."""
            return {"permissions": ["all"], "login": "test"}

        with patch("webui.backend.app._get_session", session):
            response = TestClient(create_app(core)).get("/api/tasks/viewer/search")

        self.assertEqual(response.status_code, 200)
        task = response.json()["tasks"][0]
        self.assertEqual(task["error_code"], "EXCEPTION")
        self.assertEqual(task["error_details"], "Server disconnected without sending a response.")

    def test_search_hides_stale_error_message_for_completed_task(self):
        """Do not expose stale failure data once a task is completed."""
        task_id = "completed-task"
        hashes = {
            f"aidir:task:{task_id}": {
                **_summary(task_id, "2026-10-01T12:00:00+00:00", "completed-model"),
                "error_code": "SERVICE_RESTARTED",
                "error": '{"code":"SERVICE_RESTARTED","message":"Task interrupted by service restart"}',
            },
        }
        redis = _Redis(hashes)
        core = _Core(redis)

        async def session(*args, **kwargs):
            """Provide an authenticated Viewer session."""
            return {"permissions": ["all"], "login": "test"}

        with patch("webui.backend.app._get_session", session):
            response = TestClient(create_app(core)).get("/api/tasks/viewer/search")

        self.assertEqual(response.status_code, 200)
        task = response.json()["tasks"][0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["error_details"], "")
