"""Focused regressions for the llama.cpp worker and local server lifecycle."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from core.local_server_manager import LocalServerError, LocalServerManager
from core.audit_log import AuditLog
from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.endpoints.endpoint_ollama import Endpoint_ollama
from core.resources import Resources
from core.task_types.task_agent import Task_agent
from core.task import STATUS_COMPLETED, STATUS_FAILED
from core.upstream_errors import build_upstream_error
from workers.agent.call_llama_cpp.app import CallLlamaCppWorker


class TestLlamaCppWorker(unittest.TestCase):
    """Validate protocol conversion without requiring a llama-server binary."""

    def test_converts_ollama_payload_to_openai(self) -> None:
        """Maps internal messages and generation options to OpenAI request fields."""
        payload = CallLlamaCppWorker._to_openai_payload(
            {"model": "model", "messages": [{"role": "user", "content": "hello"}], "options": {"num_predict": 12, "temperature": 0.3}},
            stream=True,
        )

        self.assertEqual(payload["model"], "model")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["max_completion_tokens"], 12)
        self.assertEqual(payload["temperature"], 0.3)

    def test_preserves_openai_parameters_and_completion_limit(self) -> None:
        """Forward OpenAI fields unchanged and prefer their explicit completion limit."""
        payload = CallLlamaCppWorker._to_openai_payload(
            {
                "model": "model",
                "messages": [{"role": "user", "content": "hello"}],
                "max_completion_tokens": 17,
                "reasoning_effort": "low",
                "custom_upstream_option": {"enabled": True},
                "options": {"num_predict": 12},
            },
            stream=False,
        )

        self.assertEqual(payload["max_completion_tokens"], 17)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["custom_upstream_option"], {"enabled": True})

    def test_converts_ollama_options_to_openai_generation_fields(self) -> None:
        """Translate compatible Ollama options for the llama.cpp OpenAI API."""
        payload = CallLlamaCppWorker._to_openai_payload(
            {
                "model": "model",
                "messages": [],
                "options": {"num_predict": 24, "repeat_penalty": 1.1, "top_k": 40},
            },
            stream=False,
        )

        self.assertEqual(payload["max_completion_tokens"], 24)
        self.assertEqual(payload["repetition_penalty"], 1.1)
        self.assertEqual(payload["top_k"], 40)

    def test_converts_openai_response_to_ollama(self) -> None:
        """Maps OpenAI message and usage fields to the internal endpoint response shape."""
        result = CallLlamaCppWorker._openai_response_to_ollama(
            {"model": "model", "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}
        )

        self.assertTrue(result["done"])
        self.assertEqual(result["message"]["content"], "hello")
        self.assertEqual(result["prompt_eval_count"], 4)

    def test_assembles_stream_content_and_reasoning_for_task_result(self) -> None:
        """Retain full streamed assistant output after the terminal empty delta."""
        result = CallLlamaCppWorker._assemble_stream_result(
            {
                "choices": [{"delta": {}, "finish_reason": "length"}],
                "message": {"role": "assistant", "content": None},
                "done": True,
            },
            [],
            ["///", "///"],
        )

        self.assertEqual(result["message"]["content"], None)
        self.assertEqual(result["message"]["thinking"], "//////")


class TestLlamaCppStreamingDiagnostics(unittest.IsolatedAsyncioTestCase):
    """Validate live persistence of exact llama.cpp SSE diagnostics."""

    async def test_stream_persists_request_raw_events_and_reasoning(self) -> None:
        """Stores every received SSE line for Show JSON while the stream is active."""
        class _Queue:
            """Captures task history persistence calls."""

            def __init__(self) -> None:
                self.persisted: list[list[dict]] = []

            async def persist_llm_call_diagnostics(self, task) -> None:
                """Capture a snapshot of persisted call diagnostics."""
                import copy
                self.persisted.append(copy.deepcopy(task.llm_call_history))

        class _Core:
            """Minimal worker core with a diagnostics-aware queue."""

            def __init__(self, directory: str) -> None:
                self.queue = _Queue()
                self.audit_log = AuditLog(directory)

        class _Response:
            """Successful llama.cpp SSE response with a reasoning delta."""

            status_code = 200

            async def __aenter__(self):
                """Enter the fake response context."""
                return self

            async def __aexit__(self, exc_type, exc, traceback) -> None:
                """Leave the fake response context."""

            async def aiter_lines(self):
                """Yield raw llama.cpp SSE lines."""
                yield 'data: {"model":"model","choices":[{"index":0,"delta":{"reasoning_content":"thinking","content":"answer"},"finish_reason":"stop"}]}'
                yield ""
                yield "data: [DONE]"
                yield ""

        class _Client:
            """Minimal client that opens the fake SSE response."""

            @staticmethod
            def stream(*args, **kwargs):
                """Return the configured fake stream response."""
                return _Response()

        worker = CallLlamaCppWorker()
        worker.id = "call_llama_cpp"
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        worker._core = _Core(temporary_directory.name)
        task = Task_agent(payload={"model": "model", "messages": []}, stream=True)
        payload = {"model": "model", "messages": [{"role": "user", "content": "hello"}], "stream": True}
        emitted: list[dict] = []

        async def emit(chunk: dict) -> None:
            """Capture endpoint-bound stream chunks."""
            emitted.append(chunk)

        with patch("workers.agent.call_llama_cpp.app.save_llm_call"):
            result = await worker._forward_stream(
                _Client(),
                "http://127.0.0.1:8888/v1/chat/completions",
                payload,
                emit,
                task=task,
                save_call=True,
            )

        entry = task.llm_call_history[0]
        self.assertTrue(result.ok)
        self.assertEqual(entry["request"], payload)
        self.assertEqual(entry["response"]["final"]["message"]["content"], "answer")
        self.assertNotIn("raw_sse", entry)
        self.assertNotIn("stream_events", entry)
        self.assertEqual(emitted[0]["message"]["content"], "answer")
        self.assertEqual(emitted[0]["message"]["reasoning_content"], "thinking")
        self.assertEqual(emitted[0]["message"]["thinking"], "thinking")
        self.assertGreaterEqual(len(worker._core.queue.persisted), 2)
        records = []
        for journal in worker._core.audit_log.directory.glob("raw_llm_*.jsonl"):
            records.extend(json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines())
        request_event = next(event for event in records if event["type"] == "llm_request")
        response_event = next(event for event in records if event["type"] == "llm_response")
        self.assertEqual(request_event["data"], payload)
        raw_stream = b'data: {"model":"model","choices":[{"index":0,"delta":{"reasoning_content":"thinking","content":"answer"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        self.assertEqual(response_event["data"], {
            "model": "model",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"reasoning_content": "thinking", "content": "answer"},
                "finish_reason": "stop",
            }],
        })
        self.assertEqual(response_event["body_sha256"], hashlib.sha256(raw_stream).hexdigest())
        self.assertEqual(response_event["body_bytes"], len(raw_stream))


class TestLlamaCppErrorForwarding(unittest.IsolatedAsyncioTestCase):
    """Verify exact executor errors survive worker and endpoint delivery."""

    def setUp(self) -> None:
        """Create the reported context-limit error and isolated endpoint core."""
        self.payload = {"error": {
            "code": 400,
            "message": "request (132703 tokens) exceeds the available context size (130048 tokens), try increasing it",
            "type": "exceed_context_size_error",
            "n_prompt_tokens": 132703,
            "n_ctx": 130048,
        }}
        self.body = json.dumps(self.payload, indent=2).encode()
        self.core = SimpleNamespace(delete_task=AsyncMock())

    def failed_task(self, error: dict, *, stream: bool = False) -> Task_agent:
        """Return a terminal task holding a JSON-roundtripped worker error."""
        task = Task_agent(payload={"model": "model"}, stream=stream)
        task.status = STATUS_FAILED
        task.error = json.loads(json.dumps(error))
        task._done_event.set()
        task._chunk_queue.put_nowait(None)
        return task

    async def test_http_errors_survive_sync_and_stream_workers_and_endpoints(self) -> None:
        """Preserve status, content type, exact bytes, and all executor fields."""
        cases = (
            (400, self.body, "application/json"),
            (429, json.dumps({"error": {"code": None, "message": "retry later", "param": None, "type": "rate_limit_error", "extra": "x" * 2048}}).encode(), "application/json; charset=utf-8"),
            (503, b"executor unavailable\n" + b"x" * 1024, "text/plain"),
            (500, b"\xff\x00executor failure", "application/octet-stream"),
        )
        for status, body, content_type in cases:
            for stream in (False, True):
                with self.subTest(status=status, stream=stream):
                    async def respond(request: httpx.Request) -> httpx.Response:
                        """Return the configured executor failure without network access."""
                        return httpx.Response(status, content=body, headers={"content-type": content_type})

                    worker = CallLlamaCppWorker()
                    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                        if stream:
                            result = await worker._forward_stream(client, "http://executor/v1/chat/completions", {}, None)
                        else:
                            result = await worker._forward_sync(client, "http://executor/v1/chat/completions", {})
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error["code"], "UPSTREAM_ERROR")
                    if status == 400:
                        self.assertEqual(result.error["message"], self.payload["error"]["message"])

                    endpoint = Endpoint_openaix({"id": "test", "errors_compatibility_mode": False})
                    endpoint._core = self.core
                    handlers = (
                        endpoint._openai_sync_response,
                        endpoint._openai_embed_sync_response,
                        endpoint._openai_streaming_response,
                    )
                    for handler in handlers:
                        task = self.failed_task(result.error, stream=stream)
                        response = await handler(task, {})
                        self.assertEqual(response.status_code, status)
                        self.assertEqual(response.body, body)
                        self.assertEqual(response.headers["content-type"], content_type)
                    for endpoint_type in (Endpoint_ollama, Endpoint_openaix):
                        endpoint = endpoint_type({"id": "test"})
                        endpoint._core = self.core
                        for handler in (endpoint._sync_response, endpoint._streaming_response):
                            response = await handler(self.failed_task(result.error, stream=stream))
                            self.assertEqual(response.status_code, status)
                            self.assertEqual(response.body, body)

    async def test_sse_error_is_not_converted_to_success(self) -> None:
        """Return the original llama.cpp error received inside an HTTP 200 SSE stream."""
        async def respond(request: httpx.Request) -> httpx.Response:
            """Return the context-limit error as an SSE data event."""
            return httpx.Response(200, stream=httpx.ByteStream(b"data: " + self.body.replace(b"\n", b"") + b"\n\n"), headers={"content-type": "text/event-stream"})

        worker = CallLlamaCppWorker()
        emit = AsyncMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await worker._forward_stream(client, "http://executor/v1/chat/completions", {}, emit)
        self.assertFalse(result.ok)
        emit.assert_not_awaited()
        endpoint = Endpoint_openaix({"id": "test"})
        endpoint._core = self.core
        response = await endpoint._openai_streaming_response(self.failed_task(result.error, stream=True), {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), self.payload)

    async def test_late_sse_error_records_failure_and_keeps_partial_output(self) -> None:
        """Persist failed SSE diagnostics and deliver a late error after a real delta."""
        first = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        failure = b"data: " + json.dumps(self.payload).encode() + b"\n\n"

        async def respond(request: httpx.Request) -> httpx.Response:
            """Stream one successful delta followed by the executor error."""
            return httpx.Response(200, stream=httpx.ByteStream(first + failure), headers={"content-type": "text/event-stream"})

        with tempfile.TemporaryDirectory() as directory:
            worker = CallLlamaCppWorker()
            worker._core = SimpleNamespace(audit_log=AuditLog(directory))
            task = Task_agent(payload={"model": "model"}, stream=True)
            emitted = []

            async def emit(chunk: dict) -> None:
                """Capture partial output sent before the error."""
                emitted.append(chunk)

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                result = await worker._forward_stream(client, "http://executor/v1/chat/completions", {}, emit, task=task)
            self.assertFalse(result.ok)
            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0]["message"]["content"], "partial")
            self.assertEqual(task.llm_call_history[0]["status"], "http_error")
            records = [
                json.loads(line)
                for journal in worker._core.audit_log.directory.glob("raw_llm_*.jsonl")
                for line in journal.read_text().splitlines()
            ]
            event = next(record for record in records if record["type"] == "llm_response")
            self.assertEqual(event["data"], (first + failure).decode())

    def test_http_routes_forward_context_error_unchanged(self) -> None:
        """Verify real HTTP chat routes return the original error in both request modes."""
        error = build_upstream_error(400, self.body, "application/json")
        routes = (
            (Endpoint_ollama, "/api/chat"),
            (Endpoint_openaix, "/api/chat"),
            (Endpoint_openaix, "/v1/chat/completions"),
        )
        for endpoint_type, route in routes:
            for stream in (False, True):
                with self.subTest(endpoint=endpoint_type.__name__, route=route, stream=stream):
                    endpoint = endpoint_type({"id": "test"})
                    task = self.failed_task(error, stream=stream)
                    core = SimpleNamespace(
                        config=SimpleNamespace(get=lambda key, default=None: default),
                        on_task_added=AsyncMock(),
                        delete_task=AsyncMock(),
                    )
                    with patch.object(endpoint, "_build_task_for_payload_async", AsyncMock(return_value=task)):
                        with TestClient(endpoint.create_app(core)) as client:
                            response = client.post(route, json={"model": "model", "messages": [], "stream": stream})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.content, self.body)
                    self.assertEqual(response.json(), self.payload)
                    self.assertEqual(response.headers["content-type"], "application/json")

    async def test_started_streams_emit_original_error(self) -> None:
        """Forward a late executor error without adding task IDs or losing fields."""
        error = build_upstream_error(400, self.body, "application/json")
        for protocol in ("openai", "ollama"):
            with self.subTest(protocol=protocol):
                endpoint = Endpoint_openaix({"id": "test"})
                endpoint._core = self.core
                task = self.failed_task(error, stream=True)
                first = {"message": {"content": "partial"}, "done": False}
                generator = endpoint._openai_stream_response(task, {}, first_chunk=first) if protocol == "openai" else endpoint._stream_response(task, first_chunk=first)
                chunks = [chunk async for chunk in generator]
                encoded_error = chunks[1].decode().strip()
                if protocol == "openai":
                    encoded_error = encoded_error.removeprefix("data: ")
                    self.assertEqual(chunks[-1], b"data: [DONE]\n\n")
                self.assertEqual(json.loads(encoded_error), self.payload)

    async def test_completed_empty_stream_terminates(self) -> None:
        """Finish a completed stream after its initial sentinel has been consumed."""
        endpoint = Endpoint_openaix({"id": "test"})
        endpoint._core = self.core
        for protocol in ("openai", "ollama"):
            task = self.failed_task({}, stream=True)
            task.status = STATUS_COMPLETED
            response = await endpoint._openai_streaming_response(task, {}) if protocol == "openai" else await endpoint._streaming_response(task)
            chunks = [chunk async for chunk in response.body_iterator]
            self.assertEqual(chunks, [b"data: [DONE]\n\n"] if protocol == "openai" else [])

    async def test_internal_errors_keep_existing_mapping(self) -> None:
        """Keep transport and locally generated errors in their existing aidir envelopes."""
        endpoint = Endpoint_openaix({"id": "test"})
        endpoint._core = self.core
        response = await endpoint._openai_sync_response(
            self.failed_task({"code": "UPSTREAM_TIMEOUT", "message": "ReadTimeout"}), {},
        )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(json.loads(response.body)["error"]["type"], "upstream_timeout_error")


class TestLocalServerManager(unittest.IsolatedAsyncioTestCase):
    """Validate persisted-PID ownership behavior."""

    def test_llama_local_uses_dedicated_log_file(self) -> None:
        """Sends managed llama_local stdout and stderr to the WebUI-visible log file."""
        with tempfile.TemporaryDirectory() as directory:
            manager = LocalServerManager({}, Path(directory))
            self.assertEqual(manager._log_path("llama_local"), Path(directory) / "logs" / "local_llama_cpp.log")

    async def test_stop_does_not_touch_unknown_process(self) -> None:
        """Returns false when no aidir-owned PID record exists for a provider."""
        with tempfile.TemporaryDirectory() as directory:
            manager = LocalServerManager({}, Path(directory))
            self.assertFalse(await manager.stop("llama_local"))

    async def test_failed_startup_is_retained_for_status(self) -> None:
        """Exposes a local process early exit as a provider startup error."""
        config = {"models": {"providers": {"llama_local": {
            "baseUrl": "http://127.0.0.1:9",
            "exec_cmd": "/bin/false",
            "startup_timeout": 1,
        }}}}
        with tempfile.TemporaryDirectory() as directory:
            manager = LocalServerManager(config, Path(directory))
            with self.assertRaises(LocalServerError):
                await manager.ensure_running("llama_local", "model")

            error = manager.startup_error("llama_local")
            self.assertEqual(error["code"], "LLAMA_CPP_START_FAILED")
            self.assertIn("exited with code", error["message"])


class TestLlamaCppResourceRestore(unittest.TestCase):
    """Validate startup restoration of llama.cpp resource occupancy."""

    def test_restores_owned_server_as_persistent_vram_consumer(self) -> None:
        """Shows surviving owned llama.cpp memory as occupied after an aidir restart."""
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 22}, "alive_time": 300}])
        config = {"models": {"providers": {"llama_local": {
            "api": "llama-cpp",
            "models": [{"id": "model", "resources": {"gpu": {"VRAM": 22}}}],
        }}}}

        class _Manager:
            """Test double returning one verified owned server record."""

            @staticmethod
            def owned_records() -> dict:
                """Return one provider/model record retained across a restart."""
                return {"llama_local": {"model_id": "model"}}

        resources.restore_owned_llama_cpp_consumers(config, _Manager())

        snapshot = resources.get("gpu").snapshot()
        self.assertEqual(snapshot["soft_used"]["VRAM"], 22)
        self.assertTrue(snapshot["soft_consumers"][0]["persistent"])
        self.assertIsNone(snapshot["soft_consumers"][0]["expires_in"])

    def test_snapshot_includes_relevant_local_startup_error(self) -> None:
        """Shows a failed local llama.cpp startup on the model's resource."""
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 22}}])
        resources.set_full_config({"models": {"providers": {"llama_local": {
            "api": "llama-cpp",
            "models": [{"id": "model", "resources": {"gpu": {"VRAM": 22}}}],
        }}}})

        class _Manager:
            """Test double exposing a local server startup error."""

            @staticmethod
            def startup_error(provider_id: str) -> dict | None:
                """Return the failure associated with the expected provider."""
                if provider_id == "llama_local":
                    return {"code": "LLAMA_CPP_START_FAILED", "message": "exited with code 1"}
                return None

        resources.set_local_server_manager(_Manager())
        snapshot = resources.snapshot()[0]

        self.assertEqual(snapshot["startup_errors"][0]["model_id"], "model")
        self.assertEqual(snapshot["startup_errors"][0]["code"], "LLAMA_CPP_START_FAILED")


