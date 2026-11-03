"""Regression tests for OpenAIx /stats and /metrics routes."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.resources import Resources
from core.task import STATUS_QUEUED, STATUS_RUNNING


class _Config:
    """Provide dotted configuration lookup for endpoint tests."""

    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key: str, default=None):
        """Return a nested value or the supplied default."""
        value = self._values
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class _Scheduler:
    """Report a fixed count of executing tasks."""

    def active_task_count(self) -> int:
        """Return the number of active runs."""
        return 2


class _Core:
    """Provide the state read by the stats routes."""

    def __init__(self) -> None:
        self.config = _Config({"instance": "test-aidir", "http": {"max_request_size": 1024}})
        self.queue = SimpleNamespace(list_tasks=lambda: [
            SimpleNamespace(status=STATUS_QUEUED),
            SimpleNamespace(status=STATUS_QUEUED),
            SimpleNamespace(status=STATUS_RUNNING),
        ])
        self.scheduler = _Scheduler()
        self.workers = {
            "agent": SimpleNamespace(enabled=True),
            "tool": SimpleNamespace(enabled=False),
        }
        self.resources = Resources([])
        self.started_at = 1000.0


class TestOpenaixStats(unittest.TestCase):
    """Verify the compatibility stats and metrics routes."""

    def _client(self) -> TestClient:
        """Create a test client for the OpenAIx endpoint."""
        endpoint = Endpoint_openaix({"id": "openaix"})
        return TestClient(endpoint.create_app(_Core()))

    def test_stats_returns_service_snapshot(self) -> None:
        """Stats reports instance, task, worker and resource counters."""
        with self._client() as client:
            response = client.get("/stats")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "aidir.stats")
        self.assertEqual(payload["instance"], "test-aidir")
        self.assertEqual(payload["tasks"]["total"], 3)
        self.assertEqual(payload["tasks"]["active"], 2)
        self.assertEqual(payload["tasks"]["by_status"], {STATUS_QUEUED: 2, STATUS_RUNNING: 1})
        self.assertEqual(payload["workers"], {"total": 2, "enabled": 1})
        self.assertEqual(payload["resources"], {"total": 0})

    def test_metrics_returns_prometheus_text(self) -> None:
        """Metrics exposes the same counters in Prometheus exposition format."""
        with self._client() as client:
            response = client.get("/metrics")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        body = response.text
        self.assertIn("aidir_schema_version 1\n", body)
        self.assertIn("aidir_up 1\n", body)
        self.assertIn('aidir_tasks_total{status="queued"} 2\n', body)
        self.assertIn('aidir_tasks_total{status="running"} 1\n', body)
        self.assertIn("aidir_tasks_active 2\n", body)
        self.assertIn("aidir_workers_total 2\n", body)
        self.assertIn("aidir_workers_enabled 1\n", body)
        self.assertIn("aidir_resources_total 0\n", body)


if __name__ == "__main__":
    unittest.main()
