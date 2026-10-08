"""Regression tests for bounded Task Viewer search."""
from __future__ import annotations

import json
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
        self.audit_log = None
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

    def test_dashboard_lists_persisted_running_task_absent_from_memory(self):
        """Show a running task even when it is owned by another process."""
        task_id = "remote-running-task"
        redis = _Redis({
            f"aidir:task:{task_id}": {
                **_summary(task_id, "2026-10-01T12:00:00+00:00", "remote-model"),
                "status": "running",
                "started_at": "2026-10-01T12:00:01+00:00",
            },
        })
        core = _Core(redis)
        core.queue.list_tasks.return_value = []

        async def session(*args, **kwargs):
            """Provide an authenticated Dashboard session."""
            return {"permissions": ["all"], "login": "test"}

        with patch("webui.backend.app._get_session", session):
            response = TestClient(create_app(core)).get("/api/tasks")

        self.assertEqual(response.status_code, 200)
        tasks = response.json()["tasks"]
        self.assertEqual([task["id"] for task in tasks], [task_id])
        self.assertEqual(tasks[0]["status"], "running")
        self.assertEqual(tasks[0]["model_id"], "remote-model")

    def test_search_uses_batched_hashes_and_bounded_results(self):
        """Return top-k rows without requiring valid heavy Redis task fields."""
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

    def test_search_decodes_endpoint_request_previews(self):
        """Decode bounded previews for Ollama, OpenAI parts, embeddings, and MCP."""
        timestamp = "2026-10-01T12:00:00+00:00"
        cases = {
            "ollama": (
                {
                    "model": "ollama-model",
                    "messages": [{"role": "user", "content": "Ollama question"}],
                },
                "Ollama question",
            ),
            "openai": (
                {
                    "model": "openai-model",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,secret"}},
                            {"type": "text", "text": "OpenAI text part"},
                        ],
                    }],
                },
                "OpenAI text part",
            ),
            "embedding": (
                {
                    "model": "embedding-model",
                    "input": ["First embedding input", "Second embedding input"],
                },
                "First embedding input",
            ),
            "mcp": (
                {
                    "tool": "echo",
                    "arguments": {"value": "MCP argument"},
                },
                'echo {"value":"MCP argument"}',
            ),
            "bounded": (
                {
                    "model": "bounded-model",
                    "messages": [{"role": "user", "content": "x" * 1200}],
                },
                f'{"x" * 999}…',
            ),
        }
        hashes = {}
        for task_id, (payload, _) in cases.items():
            task_hash = _summary(task_id, timestamp, str(payload.get("model") or ""))
            task_hash["payload"] = json.dumps(payload)
            if task_id == "mcp":
                task_hash["type"] = "tool"
            hashes[f"aidir:task:{task_id}"] = task_hash

        redis = _Redis(hashes)
        core = _Core(redis)

        async def session(*args, **kwargs):
            """Provide an authenticated Viewer session."""
            return {"permissions": ["all"], "login": "test"}

        with patch("webui.backend.app._get_session", session):
            response = TestClient(create_app(core)).get("/api/tasks/viewer/search")

        self.assertEqual(response.status_code, 200)
        tasks = {task["id"]: task for task in response.json()["tasks"]}
        for task_id, (_, expected_preview) in cases.items():
            self.assertEqual(tasks[task_id]["first_message_preview"], expected_preview)
        self.assertNotIn("data:image", tasks["openai"]["first_message_preview"])

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

    def test_search_filters_by_recent_audited_model_and_endpoint(self):
        """Match tasks by a route from the bounded recent-request audit map."""
        llama_route = "/v1/chat/completions\x1fllama"
        hashes = {
            "aidir:task:llama": _summary("llama", "2026-10-01T12:00:00+00:00", "llama"),
            "aidir:task:embed": _summary("embed", "2026-10-01T12:01:00+00:00", "embed"),
        }
        redis = _Redis(hashes)
        core = _Core(redis)
        core.audit_log = MagicMock()
        core.audit_log.recent_client_request_routes.return_value = (
            [{"value": llama_route, "label": "llama | /v1/chat/completions", "count": 1}],
            {"llama": llama_route},
        )

        async def session(*args, **kwargs):
            """Provide an authenticated Viewer session."""
            return {"permissions": ["all"], "login": "test"}

        with patch("webui.backend.app._get_session", session):
            response = TestClient(create_app(core)).get(
                "/api/tasks/viewer/search",
                params=[("route", llama_route)],
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([task["id"] for task in response.json()["tasks"]], ["llama"])
