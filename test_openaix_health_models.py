"""Regression tests for OpenAIx health, model detail and queue-state routes."""

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
        return 1


class _Task:
    """Provide the fields read by the in-memory task summaries."""

    def __init__(self, status: str, provider: str, model: str, requested: str, priority: int = 5) -> None:
        self.status = status
        self.priority = priority
        self.route_provider_id = provider
        self.model_id = model
        self.config = {"route": {
            "resolved_provider": provider,
            "resolved_model": model,
            "requested_model": requested,
        }}


def _core() -> SimpleNamespace:
    """Build a core double with one resource, one concrete model and one smart model."""
    resources = Resources([{
        "id": "gpu",
        "type": "cuda",
        "limits": {"VRAM": 1000},
        "units": {"VRAM": "MiB"},
        "alive_time": 300,
        "availability": {"command": "true", "metric": "VRAM"},
    }])
    resources.get("gpu").add_soft_consumer(
        {"VRAM": 100},
        "org/model",
        "local",
        persistent=True,
    )
    config = _Config({
        "instance": "test-aidir",
        "http": {"max_request_size": 1024},
        "routing": {"kv_affinity": {"enabled": True}},
        "models": {"providers": {
            "local": {
                "api": "llama-cpp",
                "baseUrl": "http://127.0.0.1:8888",
                "models": [{
                    "id": "org/model",
                    "alias": "org/model",
                    "contextWindow": 4096,
                    "resources": {"gpu": {"VRAM": 500}},
                }],
            },
            "smart": {
                "api": "smart",
                "models": [
                    {
                        "id": "smart-one",
                        "alias": "smart-one",
                        "items": [],
                    },
                    {
                        "id": "smart-bound",
                        "alias": "smart-bound",
                        "items": [{
                            "provider": "local",
                            "model": "org/model",
                            "fallback_prio": 10,
                        }],
                    },
                ],
            },
            "remote": {
                "api": "openaix",
                "baseUrl": "http://192.168.1.40:21434",
                "models": [{"id": "remote-model"}],
            },
        }},
    })
    return SimpleNamespace(
        config=config,
        queue=SimpleNamespace(list_tasks=lambda: [
            _Task(STATUS_QUEUED, "local", "org/model", "org/model"),
            _Task(STATUS_RUNNING, "local", "org/model", "org/model"),
        ]),
        scheduler=_Scheduler(),
        workers={"openaix": SimpleNamespace(enabled=True)},
        resources=resources,
        started_at=1000.0,
    )


class TestOpenaixHealthAndModels(unittest.TestCase):
    """Verify the health and model state contracts."""

    def _client(self) -> TestClient:
        """Create a test client for the OpenAIx endpoint."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        return TestClient(endpoint.create_app(_core()))

    def test_health_lists_resources_models_and_telemetry(self) -> None:
        """Health reports resources with loaded models plus the resource-bound model list."""
        with self._client() as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["resources"][0]["id"], "gpu")
        self.assertEqual(payload["resources"][0]["loaded_models"][0]["id"], "org/model")
        self.assertEqual(payload["resources"][0]["used"], {"VRAM": 100})
        self.assertEqual(payload["resources"][0]["active_used"], {"VRAM": 0})
        self.assertEqual(payload["resources"][0]["soft_used"], {"VRAM": 100})
        self.assertEqual(payload["models"][0]["id"], "org/model")
        smart = next(item for item in payload["models"] if item["id"] == "smart-bound")
        self.assertEqual(smart["resolved_model"], "org/model")
        self.assertEqual(len(payload["tasks"]), 2)
        self.assertIn("telemetry", payload)
        remote = next(item for item in payload["external_providers"] if item["provider"] == "remote")
        self.assertEqual(remote["host"], "192.168.1.40")

    def test_health_tasks_is_an_empty_array_without_tasks(self) -> None:
        """Health always contains the active-task list, empty when idle."""
        core = _core()
        core.queue = SimpleNamespace(list_tasks=lambda: [])
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        with TestClient(endpoint.create_app(core)) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tasks"], [])

    def test_model_detail_uses_path_converter_for_slashes(self) -> None:
        """A model id containing a slash is reachable when encoded or passed as a path."""
        with self._client() as client:
            encoded = client.get("/v1/models/org%2Fmodel")
            plain = client.get("/v1/models/org/model")

        self.assertEqual(encoded.status_code, 200)
        self.assertEqual(plain.status_code, 200)
        payload = encoded.json()
        self.assertEqual(payload["id"], "org/model")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["aidir"]["state"]["model_loaded"], True)
        self.assertEqual(payload["aidir"]["resources"][0]["id"], "gpu")
        self.assertEqual(payload["aidir"]["resources"][0]["needed"], {"VRAM": 500})

    def test_unknown_model_is_404(self) -> None:
        """An unknown model id stays a 404."""
        with self._client() as client:
            response = client.get("/v1/models/does-not-exist")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "model_not_found")

    def test_smart_model_reports_fallback_route(self) -> None:
        """A smart model exposes whether context identification exists and its fallback route."""
        with self._client() as client:
            response = client.get("/v1/models/smart-one")

        self.assertEqual(response.status_code, 200)
        smart = response.json()["aidir"]["smart"]
        self.assertTrue(smart["context_identification"])
        self.assertIn("fallback_route", smart)

    def test_loaded_smart_model_is_ready_and_runnable(self) -> None:
        """A loaded model reports ready even when admission says base memory is missing."""
        with self._client() as client:
            response = client.get("/v1/models/smart-bound")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["aidir"]
        self.assertTrue(payload["state"]["model_loaded"])
        self.assertTrue(payload["state"]["resource_ready"])
        self.assertTrue(payload["state"]["can_run_now"])
        self.assertEqual(payload["state"]["admission"], "ready")
        self.assertEqual(payload["resolved_model"], "org/model")

    def test_queue_state_for_model_with_slash_is_not_500(self) -> None:
        """Queue state for an encoded model id answers 200 with a schema version."""
        with self._client() as client:
            response = client.get("/v1/models/org%2Fmodel/queue-state")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["provider"], "local")
        self.assertEqual(payload["model"], "org/model")
        self.assertEqual(payload["queued_count_total"], 1)

    def test_queue_state_for_unresolvable_smart_model_is_not_500(self) -> None:
        """A smart model with no schedulable candidate answers queue state instead of 500."""
        with self._client() as client:
            response = client.get("/v1/models/smart-one/queue-state")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["provider"], "smart")
        self.assertEqual(payload["requested_model"], "smart-one")


if __name__ == "__main__":
    unittest.main()
