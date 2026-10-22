"""Transport tests for the Anthropic Messages worker."""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock

import httpx

from core.task_types.task_agent import Task_agent
from workers.agent.call_anthropic.app import CallAnthropicWorker
from workers.agent.call_opencode_go.app import CallOpenCodeGoWorker


class _BodyStream(httpx.AsyncByteStream):
    """Yield one SSE body without pre-consuming it."""

    def __init__(self, body: bytes) -> None:
        """Store the body bytes."""
        self.body = body

    async def __aiter__(self):
        """Yield the whole body once."""
        yield self.body


class _Config:
    """Dotted configuration accessor for worker tests."""

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
    """Minimal core carrying provider configuration and no Redis."""

    def __init__(self, providers: dict) -> None:
        """Create a core with the supplied providers and no audit store."""
        self.config = _Config({"models": {"providers": providers}, "instance": "test"})
        self.audit_log = None
        self.queue = None


def _provider(api: str = "anthropic") -> dict:
    """Return one Anthropic-compatible provider definition."""
    return {
        "api": api,
        "baseUrl": "http://upstream",
        "anthropic_version": "2023-06-01",
        "auth": {"type": "x-api-key", "key": "secret"},
        "max_output_tokens": 64,
    }


class AnthropicWorkerTests(unittest.IsolatedAsyncioTestCase):
    """Verify request conversion, headers, sync responses, and SSE decoding."""

    def _worker(self) -> CallAnthropicWorker:
        """Create an initialized worker with a deterministic provider."""
        worker = CallAnthropicWorker()
        worker._core = _Core({"anth": _provider()})
        worker._provider_id = "anth"
        worker._base_url = "http://upstream"
        return worker

    async def test_sync_request_and_response(self) -> None:
        """Send a valid Messages request and normalize the response."""
        worker = self._worker()
        captured: dict = {}

        async def respond(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            captured["headers"] = dict(request.headers)
            return httpx.Response(
                200,
                json={
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "real",
                    "content": [{"type": "text", "text": "hello"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                },
            )

        task = Task_agent(payload={"model": "real", "messages": [{"role": "user", "content": "hi"}]})
        payload = {"model": "real", "messages": [{"role": "user", "content": "hi"}], "max_completion_tokens": 32}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), headers=worker._resolve_request_headers(task, "anth")) as client:
            result = await worker._forward_sync(client, "http://upstream/v1/messages", payload, task=task)

        self.assertTrue(result.ok)
        self.assertEqual(captured["url"], "http://upstream/v1/messages")
        self.assertEqual(captured["body"]["max_tokens"], 32)
        self.assertEqual(captured["headers"]["x-api-key"], "secret")
        self.assertEqual(captured["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(result.data["message"]["content"], "hello")
        self.assertEqual(result.data["usage"]["completion_tokens"], 3)
        self.assertEqual(result.upstream_response["protocol"], "anthropic")

    async def test_stream_decoding(self) -> None:
        """Decode Anthropic SSE into internal content and preserve raw events."""
        worker = self._worker()
        events = [
            {"type": "message_start", "message": {"id": "msg_1", "model": "real", "usage": {"input_tokens": 2}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
        body = b"".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events)

        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=_BodyStream(body), headers={"content-type": "text/event-stream"})

        chunks: list[dict] = []

        async def emit(chunk) -> None:
            chunks.append(chunk)

        task = Task_agent(payload={"model": "real", "messages": []}, stream=True)
        payload = {"model": "real", "messages": [{"role": "user", "content": "hi"}], "max_completion_tokens": 32, "stream": True}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), headers=worker._resolve_request_headers(task, "anth")) as client:
            result = await worker._forward_stream(client, "http://upstream/v1/messages", payload, emit, task=task)

        self.assertTrue(result.ok)
        self.assertEqual(result.data["message"]["content"], "hello")
        self.assertEqual(result.data["done_reason"], "stop")
        self.assertEqual(result.data["usage"]["completion_tokens"], 1)
        self.assertGreaterEqual(len(chunks), 4)
        stop_events = [chunk for chunk in chunks if getattr(chunk, "raw", b"").startswith(b"event: message_stop")]
        self.assertEqual(len(stop_events), 1)
        self.assertEqual(stop_events[0]["message"]["content"], "")
        self.assertTrue(stop_events[0]["done"])

    async def test_http_error_is_preserved(self) -> None:
        """Return the upstream status and message as a worker error."""
        worker = self._worker()

        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})

        task = Task_agent(payload={"model": "real", "messages": []})
        payload = {"model": "real", "messages": [{"role": "user", "content": "hi"}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), headers=worker._resolve_request_headers(task, "anth")) as client:
            result = await worker._forward_sync(client, "http://upstream/v1/messages", payload, task=task)
        self.assertFalse(result.ok)
        self.assertEqual(result.error["upstream_status"], 400)
        self.assertEqual(result.error["message"], "bad")

    def test_model_protocol_and_url_selection(self) -> None:
        """Use the configured per-model protocol to choose the OpenCode Go path."""
        worker = CallOpenCodeGoWorker()
        worker._core = _Core({"go": {"api": "opencode-go", "baseUrl": "http://upstream/v1", "auth": {"type": "bearer", "token": "tok"}}})
        worker._provider_id = "go"
        worker._user_agent = "aidir-test/1.0"
        task = Task_agent(payload={"model": "real", "messages": []})
        task.config = {"route": {"resolved_provider": "go", "resolved_model": "real", "resolved_protocol": "anthropic-messages"}}
        task._opencode_session_value = "session"
        self.assertEqual(worker._chat_url("go", task), "http://upstream/v1/messages")
        headers = worker._resolve_request_headers(task, "go")
        self.assertEqual(headers["x-opencode-session"], "session")
        self.assertEqual(headers["User-Agent"], "aidir-test/1.0")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["x-api-key"], "tok")

        task.config["route"]["resolved_protocol"] = "chat-completions"
        self.assertEqual(worker._chat_url("go", task), "http://upstream/v1/chat/completions")
        headers = worker._resolve_request_headers(task, "go")
        self.assertNotIn("anthropic-version", headers)

    def test_non_anthropic_payload_strips_opaque_blocks(self) -> None:
        """Never send Anthropic-only message extensions to an OpenAI transport."""
        payload = CallAnthropicWorker._to_openai_payload(
            {"model": "m", "messages": [{"role": "assistant", "content": "x", "anthropic_blocks": [{"type": "thinking"}]}]},
            False,
        )
        self.assertNotIn("anthropic_blocks", payload["messages"][0])

    async def test_opencode_transport_dispatch(self) -> None:
        """Send Messages models through the Anthropic forwarder and others through OpenAI."""
        from unittest.mock import patch

        worker = CallOpenCodeGoWorker()
        worker._core = _Core({"go": {"api": "opencode-go", "baseUrl": "http://upstream", "auth": {"type": "bearer", "token": "tok"}}})
        worker._provider_id = "go"
        worker._user_agent = "aidir-test/1.0"
        task = Task_agent(payload={"model": "real", "messages": []})
        task.config = {"route": {"resolved_provider": "go", "resolved_model": "real", "resolved_protocol": "anthropic-messages"}}
        task._opencode_session_value = "session"
        with patch("workers.agent.call_opencode_go.app.CallAnthropicWorker._forward_sync", new=AsyncMock(return_value="messages")) as messages_call:
            with patch("workers.agent.call_opencode_go.app.CallOpenAIWorker._forward_sync", new=AsyncMock(return_value="chat")) as chat_call:
                result = await worker._forward_sync(object(), "url", {}, task=task)
                self.assertEqual(result, "messages")
                self.assertEqual(messages_call.await_count, 1)
                self.assertEqual(chat_call.await_count, 0)
                task.config["route"]["resolved_protocol"] = "chat-completions"
                result = await worker._forward_sync(object(), "url", {}, task=task)
                self.assertEqual(result, "chat")
                self.assertEqual(chat_call.await_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
