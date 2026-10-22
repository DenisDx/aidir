"""HTTP contract tests for the Anthropic Messages endpoint."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from core.endpoints.endpoint_anthropic import Endpoint_anthropic
from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.task import STATUS_COMPLETED, STATUS_FAILED
from core.task_types.task_agent import Task_agent
from core.upstream_response import UpstreamChunk


class _Config:
    """Dotted configuration accessor matching the production facade."""

    def __init__(self, data: dict) -> None:
        """Store the nested configuration document."""
        self._data = data

    def get(self, key: str, default=None):
        """Return a dotted value or the supplied default."""
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict):
                return default
            value = value.get(part)
            if value is None:
                return default
        return value


class _Core:
    """Minimal core used by the endpoint contract tests."""

    def __init__(self, *, stream_chunks=None, error=None, users=None, providers=None) -> None:
        data = {
            "instance": "test",
            "workers": {"default": "openaix", "items": {"openaix": {"enabled": True}}},
            "models": {
                "providers": providers or {
                    "local": {
                        "api": "openaix",
                        "models": [{"id": "real-model", "alias": "alias"}],
                    }
                }
            },
            "tasks": {"queue_timeout": 5, "run_timeout": 5},
            "users": {"items": users or []},
        }
        self.config = _Config(data)
        self.audit_log = None
        self.envid_registry = None
        self.redis = None
        self.resources = None
        self.queue = None
        self.delete_task = AsyncMock()
        self.tasks = []
        self._stream_chunks = stream_chunks
        self._error = error

    async def on_task_added(self, task) -> None:
        """Complete the queued task immediately or fail it with the configured error."""
        self.tasks.append(task)
        if self._error is not None:
            task.error = dict(self._error)
            task.status = STATUS_FAILED
            task._done_event.set()
            task._chunk_queue.put_nowait(None)
            return
        task.status = STATUS_COMPLETED
        task.result = {"message": {"role": "assistant", "content": "hello"}, "done_reason": "stop", "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
        if task.stream and self._stream_chunks is not None:
            for chunk in self._stream_chunks:
                task._chunk_queue.put_nowait(chunk)
        else:
            task._done_event.set()
        task._chunk_queue.put_nowait(None)


def _client(core: _Core) -> TestClient:
    """Create a TestClient for the endpoint with the supplied core."""
    endpoint = Endpoint_anthropic({"id": "anthropic", "api": "anthropic", "port": 21435})
    return TestClient(endpoint.create_app(core))


class AnthropicEndpointTests(unittest.IsolatedAsyncioTestCase):
    """Verify inbound Messages routing, conversion, auth, and errors."""

    def test_sync_request_and_response(self) -> None:
        """Return an Anthropic message built from the internal result."""
        client = _client(_Core())
        response = client.post(
            "/v1/messages",
            json={"model": "alias", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["content"][0], {"type": "text", "text": "hello"})
        self.assertEqual(payload["usage"], {"input_tokens": 2, "output_tokens": 1})

    def test_configured_task_timeouts_are_applied(self) -> None:
        """Apply tasks.queue_timeout and tasks.run_timeout to the queued task."""
        core = _Core()
        response = _client(core).post(
            "/v1/messages",
            json={"model": "alias", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(core.tasks[0].queue_timeout, 5)
        self.assertEqual(core.tasks[0].run_timeout, 5)

    def test_count_tokens_does_not_queue(self) -> None:
        """Estimate tokens directly and require no task."""
        core = _Core()
        client = _client(core)
        response = client.post(
            "/v1/messages/count_tokens",
            json={"model": "alias", "messages": [{"role": "user", "content": "12345678"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()["input_tokens"], 0)
        self.assertFalse(core.on_task_added.__self__.delete_task.await_count)

    def test_models_use_anthropic_shape(self) -> None:
        """Expose the configured aliases through the Anthropic model list."""
        response = _client(_Core()).get("/v1/models")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data"][0]["id"], "alias")
        self.assertEqual(payload["data"][0]["type"], "model")
        self.assertFalse(payload["has_more"])

    def test_x_api_key_authentication(self) -> None:
        """Accept a valid x-api-key and reject an invalid one."""
        users = [{"token": "secret"}]
        client = _client(_Core(users=users))
        ok = client.post(
            "/v1/messages",
            headers={"x-api-key": "secret"},
            json={"model": "alias", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(ok.status_code, 200)
        denied = client.post(
            "/v1/messages",
            headers={"x-api-key": "wrong"},
            json={"model": "alias", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["type"], "error")
        self.assertEqual(denied.json()["error"]["type"], "authentication_error")

    def test_invalid_body_is_anthropic_error(self) -> None:
        """Reject missing max_tokens with the Anthropic error envelope."""
        client = _client(_Core())
        response = client.post("/v1/messages", json={"model": "alias", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["type"], "error")
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")

    def test_unknown_model_is_not_found(self) -> None:
        """Map an unresolved model to not_found_error before queueing."""
        client = _client(_Core())
        response = client.post(
            "/v1/messages",
            json={"model": "missing", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["type"], "not_found_error")

    def test_embedding_model_is_rejected_for_chat(self) -> None:
        """Reject an embedding-only model with invalid_request_error."""
        core = _Core(providers={"local": {"api": "openaix", "models": [{"id": "embed", "alias": "embed", "embedding": True}]}})
        response = _client(core).post(
            "/v1/messages",
            json={"model": "embed", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")

    def test_timeout_error_mapping(self) -> None:
        """Map a failed timed-out task to a 504 Anthropic timeout error."""
        client = _client(_Core(error={"code": "TIMEOUT", "message": "late"}))
        response = client.post(
            "/v1/messages",
            json={"model": "alias", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["type"], "timeout_error")

    def test_native_stream_passthrough(self) -> None:
        """Preserve native Anthropic SSE event bytes."""
        event = {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "alias", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}
        raw = f"event: message_start\ndata: {json.dumps(event)}\n\n".encode()
        stop = b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
        core = _Core(stream_chunks=[UpstreamChunk({}, protocol="anthropic", raw=raw, content_type="text/event-stream", original=event), UpstreamChunk({}, protocol="anthropic", raw=stop, content_type="text/event-stream", original={"type": "message_stop"})])
        with _client(core).stream("POST", "/v1/messages", json={"model": "alias", "max_tokens": 32, "stream": True, "messages": [{"role": "user", "content": "hi"}]}) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())
        self.assertIn(raw, body)
        self.assertIn(stop, body)
        self.assertEqual(body.count(b"event: message_start"), 1)

    async def test_openai_endpoint_converts_anthropic_result(self) -> None:
        """Expose an Anthropic provider result through the OpenAI endpoint."""
        core = _Core()
        endpoint = Endpoint_openaix({"id": "openaix", "api": "openaix"})
        endpoint._core = core
        task = Task_agent(payload={"model": "alias"})
        task.route_provider_id = "anthropic_cloud"
        task.status = STATUS_COMPLETED
        task.result = {"message": {"role": "assistant", "content": "hello"}, "done_reason": "stop", "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        task._done_event.set()
        response = await endpoint._openai_sync_response(task, {"model": "alias"})
        payload = json.loads(response.body)
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello")

    async def test_openai_endpoint_maps_anthropic_upstream_error(self) -> None:
        """Do not leak an Anthropic error envelope to an OpenAI client."""
        core = _Core()
        core.config = _Config({
            "models": {"providers": {"anthropic_cloud": {"api": "anthropic", "models": [{"id": "m", "alias": "alias"}]}}},
            "workers": {"default": "openaix", "items": {"openaix": {}}},
            "tasks": {"queue_timeout": 5, "run_timeout": 5},
        })
        endpoint = Endpoint_openaix({"id": "openaix", "api": "openaix"})
        endpoint._core = core
        task = Task_agent(payload={"model": "alias"})
        task.route_provider_id = "anthropic_cloud"
        task.status = STATUS_FAILED
        task.error = {"code": "UPSTREAM_ERROR", "message": "bad", "upstream_status": 400, "upstream_body_base64": "eyJ0eXBlIjoiZXJyb3IiLCJlcnJvciI6eyJ0eXBlIjoiaW52YWxpZF9yZXF1ZXN0X2Vycm9yIiwibWVzc2FnZSI6ImJhZCJ9fQ==", "upstream_content_type": "application/json"}
        task._done_event.set()
        response = await endpoint._openai_sync_response(task, {"model": "alias"})
        payload = json.loads(response.body)
        self.assertNotEqual(payload.get("type"), "error")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(payload["error"]["code"], "UPSTREAM_ERROR")

    def test_synthesized_stream_for_internal_chunks(self) -> None:
        """Synthesize Anthropic SSE when the worker emitted internal chunks."""
        core = _Core(stream_chunks=[
            {"message": {"role": "assistant", "content": "hi"}, "done": False},
            {"message": {"role": "assistant", "content": "!"}, "done": False},
            {"done": True, "done_reason": "stop", "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
        ])
        with _client(core).stream("POST", "/v1/messages", json={"model": "alias", "max_tokens": 32, "stream": True, "messages": [{"role": "user", "content": "hi"}]}) as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())
        text = body.decode()
        self.assertIn('"type": "message_start"', text)
        self.assertIn('"type": "content_block_delta"', text)
        self.assertIn('"text_delta"', text)
        self.assertIn('"type": "message_stop"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
