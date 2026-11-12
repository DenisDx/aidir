"""Regression tests for bounded Task Viewer search."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from webui.backend.app import create_app


class _Pipeline:
    """Collect batched hash reads for the viewer search fake."""

    def __init__(self, redis):
        """Keep the Redis fake and requested keys."""
        self.redis = redis
        self.keys = []
        self.fields = []

    def hgetall(self, key):
        """Queue one full hash read."""
        self.keys.append(key)
        self.fields.append(None)
        return self

    def hmget(self, key, *fields):
        """Queue one selected-field hash read."""
        self.keys.append(key)
        self.fields.append(tuple(fields))
        return self

    async def execute(self):
        """Return all requested hash values together."""
        self.redis.executed_batches.append(list(self.keys))
        self.redis.requested_fields.extend(self.fields)
        values = []
        for key, fields in zip(self.keys, self.fields):
            task_hash = self.redis.hashes.get(key, {})
            if fields is None:
                values.append(dict(task_hash))
            else:
                values.append([task_hash.get(field) for field in fields])
        return values


class _Redis:
    """Redis fake that exposes one SCAN page and pipeline reads."""

    def __init__(self, hashes):
        """Store task hashes keyed by Redis key name."""
        self.hashes = hashes
        self.scan_calls = 0
        self.executed_batches = []
        self.requested_fields = []

    async def scan(self, cursor, match, count):
        """Return all seeded task keys in one bounded page."""
        self.scan_calls += 1
        return 0, list(self.hashes)

    async def hgetall(self, key):
        """Return one seeded task hash."""
        return dict(self.hashes.get(key, {}))

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

    def test_dashboard_keeps_recently_finished_tasks_visible(self):
        """A task that just completed stays on the dashboard for a few polls."""
        task_id = "recent-done"
        finished = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat()
        redis = _Redis({
            f"aidir:task:{task_id}": {
                **_summary(task_id, finished, "local-model"),
                "status": "completed",
                "finished_at": finished,
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
        self.assertEqual(tasks[0]["status"], "completed")

    def test_dashboard_drops_finished_tasks_after_the_grace_window(self):
        """An old terminal task is not kept on the active task list."""
        task_id = "old-done"
        finished = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        redis = _Redis({
            f"aidir:task:{task_id}": {
                **_summary(task_id, finished, "local-model"),
                "status": "completed",
                "finished_at": finished,
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
        self.assertEqual(response.json()["tasks"], [])

    def test_dashboard_exposes_resolved_smart_route_model(self):
        """Show the concrete model, requested alias, and provider for a smart task."""
        task_id = "smart-task"
        redis = _Redis({
            f"aidir:task:{task_id}": {
                **_summary(task_id, "2026-10-01T12:00:00+00:00", ""),
                "status": "running",
                "model_id": "",
                "route_provider_id": "",
                "config": json.dumps({
                    "route": {
                        "selection": "smart_route",
                        "requested_alias": "smart_gemma-4-26B",
                        "requested_model": "smart_gemma-4-26B",
                        "resolved_provider": "ollama_local",
                        "resolved_model": "juilpark/gemma-4-26B-A4B-it-heretic:q4_k_m",
                    },
                }),
            },
        })
        core = _Core(redis)
        core.queue.list_tasks.return_value = []

        async def session(*args, **kwargs):
            """Provide an authenticated Dashboard session."""
            return {"permissions": ["all"], "login": "test"}

        async def no_peers(core):
            """Return no peer task rows for this local-only case."""
            return []

        with patch("webui.backend.app._get_session", session), \
             patch("webui.backend.app._fetch_peer_tasks", no_peers):
            response = TestClient(create_app(core)).get("/api/tasks")

        self.assertEqual(response.status_code, 200)
        task = response.json()["tasks"][0]
        self.assertEqual(task["model_id"], "juilpark/gemma-4-26B-A4B-it-heretic:q4_k_m")
        self.assertEqual(task["requested_model"], "smart_gemma-4-26B")
        self.assertEqual(task["route_provider_id"], "ollama_local")
        self.assertEqual(task["route_selection"], "smart_route")
        self.assertFalse(task["remote"])

    def test_dashboard_merges_remote_peer_tasks_as_read_only(self):
        """Include active tasks published by another aidir and mark them remote."""
        task_id = "local-task"
        redis = _Redis({
            f"aidir:task:{task_id}": {
                **_summary(task_id, "2026-10-01T12:00:00+00:00", "local-model"),
                "status": "queued",
            },
        })
        core = _Core(redis)
        core.queue.list_tasks.return_value = []

        async def session(*args, **kwargs):
            """Provide an authenticated Dashboard session."""
            return {"permissions": ["all"], "login": "test"}

        async def peers(core):
            """Return one active task from a remote instance."""
            return [{
                "id": "peer-task",
                "type": "agent",
                "status": "running",
                "worker_id": "openaix",
                "model_id": "remote-model",
                "requested_model": "smart_remote",
                "route_provider_id": "remote_aidir_110",
                "created_at": "2026-10-01T12:01:00+00:00",
                "updated_at": "2026-10-01T12:01:30+00:00",
                "last_operation_at": "2026-10-01T12:01:30+00:00",
                "instance": "aidir110",
                "remote": True,
                "source_provider": "remote_aidir_110",
            }]

        with patch("webui.backend.app._get_session", session), \
             patch("webui.backend.app._fetch_peer_tasks", peers):
            response = TestClient(create_app(core)).get("/api/tasks")

        self.assertEqual(response.status_code, 200)
        tasks = {task["id"]: task for task in response.json()["tasks"]}
        self.assertEqual(set(tasks), {"local-task", "peer-task"})
        self.assertTrue(tasks["peer-task"]["remote"])
        self.assertEqual(tasks["peer-task"]["instance"], "aidir110")
        self.assertEqual(tasks["peer-task"]["model_id"], "remote-model")
        self.assertFalse(tasks["local-task"]["remote"])

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
        self.assertEqual(redis.executed_batches[0], list(hashes))
        self.assertEqual(set(redis.executed_batches[1]), {"aidir:task:new", "aidir:task:middle"})
        summary_reads = redis.requested_fields[:len(hashes)]
        for fields in summary_reads:
            self.assertNotIn("payload", fields)
            self.assertNotIn("result", fields)
            self.assertNotIn("llm_call_history", fields)
        self.assertEqual(redis.requested_fields[len(hashes):], [("payload",), ("payload",)])

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

    def test_search_decodes_first_and_last_message_previews(self):
        """Show the first and last messages for a multi-message request."""
        timestamp = "2026-10-01T12:00:00+00:00"
        payload = {
            "model": "multi-model",
            "messages": [
                {"role": "system", "content": "System prompt"},
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "Some answer"},
                {"role": "user", "content": [{"type": "text", "text": "Last question"}]},
            ],
        }
        hashes = {
            "aidir:task:multi": {
                **_summary("multi", timestamp, "multi-model"),
                "payload": json.dumps(payload),
            },
            "aidir:task:single": {
                **_summary("single", timestamp, "multi-model"),
                "payload": json.dumps({
                    "model": "multi-model",
                    "messages": [{"role": "user", "content": "Only question"}],
                }),
            },
        }
        core = _Core(_Redis(hashes))

        async def session(*args, **kwargs):
            """Provide an authenticated Viewer session."""
            return {"permissions": ["all"], "login": "test"}

        with patch("webui.backend.app._get_session", session):
            response = TestClient(create_app(core)).get("/api/tasks/viewer/search")

        self.assertEqual(response.status_code, 200)
        tasks = {task["id"]: task for task in response.json()["tasks"]}
        self.assertEqual(tasks["multi"]["first_message_preview"], "System prompt")
        self.assertEqual(tasks["multi"]["last_message_preview"], "Last question")
        self.assertEqual(tasks["single"]["first_message_preview"], "Only question")
        self.assertNotIn("last_message_preview", tasks["single"])

    def test_dashboard_exposes_first_and_last_message_previews(self):
        """Decode first and last messages for the dashboard task list."""
        timestamp = "2026-10-01T12:00:00+00:00"
        payload = {
            "model": "dashboard-model",
            "messages": [
                {"role": "system", "content": "Dashboard first"},
                {"role": "user", "content": "Dashboard last"},
            ],
        }
        redis = _Redis({
            "aidir:task:dashboard": {
                **_summary("dashboard", timestamp, "dashboard-model"),
                "status": "running",
                "payload": json.dumps(payload),
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
        task = response.json()["tasks"][0]
        self.assertEqual(task["first_message_preview"], "Dashboard first")
        self.assertEqual(task["last_message_preview"], "Dashboard last")

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

    def test_request_falls_back_to_the_persisted_task_payload(self):
        """Show the stored request when no client-request audit event survived."""
        task_id = "payload-task"
        payload = {"messages": [{"role": "user", "content": "hello"}]}
        hashes = {
            f"aidir:task:{task_id}": {
                **_summary(task_id, "2026-10-01T12:00:00+00:00", "model"),
                "payload": json.dumps(payload),
            },
        }
        core = _Core(_Redis(hashes))

        async def session(*args, **kwargs):
            """Provide an authenticated Viewer session."""
            return {"permissions": ["all"], "login": "test"}

        with patch("webui.backend.app._get_session", session):
            response = TestClient(create_app(core)).get(
                f"/api/tasks/viewer/{task_id}/request"
            )

        self.assertEqual(response.status_code, 200)
        event = response.json()["event"]
        self.assertEqual(event["source"], "task_payload")
        self.assertEqual(event["type"], "client_request")
        self.assertEqual(event["data"], payload)

    def test_request_hydrates_a_file_backed_client_request(self):
        """Read an oversized request body from its audit file on demand."""
        with tempfile.TemporaryDirectory() as directory:
            task_id = "file-task"
            body = {"messages": [{"role": "user", "content": "large"}]}
            relative = Path("files") / "2026-10-01" / "body"
            path = Path(directory) / relative
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(body), encoding="utf-8")
            event = {
                "event_id": "event-1",
                "task_id": task_id,
                "type": "client_request",
                "body_file": {"relative_path": str(relative), "content_type": "application/json"},
            }
            core = _Core(_Redis({}))
            core.audit_log = MagicMock()
            core.audit_log.directory = Path(directory)
            core.audit_log.list_task_events.return_value = [
                {"event_id": "event-1", "type": "client_request"}
            ]
            core.audit_log.read_event.return_value = event

            async def session(*args, **kwargs):
                """Provide an authenticated Viewer session."""
                return {"permissions": ["all"], "login": "test"}

            with patch("webui.backend.app._get_session", session):
                response = TestClient(create_app(core)).get(
                    f"/api/tasks/viewer/{task_id}/request"
                )

        self.assertEqual(response.status_code, 200)
        hydrated = response.json()["event"]
        self.assertEqual(hydrated["data"], body)
        self.assertEqual(hydrated["data_encoding"], "json")
