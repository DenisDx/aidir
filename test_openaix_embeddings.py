"""Regression tests for OpenAIx embedding endpoint helpers."""
from __future__ import annotations

import base64
import json
import struct
import unittest
from unittest.mock import patch

from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.smart_router import SmartRouteError
from core.task_types.task_agent import Task_agent
from core.worker import WorkerResult
from core.smart_router import SmartRouter
from workers.agent.call_ollama.app import CallOllamaWorker
from workers.agent.openaix.app import OpenAIxWorker


class _Config:
    """Minimal dotted configuration accessor for endpoint tests."""

    def __init__(self, data: dict) -> None:
        """Store configuration data for lookups."""
        self._data = data

    def get(self, key: str, default=None):
        """Return a dotted configuration value or the supplied default."""
        current = self._data
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


class _Core:
    """Minimal core for endpoint task construction."""

    def __init__(self, data: dict) -> None:
        """Expose the supplied configuration through the expected core shape."""
        self.config = _Config(data)


class TestOpenAIxEmbeddings(unittest.TestCase):
    """Verify protocol-neutral embedding request and response behavior."""

    def setUp(self) -> None:
        """Create an endpoint with one embedding-capable and one chat-only model."""
        self.endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        self.endpoint._core = _Core(
            {
                "tasks": {"queue_timeout": 300, "run_timeout": 600},
                "models": {
                    "providers": {
                        "remote": {
                            "api": "ollama",
                            "models": [{"id": "embed-model", "embedding": True}],
                        },
                        "chat": {
                            "api": "ollama",
                            "models": [{"id": "chat-model"}],
                        },
                    }
                },
            }
        )

    def test_embed_validation_accepts_text_and_string_lists(self) -> None:
        """Accept the intentionally small first-version input surface."""
        self.assertIsNone(self.endpoint._validate_embed_request({"model": "embed-model", "input": "text"}, protocol="ollama"))
        self.assertIsNone(self.endpoint._validate_embed_request({"model": "embed-model", "input": ["one", "two"]}, protocol="openai"))

    def test_embed_validation_rejects_non_text_input_and_streaming(self) -> None:
        """Reject token arrays, mixed inputs, and streaming requests."""
        for body in (
            {"model": "embed-model", "input": [1, 2]},
            {"model": "embed-model", "input": ["text", 2]},
            {"model": "embed-model", "input": "text", "stream": True},
        ):
            with self.subTest(body=body):
                response = self.endpoint._validate_embed_request(body, protocol="openai")
                self.assertEqual(response.status_code, 400)

    def test_embedding_task_is_marked_and_capability_checked(self) -> None:
        """Store embed intent on the normal task and reject chat-only models."""
        route = {
            "requested_model": "embed-model",
            "resolved_provider": "remote",
            "resolved_model": "embed-model",
        }
        self.endpoint._ensure_embedding_route(route)
        task = self.endpoint._create_task_for_payload(
            {"model": "embed-model", "input": "text"},
            False,
            "openaix",
            route,
            request_kind="embed",
        )
        self.assertEqual(task.config["request_kind"], "embed")
        self.assertEqual(task.payload["model"], "embed-model")

        with self.assertRaises(SmartRouteError) as raised:
            self.endpoint._ensure_embedding_route(
                {
                    "requested_model": "chat-model",
                    "resolved_provider": "chat",
                    "resolved_model": "chat-model",
                }
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_openai_embed_response_supports_float_and_base64(self) -> None:
        """Serialize ordered Ollama vectors in both supported OpenAI encodings."""
        result = {"model": "embed-model", "embeddings": [[1.0, -2.0]], "prompt_eval_count": 3}
        float_response = self.endpoint._ollama_embed_to_openai(result, "task-1", {"model": "public-model"})
        self.assertEqual(float_response["data"][0]["embedding"], [1.0, -2.0])
        self.assertEqual(float_response["usage"], {"prompt_tokens": 3, "total_tokens": 3})

        base64_response = self.endpoint._ollama_embed_to_openai(
            result,
            "task-1",
            {"model": "public-model", "encoding_format": "base64"},
        )
        self.assertEqual(
            base64.b64decode(base64_response["data"][0]["embedding"]),
            struct.pack("<2f", 1.0, -2.0),
        )
        self.assertEqual(json.loads(json.dumps(base64_response))["model"], "public-model")


class _AsyncClient:
    """No-op async HTTP client used when worker forwarding is intercepted."""

    async def __aenter__(self):
        """Return the test client instance."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """Complete the async context without suppressing errors."""
        return False


class TestEmbeddingWorkers(unittest.IsolatedAsyncioTestCase):
    """Verify that every Ollama-capable worker uses the embed upstream contract."""

    async def test_openaix_worker_uses_embed_url_and_strips_local_fields(self) -> None:
        """Forward an embedding task without context or OpenAI-only request fields."""
        worker = OpenAIxWorker()
        captured: dict = {}

        async def fake_forward(client, url, payload, *, task=None, save_call=False, task_id=""):
            captured.update(url=url, payload=payload, task=task)
            return WorkerResult(ok=True, data={"embeddings": [[1.0]]})

        worker._forward_sync = fake_forward
        task = Task_agent(
            payload={
                "model": "embed-model",
                "input": ["first", "second"],
                "dimensions": 256,
                "encoding_format": "base64",
                "worker": "openaix",
                "queue_timeout": 10,
                "tools": [],
            },
            stream=False,
        )
        task.config = {"request_kind": "embed"}

        with patch("workers.agent.openaix.app.httpx.AsyncClient", return_value=_AsyncClient()):
            result = await worker.execute(task)

        self.assertTrue(result.ok)
        self.assertEqual(captured["url"], "http://127.0.0.1:11434/api/embed")
        self.assertEqual(captured["payload"], {"model": "embed-model", "input": ["first", "second"], "dimensions": 256})

    async def test_call_ollama_worker_uses_embed_url_and_strips_local_fields(self) -> None:
        """Forward an embedding task through the plain Ollama worker contract."""
        worker = CallOllamaWorker()
        captured: dict = {}

        async def fake_forward(client, url, payload, task, save_call=False):
            captured.update(url=url, payload=payload, task=task)
            return WorkerResult(ok=True, data={"embeddings": [[1.0]]})

        worker._forward_sync = fake_forward
        task = Task_agent(
            payload={
                "model": "embed-model",
                "input": "first",
                "truncate": True,
                "user": "caller-id",
                "context_builder": {},
            },
            stream=False,
        )
        task.config = {"request_kind": "embed"}

        with patch("workers.agent.call_ollama.app.httpx.AsyncClient", return_value=_AsyncClient()):
            result = await worker.execute(task)

        self.assertTrue(result.ok)
        self.assertEqual(captured["url"], "http://127.0.0.1:11434/api/embed")
        self.assertEqual(captured["payload"], {"model": "embed-model", "input": "first", "truncate": True})


class TestEmbeddingSmartRouting(unittest.IsolatedAsyncioTestCase):
    """Verify capability filtering before smart-route availability probes."""

    async def test_disallowed_embedding_candidate_is_not_routing_eligible(self) -> None:
        """Skip a candidate without invoking resource or remote availability probes."""
        router = SmartRouter(
            endpoint_id="test",
            default_worker_id="openaix",
            find_provider_model_cfg=lambda provider, model: {},
            provider_api=lambda provider: "ollama",
            resolve_model_resource_requirements=lambda provider, model: {},
            get_local_queue_state=None,
            check_resource_available=None,
            check_resource_available_after_unload=None,
            probe_remote_model_queue_state=lambda *args, **kwargs: None,
            probe_ollama_model_availability=None,
            resolve_probe_timeout_ms=lambda item: 100,
            is_candidate_allowed=lambda provider, model: False,
        )

        candidate = await router.evaluate_candidate(
            {"provider": "chat-only", "model": "chat-model"},
            request_priority=5,
            index=0,
        )

        self.assertFalse(candidate["routing_eligible"])
        self.assertEqual(candidate["probe_error"], "candidate_not_allowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)