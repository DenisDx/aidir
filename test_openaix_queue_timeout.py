"""Regression tests for OpenAIx per-request queue timeout handling."""
from __future__ import annotations

import json
import unittest

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.task import STATUS_FAILED
from core.task_types.task_agent import Task_agent


class _Config:
    """Minimal configuration accessor for endpoint task construction."""

    def get(self, key: str, default=None):
        """Return configured task timeouts for the requested dotted key."""
        if key == "tasks":
            return {"queue_timeout": 300, "run_timeout": 600}
        return default


class _Core:
    """Minimal core exposing endpoint configuration."""

    config = _Config()


class TestOpenAIxQueueTimeout(unittest.TestCase):
    """Validate request queue timeout precedence over configuration."""

    def setUp(self) -> None:
        """Create an endpoint with predictable task timeout defaults."""
        self.endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        self.endpoint._core = _Core()

    def test_request_queue_timeout_overrides_config_and_is_not_forwarded(self) -> None:
        """Uses request queue_timeout while keeping it out of model payload."""
        payload = self.endpoint._openai_request_to_ollama({
            "model": "model",
            "messages": [],
            "queue_timeout": 12,
        })
        task = self.endpoint._create_task_for_payload(payload, False, "openaix", None)

        self.assertEqual(task.queue_timeout, 12)
        self.assertEqual(task.run_timeout, 600)
        self.assertNotIn("queue_timeout", task.payload)

    def test_missing_request_queue_timeout_uses_config_default(self) -> None:
        """Keeps the configured queue timeout when the request omits it."""
        task = self.endpoint._create_task_for_payload(
            {"model": "model", "messages": []},
            False,
            "openaix",
            None,
        )

        self.assertEqual(task.queue_timeout, 300)

    def test_request_timeout_overrides_run_and_upstream_timeouts(self) -> None:
        """Persists an explicit request timeout for execution and the LLM call."""
        task = self.endpoint._create_task_for_payload(
            {"model": "model", "messages": [], "timeout": 1200},
            False,
            "openaix",
            None,
        )

        self.assertEqual(task.queue_timeout, 1200)
        self.assertEqual(task.run_timeout, 1200)
        self.assertEqual(task.config["request_timeout"], 1200)
        self.assertNotIn("timeout", task.payload)

    def test_request_timeout_preserves_configured_queue_headroom(self) -> None:
        """Extends the total queue deadline by the configured queue/run gap."""
        class _HeadroomConfig:
            """Configuration with a 300-second queue headroom."""

            @staticmethod
            def get(key: str, default=None):
                """Return test task timeout defaults."""
                if key == "tasks":
                    return {"queue_timeout": 1200, "run_timeout": 900}
                return default

        self.endpoint._core.config = _HeadroomConfig()
        task = self.endpoint._create_task_for_payload(
            {"model": "model", "messages": [], "timeout": 1800},
            False,
            "openaix",
            None,
        )

        self.assertEqual(task.run_timeout, 1800)
        self.assertEqual(task.queue_timeout, 2100)

    def test_queue_timeout_overrides_request_timeout_for_queue_only(self) -> None:
        """Keeps a more specific queue timeout while timeout controls execution."""
        task = self.endpoint._create_task_for_payload(
            {"model": "model", "messages": [], "timeout": 1200, "queue_timeout": 15},
            False,
            "openaix",
            None,
        )

        self.assertEqual(task.queue_timeout, 15)
        self.assertEqual(task.run_timeout, 1200)

    def test_invalid_request_queue_timeout_is_rejected(self) -> None:
        """Rejects negative, fractional, boolean, and text timeout values."""
        for value in (-1, 1.5, True, "invalid"):
            with self.subTest(value=value), self.assertRaises(HTTPException) as raised:
                self.endpoint._resolve_queue_timeout(value, 300)
            self.assertEqual(raised.exception.status_code, 400)


class TestOpenAIxStreamingTimeout(unittest.IsolatedAsyncioTestCase):
    """Validate HTTP error handling before OpenAI SSE output starts."""

    async def test_upstream_timeout_before_first_chunk_returns_http_504(self) -> None:
        """Returns an error response instead of opening an empty SSE stream."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})

        class _Core:
            """Minimal core that accepts deferred task cleanup."""

            @staticmethod
            async def delete_task(task_id: str) -> None:
                """Accept endpoint cleanup for the failed task."""

        endpoint._core = _Core()
        task = Task_agent(payload={"model": "model", "messages": []}, stream=True)
        task.status = STATUS_FAILED
        task.error = {"code": "UPSTREAM_TIMEOUT", "message": "ReadTimeout"}
        task._done_event.set()
        await task._chunk_queue.put(None)

        response = await endpoint._openai_streaming_response(task, {"model": "model"})

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(json.loads(response.body)["error"]["code"], "upstream_timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)