class TestLlamaCppFailedStartupRelease(unittest.IsolatedAsyncioTestCase):
    """Validate resource cleanup after llama.cpp cannot start."""

    async def test_failed_startup_does_not_create_soft_consumer(self) -> None:
        """Releases failed startup reservations without reporting a loaded model."""
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 22}, "alive_time": 300}])
        resources.set_full_config({"models": {"providers": {"llama_local": {"api": "llama-cpp"}}}})
        requirements = {"gpu": {"VRAM": 22}}

        await resources.reserve_blind_for(requirements, consumer_id="task", model_id="model", provider_id="llama_local")
        await resources.release_for(
            requirements,
            consumer_id="task",
            model_id="model",
            provider_id="llama_local",
            retain_model=False,
        )

        snapshot = resources.snapshot()[0]
        self.assertEqual(snapshot["used"]["VRAM"], 0)
        self.assertEqual(snapshot["soft_consumers"], [])


class _Config:
    """Minimal dotted configuration accessor for endpoint tests."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str, default=None):
        """Return a dotted configuration value or its default."""
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict):
                return default
            value = value.get(part)
            if value is None:
                return default
        return value


class _Core:
    """Minimal endpoint core that exposes only configuration."""

    def __init__(self, config: dict) -> None:
        self.config = _Config(config)


class TestOllamaShowEndpoint(unittest.TestCase):
    """Validate configuration-backed Ollama /api/show behavior."""

    def setUp(self) -> None:
        """Create an OpenAIx endpoint backed by a llama.cpp model configuration."""
        config = {
            "workers": {"items": {"openaix": {"provider": "ollama_local"}}},
            "models": {"providers": {
                "llama_local": {
                    "api": "llama-cpp",
                    "models": [{"id": "qwen3.8-27b", "alias": "qwen3.8-27", "contextWindow": 200000}],
                },
                "ollama_local": {"api": "ollama", "models": []},
            }},
        }
        endpoint = Endpoint_openaix({"id": "test", "worker": "openaix"})
        self.client = TestClient(endpoint.create_app(_Core(config)))

    def test_show_resolves_alias_and_returns_llama_metadata(self) -> None:
        """Returns stable metadata for a configured llama.cpp model alias."""
        response = self.client.post("/api/show", json={"name": "qwen3.8-27", "verbose": True})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["details"]["format"], "gguf")
        self.assertEqual(payload["model_info"]["aidir.provider"], "llama_local")
        self.assertEqual(payload["model_info"]["aidir.model"], "qwen3.8-27b")
        self.assertEqual(payload["model_info"]["aidir.context_window"], 200000)
        self.assertEqual(payload["model_info"]["aidir.context_length"], 200000)

    def test_show_rejects_missing_name(self) -> None:
        """Requires the standard Ollama name field."""
        response = self.client.post("/api/show", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_show_rejects_unknown_model(self) -> None:
        """Returns a model-not-found response for unconfigured names."""
        response = self.client.post("/api/show", json={"name": "missing"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "INVALID_MODEL")


if __name__ == "__main__":
    unittest.main(verbosity=2)