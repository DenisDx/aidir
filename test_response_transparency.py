"""Response fidelity regressions for native protocols, conversion, and internal tools."""
from __future__ import annotations

import gzip
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from core.endpoints.endpoint_ollama import Endpoint_ollama
from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.queue_manager import QueueManager
from core.scheduler import Scheduler
from core.task import STATUS_COMPLETED, Task
from core.task_types.task_agent import Task_agent
from core.upstream_response import UpstreamChunk, capture_response, original_payload, original_response
from core.worker import WorkerResult
from workers.agent.call_llama_cpp.app import CallLlamaCppWorker
from workers.agent.call_ollama.app import CallOllamaWorker
from workers.agent.openaix.app import OpenAIxWorker


class _FragmentedStream(httpx.AsyncByteStream):
    """Return arbitrary byte fragments, including split UTF-8 and SSE delimiters."""

    def __init__(self, body: bytes) -> None:
        """Store the exact executor stream."""
        self.body = body

    async def __aiter__(self):
        """Yield small fragments that must not alter the client-visible stream."""
        for offset in range(0, len(self.body), 7):
            yield self.body[offset:offset + 7]


class ResponseTransparencyTests(unittest.IsolatedAsyncioTestCase):
    """Verify executor response fidelity rather than a subset of copied fields."""

    def setUp(self) -> None:
        """Create rich executor responses and isolated endpoint cleanup."""
        self.openai = {
            "id": "executor-completion",
            "object": "chat.completion",
            "created": 123456,
            "model": "real-model",
            "system_fingerprint": "executor-fingerprint",
            "choices": [
                {"index": 0, "finish_reason": "length", "logprobs": {"content": []}, "message": {
                    "role": "assistant", "content": "", "reasoning_content": "model reasoning",
                    "reasoning_details": [{"text": "detail"}], "refusal": None, "vendor_message": {"a": 1},
                }},
                {"index": 1, "finish_reason": "stop", "message": {"role": "assistant", "content": "alternative"}},
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16, "completion_tokens_details": {"reasoning_tokens": 5}},
            "vendor_response": {"enabled": True},
        }
        self.ollama = {
            "model": "real-model", "created_at": "2026-10-03T16:00:00Z",
            "message": {"role": "assistant", "content": "", "thinking": "model reasoning", "vendor_message": {"a": 1}},
            "done": True, "done_reason": "length", "total_duration": 456,
            "prompt_eval_count": 7, "eval_count": 9, "vendor_response": {"enabled": True},
        }
        self.core = SimpleNamespace(delete_task=AsyncMock())

    @staticmethod
    def complete(task: Task_agent, result: WorkerResult) -> None:
        """Transfer worker data and transport to a completed endpoint task."""
        task.result = result.data
        task.upstream_response = result.upstream_response
        task.status = STATUS_COMPLETED
        task._done_event.set()
        task._chunk_queue.put_nowait(None)

    async def sync_result(self, worker, payload: dict) -> tuple[Task_agent, WorkerResult, bytes]:
        """Execute a worker against a deterministic JSON executor without a network."""
        body = b" \n" + json.dumps(payload, ensure_ascii=False, indent=2).encode() + b"\n"
        content_type = "application/json; charset=utf-8"

        async def respond(request: httpx.Request) -> httpx.Response:
            """Return the original executor JSON bytes."""
            return httpx.Response(200, content=body, headers={"content-type": content_type})

        task = Task_agent(payload={"model": "alias", "messages": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await worker._forward_sync(client, "http://executor/chat", {}, task=task)
        self.assertTrue(result.ok)
        self.complete(task, result)
        return task, result, body

    async def test_native_sync_bodies_match_exactly_and_survive_persistence(self) -> None:
        """Preserve complete native response bytes, reasoning, choices, and content type."""
        for worker, payload, protocol in (
            (CallLlamaCppWorker(), self.openai, "openai"),
            (CallOllamaWorker(), self.ollama, "ollama"),
            (OpenAIxWorker(), self.ollama, "ollama"),
        ):
            with self.subTest(worker=type(worker).__name__):
                task, result, body = await self.sync_result(worker, payload)
                endpoint = Endpoint_openaix({"id": "test"})
                endpoint._core = self.core
                response = await endpoint._openai_sync_response(task, {}) if protocol == "openai" else await endpoint._sync_response(task)
                self.assertEqual(response.body, body)
                self.assertEqual(response.headers["content-type"], "application/json; charset=utf-8")
                restored = Task.from_redis_hash(task.to_redis_hash())
                self.assertEqual(original_response(restored.upstream_response, protocol).body, body)
                self.assertEqual(result.upstream_response, restored.upstream_response)

    async def test_native_streams_match_exactly_with_fragmentation(self) -> None:
        """Keep SSE/NDJSON bytes, all fields, controls, Unicode, and one original terminator."""
        event = {**self.openai, "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {"content": None, "reasoning_content": "caf\u00e9", "vendor_delta": 12}, "finish_reason": None},
            {"index": 1, "delta": {"content": "second"}, "finish_reason": "length"},
        ]}
        sse = b": keepalive\r\n\r\nevent: chunk\r\nid: executor-event\r\n"
        sse += b"".join(b"data: " + line.encode() + b"\r\n" for line in json.dumps(event, ensure_ascii=False, indent=2).splitlines())
        sse += b"\r\ndata: {\"choices\":[],\"usage\":{\"completion_tokens_details\":{\"reasoning_tokens\":5}}}\r\n\r\ndata: [DONE]\r\n\r\n: final comment\r\n\r\n"
        ndjson = b"\r\n" + json.dumps(self.ollama, ensure_ascii=False, separators=(", ", ": ")).encode() + b"\r\n"
        for worker, body, protocol in (
            (CallLlamaCppWorker(), sse, "openai"),
            (CallOllamaWorker(), ndjson, "ollama"),
            (OpenAIxWorker(), ndjson, "ollama"),
        ):
            with self.subTest(worker=type(worker).__name__):
                content_type = "text/event-stream; charset=utf-8" if protocol == "openai" else "application/x-ndjson; charset=utf-8"

                async def respond(request: httpx.Request) -> httpx.Response:
                    """Return an unread, fragmented executor stream."""
                    return httpx.Response(200, stream=_FragmentedStream(body), headers={"content-type": content_type})

                task = Task_agent(payload={"model": "alias"}, stream=True)

                async def emit(chunk: dict) -> None:
                    """Push worker output through the ordinary endpoint queue."""
                    await task._chunk_queue.put(chunk)

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    result = await worker._forward_stream(client, "http://executor/chat", {}, emit_chunk=emit, task=task)
                self.assertTrue(result.ok)
                self.complete(task, result)
                endpoint = Endpoint_openaix({"id": "test"})
                endpoint._core = self.core
                response = await endpoint._openai_streaming_response(task, {}) if protocol == "openai" else await endpoint._streaming_response(task)
                output = b"".join([chunk async for chunk in response.body_iterator])
                self.assertEqual(output, body)
                self.assertEqual(response.headers["content-type"], content_type)

    async def test_cross_protocol_sync_keeps_reasoning_and_extra_fields(self) -> None:
        """Translate envelopes without substituting reasoning for an empty answer."""
        endpoint = Endpoint_openaix({"id": "test"})
        endpoint._core = self.core
        task, _, _ = await self.sync_result(CallOllamaWorker(), self.ollama)
        converted = json.loads((await endpoint._openai_sync_response(task, {})).body)
        self.assertEqual(converted["choices"][0]["message"]["content"], "")
        self.assertEqual(converted["choices"][0]["message"]["reasoning_content"], "model reasoning")
        self.assertEqual(converted["choices"][0]["message"]["vendor_message"], {"a": 1})
        self.assertEqual(converted["choices"][0]["finish_reason"], "length")
        self.assertEqual(converted["vendor_response"], {"enabled": True})
        self.assertEqual(converted["total_duration"], 456)
        self.assertEqual(converted["usage"]["prompt_tokens"], 7)
        task, _, _ = await self.sync_result(CallLlamaCppWorker(), self.openai)
        converted = json.loads((await endpoint._sync_response(task)).body)
        self.assertEqual(converted["message"]["content"], "")
        self.assertEqual(converted["message"]["thinking"], "model reasoning")
        self.assertEqual(converted["message"]["reasoning_details"], [{"text": "detail"}])
        self.assertEqual(converted["choices"], self.openai["choices"])
        self.assertEqual(converted["usage"], self.openai["usage"])
        self.assertEqual(converted["system_fingerprint"], "executor-fingerprint")

    async def test_cross_protocol_stream_keeps_reasoning_and_finish_reason(self) -> None:
        """Convert original Ollama stream payload, not its legacy normalized content."""
        body = json.dumps(self.ollama).encode() + b"\n"

        async def respond(request: httpx.Request) -> httpx.Response:
            """Return one Ollama event containing reasoning and an empty answer."""
            return httpx.Response(200, stream=_FragmentedStream(body))

        task = Task_agent(payload={}, stream=True)
        worker = CallOllamaWorker()

        async def emit(chunk: dict) -> None:
            """Queue executor chunks for OpenAI conversion."""
            await task._chunk_queue.put(chunk)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await worker._forward_stream(client, "http://executor/chat", {}, emit_chunk=emit, task=task)
        self.complete(task, result)
        endpoint = Endpoint_openaix({"id": "test"})
        endpoint._core = self.core
        response = await endpoint._openai_streaming_response(task, {})
        chunks = [chunk async for chunk in response.body_iterator]
        converted = json.loads(chunks[0].decode().removeprefix("data: "))
        self.assertEqual(converted["choices"][0]["delta"]["content"], "")
        self.assertEqual(converted["choices"][0]["delta"]["reasoning_content"], "model reasoning")
        self.assertEqual(converted["choices"][0]["finish_reason"], "length")
        self.assertEqual(converted["vendor_response"], {"enabled": True})
        self.assertEqual(chunks[-1], b"data: [DONE]\n\n")

    async def test_openai_stream_to_ollama_keeps_reasoning_and_extensions(self) -> None:
        """Convert OpenAI deltas without emitting SSE controls as empty Ollama messages."""
        payload = {**self.openai, "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": "", "reasoning": "trace", "vendor_delta": 12}, "finish_reason": "length"},
        ]}
        body = b": keepalive\n\ndata: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n"

        async def respond(request: httpx.Request) -> httpx.Response:
            """Return OpenAI reasoning and control events."""
            return httpx.Response(200, stream=_FragmentedStream(body))

        task = Task_agent(payload={}, stream=True)

        async def emit(chunk: dict) -> None:
            """Queue executor deltas and transport controls."""
            await task._chunk_queue.put(chunk)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await CallLlamaCppWorker()._forward_stream(client, "http://executor/chat", {}, emit, task=task)
        self.complete(task, result)
        endpoint = Endpoint_openaix({"id": "test"})
        endpoint._core = self.core
        response = await endpoint._streaming_response(task)
        chunks = [chunk async for chunk in response.body_iterator]
        self.assertEqual(len(chunks), 1)
        converted = json.loads(chunks[0])
        self.assertEqual(converted["message"]["content"], "")
        self.assertEqual(converted["message"]["thinking"], "trace")
        self.assertEqual(converted["message"]["vendor_delta"], 12)
        self.assertEqual(converted["done_reason"], "length")
        self.assertEqual(converted["usage"], self.openai["usage"])
        self.assertEqual(converted["choices"], payload["choices"])

    async def test_compressed_native_streams_preserve_decoded_events(self) -> None:
        """Handle HTTP compression without treating compressed bytes as JSON/SSE text."""
        for protocol, worker, payload in (
            ("openai", CallLlamaCppWorker(), self.openai),
            ("ollama", CallOllamaWorker(), self.ollama),
        ):
            body = b"data: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n" if protocol == "openai" else json.dumps(payload).encode() + b"\n"

            async def respond(request: httpx.Request) -> httpx.Response:
                """Return a gzip-encoded executor stream."""
                return httpx.Response(200, stream=_FragmentedStream(gzip.compress(body)), headers={"content-encoding": "gzip"})

            emitted = []

            async def emit(chunk: dict) -> None:
                """Capture original decoded stream transport."""
                emitted.append(chunk.raw)

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                result = await worker._forward_stream(client, "http://executor/chat", {}, emit_chunk=emit, task=Task_agent(payload={}, stream=True))
            self.assertTrue(result.ok)
            self.assertEqual(b"".join(emitted), body)

    async def test_invalid_sse_event_is_an_explicit_failure(self) -> None:
        """Reject malformed or non-object executor events rather than dropping them."""
        for body in (b"data: {broken}\n\n", b"data: []\n\n"):
            with self.subTest(body=body):
                async def respond(request: httpx.Request) -> httpx.Response:
                    """Return an invalid executor chat event."""
                    return httpx.Response(200, stream=_FragmentedStream(body))

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    result = await CallLlamaCppWorker()._forward_stream(client, "http://executor/chat", {}, None)
                self.assertFalse(result.ok)
                self.assertEqual(result.error["code"], "UPSTREAM_INVALID_JSON")

    async def test_internal_tool_loops_return_final_answer_without_reasoning(self) -> None:
        """Strip final reasoning only after executing tools, including loop-limit fallback."""
        for protocol in ("openai", "ollama"):
            for max_turns in (1, 3):
                for stream in (False, True):
                    with self.subTest(protocol=protocol, max_turns=max_turns, stream=stream):
                        worker = CallLlamaCppWorker() if protocol == "openai" else OpenAIxWorker()
                        worker._tools_max_turns = max_turns
                        final = json.loads(json.dumps(self.openai if protocol == "openai" else self.ollama))
                        message = final["choices"][0]["message"] if protocol == "openai" else final["message"]
                        message["content"] = "final answer"
                        message["thinking"] = "private final-stage reasoning"
                        final["reasoning"] = "root reasoning"
                        initial = json.loads(json.dumps(final))
                        initial_message = initial["choices"][0]["message"] if protocol == "openai" else initial["message"]
                        initial_message["tool_calls"] = [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]
                        results = []
                        for payload in (initial, final):
                            data = worker._openai_response_to_ollama(payload) if protocol == "openai" else payload
                            results.append(WorkerResult(
                                ok=True, data=data, usage=data.get("usage"),
                                upstream_response=capture_response(protocol, 200, "application/json", json.dumps(payload)),
                            ))
                        task = Task_agent(payload={"model": "model", "messages": []}, stream=stream)
                        task.config["injected_tool_names"] = ["lookup"]
                        emitted = []

                        async def emit(chunk: dict) -> None:
                            """Capture only final user-facing tool-loop output."""
                            emitted.append(chunk)

                        with patch.object(worker, "_forward_sync", AsyncMock(side_effect=results)), patch.object(worker, "_execute_internal_tool", AsyncMock(return_value=WorkerResult(ok=True, data={"content": "tool result"}))) as execute:
                            result = await worker._run_with_internal_tools(None, "http://executor/chat", task.payload, task, emit)
                        self.assertTrue(result.ok)
                        execute.assert_awaited_once()
                        payload = original_payload(result.upstream_response, protocol, {})
                        message = payload["choices"][0]["message"] if protocol == "openai" else payload["message"]
                        self.assertEqual(message["content"], "final answer")
                        for field in ("thinking", "reasoning", "reasoning_content", "reasoning_details"):
                            self.assertNotIn(field, message)
                            self.assertNotIn(field, payload)
                            self.assertNotIn(field, result.data["message"])
                        self.assertEqual(payload["vendor_response"], {"enabled": True})
                        if protocol == "openai":
                            self.assertEqual(payload["usage"], self.openai["usage"])
                        self.assertEqual(len(emitted), 1 if stream else 0)

    async def test_caller_owned_tools_keep_original_reasoning(self) -> None:
        """Tool definitions alone do not make a request an internal multistep task."""
        worker = CallLlamaCppWorker()
        payload = json.loads(json.dumps(self.openai))
        payload["choices"][0]["message"]["tool_calls"] = [{"id": "external", "function": {"name": "caller_tool", "arguments": "{}"}}]
        raw = json.dumps(payload).encode()
        step = WorkerResult(ok=True, data=worker._openai_response_to_ollama(payload), upstream_response=capture_response("openai", 200, "application/json", raw))
        task = Task_agent(payload={"messages": [], "tools": [{"type": "function"}]})
        with patch.object(worker, "_forward_sync", AsyncMock(return_value=step)), patch.object(worker, "_execute_internal_tool", AsyncMock()) as execute:
            result = await worker._run_with_internal_tools(None, "http://executor/chat", task.payload, task, None)
        execute.assert_not_awaited()
        self.assertEqual(original_response(result.upstream_response, "openai").body, raw)

    async def test_empty_final_answer_never_contains_tool_loop_reasoning(self) -> None:
        """Do not leak reasoning through the legacy thinking-to-content normalization."""
        worker = OpenAIxWorker()
        worker._tools_max_turns = 3
        initial = {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "lookup", "arguments": {}}}]}}
        final = {"message": {"role": "assistant", "content": "", "thinking": "must not become final content"}, "done": True}
        steps = [
            WorkerResult(ok=True, data=worker._normalize_upstream_response_data(payload), upstream_response=capture_response("ollama", 200, "application/json", json.dumps(payload)))
            for payload in (initial, final)
        ]
        task = Task_agent(payload={"messages": []}, stream=True)
        task.config["injected_tool_names"] = ["lookup"]
        emitted = []

        async def emit(chunk: dict) -> None:
            """Capture synthesized final stream output."""
            emitted.append(chunk)

        with patch.object(worker, "_forward_sync", AsyncMock(side_effect=steps)), patch.object(worker, "_execute_internal_tool", AsyncMock(return_value=WorkerResult(ok=True, data={}))):
            result = await worker._run_with_internal_tools(None, "http://executor/chat", task.payload, task, emit)
        self.assertEqual(result.data["message"]["content"], "")
        self.assertNotIn("thinking", emitted[0]["message"])
        self.assertEqual(emitted[0]["message"]["content"], "")

    async def test_caller_owned_tools_use_native_streaming(self) -> None:
        """Do not force synchronous tool-loop transport for tools owned by the caller."""
        for worker in (CallLlamaCppWorker(), OpenAIxWorker()):
            with self.subTest(worker=type(worker).__name__):
                worker._core = SimpleNamespace(
                    config=SimpleNamespace(get=lambda key, default=None: default),
                    llama_cpp_server_manager=SimpleNamespace(ensure_running=AsyncMock()),
                )
                task = Task_agent(payload={"model": "model", "messages": [], "tools": [{"type": "function", "function": {"name": "caller_tool"}}]}, stream=True)
                with patch.object(worker, "_apply_context_chain", AsyncMock(return_value=WorkerResult(ok=True))), patch.object(worker, "_forward_stream", AsyncMock(return_value=WorkerResult(ok=True))) as streaming, patch.object(worker, "_run_with_internal_tools", AsyncMock()) as tool_loop:
                    result = await worker.execute(task)
                self.assertTrue(result.ok)
                streaming.assert_awaited_once()
                tool_loop.assert_not_awaited()

    async def test_scheduler_transfers_and_persists_original_transport(self) -> None:
        """Exercise the real scheduler and queue handoff, not just endpoint helper state."""
        redis = SimpleNamespace(hset=AsyncMock())
        queue = QueueManager(redis)
        task = Task_agent(payload={})
        queue._tasks[task.id] = task
        metadata = capture_response("openai", 200, "application/json", json.dumps(self.openai))
        worker = SimpleNamespace(id="worker", execute=AsyncMock(return_value=WorkerResult(ok=True, data={"message": {"content": ""}}, upstream_response=metadata)))
        scheduler = Scheduler(queue, {"worker": worker})
        await scheduler._run_task(task, worker)
        self.assertEqual(task.status, STATUS_COMPLETED)
        self.assertEqual(task.upstream_response, metadata)
        self.assertEqual(json.loads(redis.hset.call_args.kwargs["mapping"]["upstream_response"]), metadata)

    async def test_queue_persists_and_clears_original_response(self) -> None:
        """Persist the final transport and clear it before a subsequent attempt."""
        redis = SimpleNamespace(hset=AsyncMock())
        queue = QueueManager(redis)
        task = Task_agent(payload={})
        task.upstream_response = capture_response("openai", 200, "application/json", json.dumps(self.openai))
        queue._tasks[task.id] = task
        await queue.mark_completed(task)
        stored = redis.hset.call_args.kwargs["mapping"]["upstream_response"]
        self.assertEqual(json.loads(stored), task.upstream_response)
        await queue.mark_running(task.id, "worker")
        self.assertIsNone(task.upstream_response)
        self.assertEqual(redis.hset.call_args.kwargs["mapping"]["upstream_response"], "")

    def test_real_http_routes_keep_native_response_bytes(self) -> None:
        """Exercise actual OpenAI/Ollama HTTP routes with a completed native response."""
        for endpoint_type, route, protocol, payload in (
            (Endpoint_openaix, "/v1/chat/completions", "openai", self.openai),
            (Endpoint_openaix, "/api/chat", "ollama", self.ollama),
            (Endpoint_ollama, "/api/chat", "ollama", self.ollama),
        ):
            with self.subTest(route=route, endpoint=endpoint_type.__name__):
                body = json.dumps(payload, indent=2).encode() + b"\n"
                task = Task_agent(payload={"model": "alias"})
                self.complete(task, WorkerResult(ok=True, data={}, upstream_response=capture_response(protocol, 200, "application/json", body)))
                endpoint = endpoint_type({"id": "test"})
                core = SimpleNamespace(
                    config=SimpleNamespace(get=lambda key, default=None: default),
                    on_task_added=AsyncMock(), delete_task=AsyncMock(),
                )
                with patch.object(endpoint, "_build_task_for_payload_async", AsyncMock(return_value=task)):
                    with TestClient(endpoint.create_app(core)) as client:
                        response = client.post(route, json={"model": "alias", "messages": [], "stream": False})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, body)
                self.assertEqual(response.json(), payload)

    def test_real_http_stream_routes_keep_native_event_bytes(self) -> None:
        """Exercise actual streaming routes without exposing internal transport fields."""
        for route, protocol, payload in (
            ("/v1/chat/completions", "openai", self.openai),
            ("/api/chat", "ollama", self.ollama),
        ):
            with self.subTest(route=route):
                body = b"data: " + json.dumps(payload).encode() + b"\r\n\r\ndata: [DONE]\r\n\r\n" if protocol == "openai" else json.dumps(payload).encode() + b"\r\n"
                task = Task_agent(payload={"model": "alias"}, stream=True)
                content_type = "text/event-stream" if protocol == "openai" else "application/x-ndjson"
                task._chunk_queue.put_nowait(UpstreamChunk({}, protocol=protocol, raw=body, content_type=content_type, original=payload))
                self.complete(task, WorkerResult(ok=True))
                endpoint = Endpoint_openaix({"id": "test"})
                core = SimpleNamespace(
                    config=SimpleNamespace(get=lambda key, default=None: default),
                    on_task_added=AsyncMock(), delete_task=AsyncMock(),
                )
                with patch.object(endpoint, "_build_task_for_payload_async", AsyncMock(return_value=task)):
                    with TestClient(endpoint.create_app(core)) as client:
                        response = client.post(route, json={"model": "alias", "messages": [], "stream": True})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, body)
                self.assertEqual(response.headers["content-type"], content_type)


if __name__ == "__main__":
    unittest.main()
