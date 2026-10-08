"""Regression tests for task-correlated endpoint audit events."""
from __future__ import annotations

import asyncio
import json
import hashlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from core.audit_log import AuditLog
from core.endpoints.endpoint_mcp import Endpoint_mcp
from core.endpoints.endpoint_ollama import Endpoint_ollama
from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.task import STATUS_COMPLETED
from core.task_types.task_agent import Task_agent
from core.upstream_response import UpstreamChunk
from workers.agent.openaix.app import OpenAIxWorker


class _Config:
    """Minimal endpoint configuration accessor for audit integration tests."""

    @staticmethod
    def get(key: str, default=None):
        """Return configured task timeouts or the requested default."""
        if key == "tasks":
            return {"queue_timeout": 30, "run_timeout": 30}
        return default


class _Core:
    """Complete endpoint tasks immediately with a deterministic response."""

    config = _Config()

    def __init__(self, audit_log: AuditLog) -> None:
        """Store the test audit writer and received task list."""
        self.audit_log = audit_log
        self.tasks = []
        self.workers = {}

    async def on_task_added(self, task) -> None:
        """Complete an accepted task with an Ollama-compatible result."""
        self.tasks.append(task)
        task.result = {"model": "example", "message": {"role": "assistant", "content": "hello"}, "done": True}
        task.status = STATUS_COMPLETED
        task._done_event.set()

    @staticmethod
    async def delete_task(task_id: str) -> None:
        """Accept deferred endpoint cleanup without external storage."""


class EndpointAuditTests(unittest.TestCase):
    """Verify the endpoint writes request and response audit events after task creation."""

    def test_every_endpoint_logs_ingress_before_route_matching(self) -> None:
        """Log an incoming request even when no endpoint route matches it."""
        endpoints = (
            Endpoint_ollama({"id": "ollama", "worker": "call_ollama"}),
            Endpoint_openaix({"id": "openaix", "worker": "openaix"}),
            Endpoint_mcp({"id": "mcp", "tools": {"echo": "echo_worker"}}),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            core = _Core(AuditLog(temporary_directory))
            with patch("core.endpoint.log") as logger:
                for endpoint in endpoints:
                    with self.subTest(endpoint=endpoint.id), TestClient(endpoint.create_app(core)) as client:
                        response = client.get("/ingress-test")
                        self.assertEqual(response.status_code, 404)

            expected_messages = {
                endpoint.id: f"Incoming request method=GET path=/ingress-test"
                for endpoint in endpoints
            }
            actual_messages = {
                call.args[3]: call.args[2]
                for call in logger.call_args_list
                if call.args[:2] == ("http", "info")
            }
        self.assertEqual(actual_messages, expected_messages)

    def test_sync_chat_records_correlated_request_and_response(self) -> None:
        """Write matching client audit records containing the visible JSON payloads."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            core = _Core(AuditLog(temporary_directory))
            endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
            with TestClient(endpoint.create_app(core)) as client:
                response = client.post(
                    "/api/chat",
                    headers={"X-Request-ID": "request-1"},
                    json={"model": "example", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                )

            self.assertEqual(response.status_code, 200)
            task_id = core.tasks[0].id
            request_events = list(core.audit_log.directory.glob("raw_client_requests-*.jsonl"))
            response_events = list(core.audit_log.directory.glob("raw_client_responses-*.jsonl"))
            self.assertEqual(len(request_events), 1)
            self.assertEqual(len(response_events), 1)

            records = []
            for journal in request_events + response_events:
                records.extend(json.loads(line) for line in journal.read_text().splitlines())
            self.assertEqual({record["task_id"] for record in records}, {task_id})
            self.assertEqual({record["request_id"] for record in records}, {"request-1"})
            self.assertEqual(records[0]["data"]["model"], "example")
            self.assertEqual(records[1]["data"]["message"]["content"], "hello")
            for record in records:
                self.assertEqual(core.audit_log.read_event(record["event_id"]), record)

    def test_invalid_chat_json_records_compact_pre_task_rejection(self) -> None:
        """Reject malformed input without task creation or body retention."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            core = _Core(AuditLog(temporary_directory))
            endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
            with TestClient(endpoint.create_app(core)) as client:
                response = client.post(
                    "/api/chat",
                    headers={"X-Request-ID": "request-invalid-1", "content-type": "application/json"},
                    content=b'{"model":',
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(core.tasks, [])
            journal = next(core.audit_log.directory.glob("rejected_requests-*.jsonl"))
            event = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(event["request_id"], "request-invalid-1")
            self.assertEqual(event["error_code"], "INVALID_REQUEST")
            self.assertNotIn("data", event)

    def test_non_object_json_is_rejected_by_every_task_endpoint(self) -> None:
        """Reject valid JSON scalars and arrays before task construction."""
        endpoints = (
            (Endpoint_ollama({"id": "ollama", "worker": "call_ollama"}), "/api/chat"),
            (Endpoint_openaix({"id": "openaix", "worker": "openaix"}), "/api/chat"),
            (Endpoint_openaix({"id": "openaix", "worker": "openaix"}), "/v1/chat/completions"),
            (Endpoint_openaix({"id": "openaix", "worker": "openaix"}), "/api/embed"),
            (Endpoint_openaix({"id": "openaix", "worker": "openaix"}), "/v1/embeddings"),
            (Endpoint_mcp({"id": "mcp", "tools": {"echo": "echo_worker"}}), "/mcp"),
        )
        values = (None, [], "text", 1, True)

        for endpoint, path in endpoints:
            for value in values:
                with self.subTest(path=path, value=value), tempfile.TemporaryDirectory() as temporary_directory:
                    core = _Core(AuditLog(temporary_directory))
                    with TestClient(endpoint.create_app(core)) as client:
                        response = client.post(path, content=json.dumps(value), headers={"content-type": "application/json"})

                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(core.tasks, [])
                    journal = next(core.audit_log.directory.glob("rejected_requests-*.jsonl"))
                    event = json.loads(journal.read_text(encoding="utf-8"))
                    self.assertNotIn("data", event)

    def test_authorization_rejection_is_compact_and_does_not_create_task(self) -> None:
        """Audit a pre-task authorization failure without retaining its request body."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            core = _Core(AuditLog(temporary_directory))
            endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
            endpoint._authorize_and_apply_envid = lambda request, body: JSONResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "Invalid API token"}},
                status_code=401,
            )
            with TestClient(endpoint.create_app(core)) as client:
                response = client.post(
                    "/api/chat",
                    headers={"X-Request-ID": "request-auth-1"},
                    json={"model": "example", "messages": [], "stream": False},
                )

            self.assertEqual(response.status_code, 401)
            self.assertEqual(core.tasks, [])
            journal = next(core.audit_log.directory.glob("rejected_requests-*.jsonl"))
            event = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(event["request_id"], "request-auth-1")
            self.assertEqual(event["error_code"], "UNAUTHORIZED")
            self.assertNotIn("data", event)

    def test_mcp_tool_call_records_original_request_json(self) -> None:
        """Store an accepted MCP tool-call body from the original request bytes."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            core = _Core(AuditLog(temporary_directory))
            endpoint = Endpoint_mcp({"id": "mcp", "tools": {"echo": "echo_worker"}})
            raw_body = b'{ "jsonrpc" : "2.0", "id" : 7, "method" : "tools/call", "params" : { "name" : "echo", "arguments" : { "value" : "hi" } } }'
            with TestClient(endpoint.create_app(core)) as client:
                response = client.post(
                    "/mcp",
                    headers={"X-Request-ID": "request-mcp-1", "content-type": "application/json"},
                    content=raw_body,
                )

            self.assertEqual(response.status_code, 200)
            journal = next(core.audit_log.directory.glob("raw_client_requests-*.jsonl"))
            event = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(event["request_id"], "request-mcp-1")
            self.assertEqual(event["data"], json.loads(raw_body))

    def test_stream_chat_records_one_inline_response_with_sent_bytes(self) -> None:
        """Capture the complete NDJSON stream once after delivering the same bytes."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            core = _Core(AuditLog(temporary_directory))
            endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
            endpoint._core = core
            task = Task_agent(payload={"model": "example"}, stream=True, external=True)
            task.status = STATUS_COMPLETED
            task._audit_client_context = {
                "request_id": "request-stream-1",
                "protocol": "ollama",
                "endpoint": "/api/chat",
            }

            async def consume_stream() -> bytes:
                """Yield deterministic chunks and return the endpoint-delivered body."""
                await task._chunk_queue.put({"message": {"role": "assistant", "content": "hel"}, "done": False})
                await task._chunk_queue.put({"message": {"role": "assistant", "content": "lo"}, "done": True})
                await task._chunk_queue.put(None)
                return b"".join([chunk async for chunk in endpoint._stream_response(task)])

            delivered = asyncio.run(consume_stream())
            journal = next(core.audit_log.directory.glob("raw_client_responses-*.jsonl"))
            records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            event = records[0]
            self.assertEqual(event["task_id"], task.id)
            self.assertEqual(event["body_storage"], "inline")
            self.assertEqual(event["data"].encode("utf-8"), delivered)
            self.assertEqual(event["body_bytes"], len(delivered))

    def test_openai_stream_records_one_inline_response_with_sent_bytes(self) -> None:
        """Capture terminal SSE bytes after OpenAI-compatible stream delivery."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            core = _Core(AuditLog(temporary_directory))
            endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
            endpoint._core = core
            task = Task_agent(payload={"model": "example"}, stream=True, external=True)
            task.status = STATUS_COMPLETED
            task._audit_client_context = {
                "request_id": "request-openai-stream-1",
                "protocol": "openai",
                "endpoint": "/v1/chat/completions",
            }
            request_body = {"model": "example", "messages": [{"role": "user", "content": "hi"}]}

            async def consume_stream() -> bytes:
                """Yield one deterministic OpenAI stream chunk and its terminal marker."""
                await task._chunk_queue.put({"message": {"role": "assistant", "content": "hello"}, "done": True})
                await task._chunk_queue.put(None)
                return b"".join([chunk async for chunk in endpoint._openai_stream_response(task, request_body)])

            delivered = asyncio.run(consume_stream())
            journal = next(core.audit_log.directory.glob("raw_client_responses-*.jsonl"))
            records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            event = records[0]
            self.assertEqual(event["body_storage"], "inline")
            self.assertEqual(event["data"]["object"], "chat.completion")
            self.assertEqual(event["data"]["choices"][0]["message"], {"role": "assistant", "content": "hello"})
            self.assertEqual(event["data"]["choices"][0]["finish_reason"], "stop")
            self.assertEqual(event["body_format"], "sse")
            self.assertEqual(event["sse_chunk_count"], 1)
            self.assertTrue(delivered.endswith(b"data: [DONE]\n\n"))

    def test_native_sse_is_delivered_unchanged_then_audited_as_json(self) -> None:
        """Keep client bytes exact while final audit data contains all decoded JSON chunks."""
        payloads = [
            {
                "id": "executor-id", "object": "chat.completion.chunk", "model": "model",
                "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "trace", "vendor": 7}, "finish_reason": None}],
            },
            {
                "id": "executor-id", "object": "chat.completion.chunk", "model": "model",
                "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": "length"}],
            },
        ]
        events = [
            b": keepalive\r\n\r\n",
            b"event: message\r\ndata: " + json.dumps(payloads[0]).encode() + b"\r\n\r\n",
            b"data: " + json.dumps(payloads[1]).encode() + b"\r\n\r\n",
            b"data: [DONE]\r\n\r\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            core = _Core(AuditLog(directory))
            endpoint = Endpoint_openaix({"id": "openaix"})
            endpoint._core = core
            task = Task_agent(payload={"model": "example"}, stream=True)
            task.status = STATUS_COMPLETED
            task._audit_client_context = {
                "request_id": "native-sse",
                "protocol": "openai",
                "endpoint": "/v1/chat/completions",
            }

            async def consume_stream() -> bytes:
                """Forward original executor frames through the ordinary stream queue."""
                for raw in events:
                    await task._chunk_queue.put(UpstreamChunk({}, protocol="openai", raw=raw, content_type="text/event-stream", original=None))
                await task._chunk_queue.put(None)
                return b"".join([chunk async for chunk in endpoint._openai_stream_response(task, {})])

            delivered = asyncio.run(consume_stream())
            self.assertEqual(delivered, b"".join(events))
            journal = next(core.audit_log.directory.glob("raw_client_responses-*.jsonl"))
            event = json.loads(journal.read_text())
            self.assertEqual(event["data"]["id"], "executor-id")
            self.assertEqual(event["data"]["object"], "chat.completion")
            self.assertEqual(event["data"]["choices"][0]["message"], {
                "role": "assistant", "reasoning_content": "trace", "vendor": 7, "content": "answer",
            })
            self.assertEqual(event["data"]["choices"][0]["finish_reason"], "length")
            self.assertEqual(event["data_encoding"], "json")
            self.assertEqual(event["body_format"], "sse")
            self.assertEqual(event["sse_chunk_count"], len(payloads))
            self.assertEqual(event["body_sha256"], hashlib.sha256(delivered).hexdigest())
            self.assertEqual(event["body_bytes"], len(delivered))


class UpstreamAuditCancellationTests(unittest.IsolatedAsyncioTestCase):
    """Verify partial upstream streams are finalized when canceled."""

    async def test_openaix_stream_cancellation_records_partial_raw_response(self) -> None:
        """Finalize the started LLM exchange with partial bytes and cancelled status."""
        class _Response:
            """Successful upstream stream that pauses after its first raw chunk."""

            status_code = 200
            headers = {"content-type": "application/x-ndjson"}

            async def __aenter__(self):
                """Enter the fake streaming response."""
                return self

            async def __aexit__(self, exc_type, exc, traceback) -> None:
                """Leave the fake streaming response."""

            async def aiter_raw(self):
                """Yield one raw NDJSON message before waiting for cancellation."""
                yield b'{"message":{"role":"assistant","content":"partial"},"done":false}\n'
                await asyncio.Event().wait()

        class _Client:
            """Minimal client exposing the stream API used by the worker."""

            def __init__(self, response) -> None:
                """Store the response returned for the one upstream request."""
                self._response = response

            def stream(self, *args, **kwargs):
                """Return the configured response context manager."""
                return self._response

        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            worker = OpenAIxWorker()
            worker.id = "openaix"
            worker._core = SimpleNamespace(
                audit_log=audit_log,
                config=SimpleNamespace(get=lambda key, default=None: default),
            )
            task = Task_agent(payload={"model": "example", "messages": []}, stream=True)
            parsed = asyncio.Event()

            async def mark_parsed(chunk: dict) -> None:
                """Signal once the worker has parsed and retained the raw chunk."""
                parsed.set()

            running = asyncio.create_task(
                worker._forward_stream(
                    _Client(_Response()),
                    "http://example.test/api/chat",
                    {"model": "example", "messages": []},
                    mark_parsed,
                    task=task,
                )
            )
            await parsed.wait()
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running

            responses = [
                audit_log.read_event(event["event_id"])
                for event in audit_log.list_task_events(task.id)
                if event["type"] == "llm_response"
            ]
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0]["terminal_status"], "cancelled")
            self.assertEqual(responses[0]["data"], {"message": {"role": "assistant", "content": "partial"}, "done": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)