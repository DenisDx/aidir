"""Regression tests for OpenAIx tool arguments parsing."""

from __future__ import annotations

import json
import unittest
import base64
from unittest.mock import patch

import httpx

from core.context import Context
from core.scheduler import Scheduler
from core.worker import WorkerResult
from core.task_types.task_agent import Task_agent
from core.endpoints.endpoint_ollama import Endpoint_ollama
from core.endpoints.endpoint_openaix import Endpoint_openaix
from workers.agent.call_ollama.app import CallOllamaWorker
from workers.agent.openaix.app import OpenAIxWorker


class _FakeConfig:
    """Tiny dotted-path config stub for endpoint tests."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str, default=None):
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node


class _FakeCore:
    """Minimal core stub exposing config for endpoint tests."""

    def __init__(self, config_data: dict, *, queue=None, resources=None) -> None:
        self.config = _FakeConfig(config_data)
        self.queue = queue
        self.resources = resources


class _FakeQueue:
    """Queue stub returning a preconfigured queue-state payload."""

    def __init__(self, state: dict) -> None:
        self._state = state

    async def get_resource_queue_state(self, requirements, priority: int = 5):
        return dict(self._state)


class _FakeResources:
    """Resources stub with deterministic availability checks."""

    def __init__(self, available: bool) -> None:
        self._available = available

    def check_available(self, requirements) -> bool:
        return self._available


def _requirements_key(requirements: dict | None) -> str:
    """Serialize requirements into a stable lookup key for test doubles."""
    return json.dumps(requirements or {}, sort_keys=True, separators=(",", ":"))


class _SmartFakeQueue:
    """Queue stub that returns queue-state by normalized requirements."""

    def __init__(self, states_by_requirements: dict[str, dict]) -> None:
        self._states_by_requirements = states_by_requirements

    async def get_resource_queue_state(self, requirements, priority: int = 5):
        return dict(
            self._states_by_requirements.get(
                _requirements_key(requirements),
                {
                    "queued_count_total": 0,
                    "queued_count_below_priority": 0,
                    "priority_counts": [],
                },
            )
        )


class _SmartFakeResources:
    """Resources stub that reports availability by normalized requirements."""

    def __init__(self, availability_by_requirements: dict[str, bool]) -> None:
        self._availability_by_requirements = availability_by_requirements

    def check_available(self, requirements) -> bool:
        return bool(self._availability_by_requirements.get(_requirements_key(requirements), False))


class TestOpenAIxToolArgsParser(unittest.TestCase):
    """Validate robust parsing of tool arguments with quotes and encoded payloads."""

    def test_parse_tool_arguments_dict_passthrough(self) -> None:
        """Returns dict unchanged when arguments are already structured."""
        raw = {"message": 'He said "hello"', "count": 2}
        self.assertEqual(OpenAIxWorker._parse_tool_arguments(raw), raw)

    def test_parse_tool_arguments_json_string_with_quotes(self) -> None:
        """Parses regular JSON string containing escaped quotes."""
        raw = '{"message": "He said \\\"hello\\\"", "ok": true}'
        parsed = OpenAIxWorker._parse_tool_arguments(raw)
        self.assertEqual(parsed, {"message": 'He said "hello"', "ok": True})

    def test_parse_tool_arguments_double_encoded_json_string(self) -> None:
        """Parses JSON string that itself contains encoded JSON object string."""
        inner = {"query": 'title:"a b" AND site:example.com', "limit": 5}
        raw = json.dumps(json.dumps(inner))
        parsed = OpenAIxWorker._parse_tool_arguments(raw)
        self.assertEqual(parsed, inner)

    def test_parse_tool_arguments_python_literal_fallback(self) -> None:
        """Falls back to python literal dict parsing when JSON decoding fails."""
        raw = "{'message': 'He said \"hello\"', 'x': 1}"
        parsed = OpenAIxWorker._parse_tool_arguments(raw)
        self.assertEqual(parsed, {"message": 'He said "hello"', "x": 1})

    def test_parse_tool_arguments_invalid_string_returns_empty_dict(self) -> None:
        """Returns empty dict for non-parseable argument strings."""
        parsed = OpenAIxWorker._parse_tool_arguments("{not valid}")
        self.assertEqual(parsed, {})

    def test_extract_tool_calls_uses_robust_parser(self) -> None:
        """Extracts tool call args from double-encoded arguments string."""
        args = {"q": 'He said "hello"'}
        msg = {
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps(json.dumps(args)),
                    },
                }
            ]
        }
        calls = OpenAIxWorker._extract_tool_calls(msg)
        self.assertEqual(calls, [{"id": "c1", "name": "web_search", "arguments": args}])

    def test_normalize_assistant_message_for_history_uses_robust_parser(self) -> None:
        """Normalizes OpenAI-style tool arguments to dict for follow-up Ollama turn."""
        args = {"text": 'a "quoted" value'}
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "t1",
                    "function": {
                        "name": "echo",
                        "arguments": json.dumps(json.dumps(args)),
                    },
                }
            ],
        }
        normalized = OpenAIxWorker._normalize_assistant_message_for_history(msg)
        self.assertEqual(
            normalized,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "t1",
                        "function": {
                            "name": "echo",
                            "arguments": args,
                        },
                    }
                ],
            },
        )


class TestOpenAIxToolInjection(unittest.IsolatedAsyncioTestCase):
    """Regression checks for selective tool injection and execution ownership."""

    def test_apply_context_to_payload_injects_all_internal_tools_when_request_omits_tools(self) -> None:
        """Injects all internal tools when caller does not provide a tools field."""
        task = Task_agent(payload={"messages": []}, stream=False)
        task.context = Context.empty()
        task.context.tools = {
            "internal_echo": {
                "worker": "echo_tool",
                "description": "Echo input",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            }
        }

        OpenAIxWorker._apply_context_to_payload(task)

        self.assertEqual(task.config["injected_tool_names"], ["internal_echo"])
        self.assertEqual(
            task.payload["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "internal_echo",
                        "description": "Echo input",
                        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
                    },
                }
            ],
        )

    def test_apply_context_to_payload_replaces_only_string_tool_selectors(self) -> None:
        """Preserves caller tool objects and replaces only string selectors with injected internal tools."""
        caller_tool = {
            "type": "function",
            "function": {
                "name": "caller_tool",
                "description": "Caller supplied",
                "parameters": {"type": "object"},
            },
        }
        task = Task_agent(payload={"messages": [], "tools": [caller_tool, "internal_echo", "missing_tool"]}, stream=False)
        task.context = Context.empty()
        task.context.tools = {
            "internal_echo": {
                "worker": "echo_tool",
                "description": "Echo input",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
            "other_tool": {
                "worker": "other_tool",
                "description": "Other",
                "inputSchema": {"type": "object"},
            },
        }

        OpenAIxWorker._apply_context_to_payload(task)

        self.assertEqual(task.config["injected_tool_names"], ["internal_echo"])
        self.assertEqual(task.payload["tools"][0], caller_tool)
        self.assertEqual(task.payload["tools"][1]["function"]["name"], "internal_echo")
        self.assertEqual(len(task.payload["tools"]), 2)

    async def test_run_with_internal_tools_does_not_execute_non_injected_tool(self) -> None:
        """Passes through tool calls when the tool was caller-provided rather than injected by aidir."""
        worker = OpenAIxWorker()

        async def fake_forward_sync(client, url, payload, *, save_call=False, task_id=""):
            return WorkerResult(
                ok=True,
                data={
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "caller_tool",
                                    "arguments": {},
                                },
                            }
                        ]
                    }
                },
            )

        async def fail_execute_internal_tool(call, parent_task):
            raise AssertionError("caller-provided tool must not be executed by aidir")

        worker._forward_sync = fake_forward_sync
        worker._execute_internal_tool = fail_execute_internal_tool

        parent_task = Task_agent(
            payload={
                "messages": [{"role": "user", "content": "Hi"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "caller_tool",
                            "description": "Caller supplied",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
            stream=False,
        )
        parent_task.context = Context.empty()
        parent_task.context.tools = {
            "caller_tool": {
                "worker": "echo_tool",
                "description": "Internal collision",
                "inputSchema": {"type": "object"},
            }
        }
        parent_task.config = {"injected_tool_names": []}

        result = await worker._run_with_internal_tools(
            client=None,
            url="http://example.test/api/chat",
            payload=parent_task.payload,
            parent_task=parent_task,
            emit_chunk=None,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["message"]["tool_calls"][0]["function"]["name"], "caller_tool")


class TestOpenAIxTimeoutBehavior(unittest.TestCase):
    """Regression checks for timeout behavior in OpenAIx worker."""

    def test_resolve_upstream_timeout_prefers_task_run_timeout(self) -> None:
        """Uses task.run_timeout so upstream timeout matches task execution budget."""
        worker = OpenAIxWorker()
        worker._timeout = 100
        task = Task_agent(payload={}, stream=False)
        task.run_timeout = 300

        self.assertEqual(worker._resolve_upstream_timeout(task), 300)

    def test_resolve_upstream_timeout_falls_back_to_worker_timeout(self) -> None:
        """Uses worker timeout when task timeout is not set."""
        worker = OpenAIxWorker()
        worker._timeout = 45
        task = Task_agent(payload={}, stream=False)
        task.run_timeout = 0

        self.assertEqual(worker._resolve_upstream_timeout(task), 45)

    def test_build_timeout_message_non_empty_for_blank_exception(self) -> None:
        """Produces diagnostic text even when timeout exception string is empty."""
        request = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
        exc = httpx.ReadTimeout("", request=request)

        self.assertEqual(
            OpenAIxWorker._build_timeout_message(exc),
            "ReadTimeout: POST http://127.0.0.1:11434/api/chat",
        )


class TestWorkerWarningLogs(unittest.IsolatedAsyncioTestCase):
    """Regression checks for warning-level logs on model/upstream failures."""

    async def test_call_ollama_execute_logs_unreachable_as_warning(self) -> None:
        """Logs upstream reachability failures as warning on the plain Ollama worker."""
        worker = CallOllamaWorker()
        worker._base_url = "http://127.0.0.1:11434"
        task = Task_agent(payload={"model": "qwen3.5:9b", "messages": []}, stream=False)
        request = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")

        async def raise_connect(client, url, payload, task_obj, save_call=False):
            raise httpx.ConnectError("connection refused", request=request)

        worker._forward_sync = raise_connect
        records: list[tuple[str, str, str, str | None]] = []

        with patch("workers.agent.call_ollama.app.log", side_effect=lambda kind, level, message, tag=None: records.append((kind, level, message, tag))):
            result = await worker.execute(task)

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "UPSTREAM_UNREACHABLE")
        self.assertTrue(any(level == "warning" and "unreachable" in message.lower() for _, level, message, _ in records))

    async def test_call_ollama_forward_sync_logs_upstream_http_error_as_warning(self) -> None:
        """Logs model HTTP refusal as warning on the plain Ollama worker."""
        worker = CallOllamaWorker()
        task = Task_agent(payload={"model": "qwen3.5:9b", "messages": []}, stream=False)

        class FakeResponse:
            status_code = 503
            text = "model is busy"

        class FakeClient:
            async def post(self, url, json):
                return FakeResponse()

        records: list[tuple[str, str, str, str | None]] = []
        with patch("workers.agent.call_ollama.app.log", side_effect=lambda kind, level, message, tag=None: records.append((kind, level, message, tag))):
            result = await worker._forward_sync(FakeClient(), "http://127.0.0.1:11434/api/chat", {"model": "qwen3.5:9b"}, task)

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "UPSTREAM_ERROR")
        self.assertTrue(any(level == "warning" and "status=503" in message for _, level, message, _ in records))

    async def test_openaix_execute_logs_timeout_as_warning(self) -> None:
        """Logs upstream timeout as warning on the OpenAIx worker."""
        worker = OpenAIxWorker()
        worker._base_url = "http://127.0.0.1:11434"
        task = Task_agent(payload={"model": "qwen3.5:9b", "messages": [{"role": "user", "content": "Hi"}]}, stream=False)
        request = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")

        async def ok_context(task_obj):
            return WorkerResult(ok=True, data={})

        async def raise_timeout(client, url, payload, parent_task, emit_chunk, save_call=False):
            raise httpx.ReadTimeout("", request=request)

        worker._apply_context_chain = ok_context
        worker._run_with_internal_tools = raise_timeout
        records: list[tuple[str, str, str, str | None]] = []

        with patch("workers.agent.openaix.app.log", side_effect=lambda kind, level, message, tag=None: records.append((kind, level, message, tag))):
            result = await worker.execute(task)

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "UPSTREAM_TIMEOUT")
        self.assertTrue(any(level == "warning" and "timeout" in message.lower() for _, level, message, _ in records))

    async def test_openaix_forward_stream_logs_upstream_http_error_as_warning(self) -> None:
        """Logs streaming model HTTP refusal as warning on the OpenAIx worker."""
        worker = OpenAIxWorker()

        class FakeStreamResponse:
            status_code = 502

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeClient:
            def stream(self, method, url, json):
                return FakeStreamResponse()

        records: list[tuple[str, str, str, str | None]] = []
        with patch("workers.agent.openaix.app.log", side_effect=lambda kind, level, message, tag=None: records.append((kind, level, message, tag))):
            result = await worker._forward_stream(FakeClient(), "http://127.0.0.1:11434/api/chat", {"model": "qwen3.5:9b"}, None, task_id="task-1")

        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "UPSTREAM_ERROR")
        self.assertTrue(any(level == "warning" and "status=502" in message for _, level, message, _ in records))


class TestOpenAIxAuthHeaders(unittest.IsolatedAsyncioTestCase):
    """Regression checks for provider auth and incoming bearer precedence."""

    def test_worker_prefers_provider_auth_over_incoming_bearer(self) -> None:
        """Uses provider auth config for routed inference requests when present."""
        worker = OpenAIxWorker()
        worker._core = _FakeCore(
            {
                "models": {
                    "providers": {
                        "remote_aidir": {
                            "auth": {
                                "type": "basic",
                                "username": "alice",
                                "password": "secret",
                            }
                        }
                    }
                }
            }
        )
        task = Task_agent(payload={}, stream=False)
        task.config = {"incoming_bearer_token": "incoming-token"}

        headers = worker._resolve_request_headers(task, "remote_aidir")

        expected = base64.b64encode(b"alice:secret").decode("ascii")
        self.assertEqual(headers, {"Authorization": f"Basic {expected}"})

    def test_worker_falls_back_to_incoming_bearer_when_provider_auth_missing(self) -> None:
        """Passes through incoming bearer token when provider auth config is absent."""
        worker = OpenAIxWorker()
        worker._core = _FakeCore({"models": {"providers": {"remote_aidir": {}}}})
        task = Task_agent(payload={}, stream=False)
        task.config = {"incoming_bearer_token": "incoming-token"}

        headers = worker._resolve_request_headers(task, "remote_aidir")

        self.assertEqual(headers, {"Authorization": "Bearer incoming-token"})

    async def test_build_task_for_payload_async_persists_incoming_bearer_token(self) -> None:
        """Stores incoming bearer token in task config for later probe/inference auth decisions."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {"openaix": {"provider": "ollama_local"}},
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
                "models": {
                    "providers": {
                        "ollama_local": {
                            "api": "ollama",
                            "models": [{"id": "qwen3.5:9b"}],
                        }
                    }
                },
            }
        )

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "qwen3.5:9b",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
            incoming_bearer_token="incoming-token",
            route_trace={"route_id": "route-1", "visited_instances": ["node-a"], "current_instance": "node-b", "max_hops": 8},
        )

        self.assertEqual(task.config["incoming_bearer_token"], "incoming-token")
        self.assertEqual(task.config["route_trace"]["route_id"], "route-1")

    def test_endpoint_probe_headers_prefer_provider_auth_over_incoming_bearer(self) -> None:
        """Uses provider auth config for remote smart probes when present."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "models": {
                    "providers": {
                        "remote_aidir": {
                            "auth": {
                                "type": "bearer",
                                "token": "provider-token",
                            }
                        }
                    }
                }
            }
        )

        headers = endpoint._resolve_probe_headers("remote_aidir", "incoming-token")

        self.assertEqual(headers, {"Authorization": "Bearer provider-token"})


class TestAidirRouteTrace(unittest.TestCase):
    """Regression checks for inter-aidir route tracing and loop protection."""

    def test_validate_incoming_route_trace_rejects_loop(self) -> None:
        """Rejects requests that already visited the current instance."""
        endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})

        error = endpoint._validate_incoming_route_trace(
            {
                "route_id": "route-1",
                "visited_instances": ["node-a", "node-b"],
                "current_instance": "node-b",
                "max_hops": 8,
            }
        )

        self.assertEqual(error, ("ROUTING_LOOP", 409, "Routing loop detected for instance 'node-b'"))

    def test_validate_incoming_route_trace_rejects_hop_limit(self) -> None:
        """Rejects requests whose visited-instance chain already reached the hop limit."""
        endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})

        error = endpoint._validate_incoming_route_trace(
            {
                "route_id": "route-1",
                "visited_instances": ["a", "b", "c"],
                "current_instance": "d",
                "max_hops": 3,
            }
        )

        self.assertEqual(error, ("ROUTING_HOPS_EXCEEDED", 409, "Routing hop limit exceeded (3)"))

    def test_worker_adds_route_trace_headers_for_remote_openaix(self) -> None:
        """Appends the current instance to outgoing trace headers for remote OpenAIx hops."""
        worker = OpenAIxWorker()
        worker._core = _FakeCore(
            {
                "instance": "node-b",
                "models": {
                    "providers": {
                        "remote_aidir": {
                            "api": "openaix",
                        }
                    }
                },
            }
        )
        task = Task_agent(payload={}, stream=False)
        task.config = {
            "route_trace": {
                "route_id": "route-1",
                "visited_instances": ["node-a"],
            }
        }

        headers = worker._resolve_request_headers(task, "remote_aidir")

        self.assertEqual(headers["X-Aidir-Route-Id"], "route-1")
        self.assertEqual(headers["X-Aidir-Visited-Instances"], "node-a, node-b")


class TestOpenAIxGenerationParameters(unittest.TestCase):
    """Regression checks for generation parameter handling across endpoint and worker."""

    def test_openai_request_mapping_accepts_repetition_penalty(self) -> None:
        """Maps supported generation fields from OpenAI request into internal OpenAIx payload."""
        payload = Endpoint_openaix._openai_request_to_ollama(
            {
                "model": "qwen3",
                "messages": [{"role": "user", "content": "Hi"}],
                "temperature": 0.2,
                "top_p": 0.8,
                "repetition_penalty": 1.15,
                "repeat_last_n": 96,
                "num_predict": 222,
                "max_tokens": 256,
                "seed": 7,
                "presence_penalty": 0.4,
                "frequency_penalty": 0.5,
                "top_k": 33,
                "min_p": 0.06,
                "options": {"seed": 7},
            }
        )

        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["top_p"], 0.8)
        self.assertEqual(payload["repetition_penalty"], 1.15)
        self.assertEqual(payload["repeat_last_n"], 96)
        self.assertEqual(payload["num_predict"], 222)
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(payload["seed"], 7)
        self.assertEqual(payload["presence_penalty"], 0.4)
        self.assertEqual(payload["frequency_penalty"], 0.5)
        self.assertEqual(payload["top_k"], 33)
        self.assertEqual(payload["min_p"], 0.06)
        self.assertEqual(payload["options"], {"seed": 7})

    def test_openai_request_mapping_accepts_ollama_generation_aliases(self) -> None:
        """Passes through direct Ollama-style generation field names when caller uses them top-level."""
        payload = Endpoint_openaix._openai_request_to_ollama(
            {
                "model": "qwen3",
                "messages": [{"role": "user", "content": "Hi"}],
                "repeat_penalty": 1.07,
                "repeat_last_n": 72,
                "num_predict": 333,
            }
        )

        self.assertEqual(payload["repeat_penalty"], 1.07)
        self.assertEqual(payload["repeat_last_n"], 72)
        self.assertEqual(payload["num_predict"], 333)

    def test_build_task_for_payload_applies_worker_generation_defaults(self) -> None:
        """Stores per-worker generation defaults in task payload so they are persisted with the task."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "generation_defaults": {
                                "temperature": 0.55,
                                "top_p": 0.91,
                                "repetition_penalty": 1.2,
                                "repeat_last_n": 64,
                                "max_tokens": 777,
                                "seed": 123,
                                "presence_penalty": 0.25,
                                "frequency_penalty": 0.35,
                                "top_k": 44,
                                "min_p": 0.02,
                            }
                        }
                    },
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
            }
        )

        task = endpoint._build_task_for_payload(
            {
                "model": "qwen3",
                "messages": [{"role": "user", "content": "Hi"}],
                "top_p": 0.7,
            },
            stream=False,
        )

        self.assertEqual(task.payload["temperature"], 0.55)
        self.assertEqual(task.payload["top_p"], 0.7)
        self.assertEqual(task.payload["repetition_penalty"], 1.2)
        self.assertEqual(task.payload["repeat_last_n"], 64)
        self.assertEqual(task.payload["max_tokens"], 777)
        self.assertEqual(task.payload["seed"], 123)
        self.assertEqual(task.payload["presence_penalty"], 0.25)
        self.assertEqual(task.payload["frequency_penalty"], 0.35)
        self.assertEqual(task.payload["top_k"], 44)
        self.assertEqual(task.payload["min_p"], 0.02)

    def test_build_task_for_payload_applies_direct_ollama_generation_defaults(self) -> None:
        """Accepts direct Ollama option names in generation_defaults when aliases exist."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "generation_defaults": {
                                "repeat_penalty": 1.09,
                                "repeat_last_n": 80,
                                "num_predict": 888,
                            }
                        }
                    },
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
            }
        )

        task = endpoint._build_task_for_payload(
            {
                "model": "qwen3",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
        )

        self.assertEqual(task.payload["repeat_penalty"], 1.09)
        self.assertEqual(task.payload["repeat_last_n"], 80)
        self.assertEqual(task.payload["num_predict"], 888)

    def test_normalize_payload_maps_openaix_generation_fields_to_ollama_options(self) -> None:
        """Converts OpenAI/OpenAIx generation aliases into Ollama options names."""
        normalized = OpenAIxWorker._normalize_payload(
            {
                "model": "qwen3",
                "messages": [{"role": "user", "content": "Hi"}],
                "temperature": 0.33,
                "top_p": 0.88,
                "repeat_penalty": 1.19,
                "repetition_penalty": 1.17,
                "repeat_last_n": 48,
                "num_predict": 321,
                "max_tokens": 123,
                "seed": 77,
                "presence_penalty": 0.45,
                "frequency_penalty": 0.55,
                "top_k": 27,
                "min_p": 0.04,
                "options": {"repeat_penalty": 1.05, "seed": 9},
            },
            stream=False,
        )

        self.assertEqual(
            normalized.get("options"),
            {
                "temperature": 0.33,
                "top_p": 0.88,
                "repeat_penalty": 1.05,
                "repeat_last_n": 48,
                "num_predict": 321,
                "seed": 9,
                "presence_penalty": 0.45,
                "frequency_penalty": 0.55,
                "top_k": 27,
                "min_p": 0.04,
            },
        )

    def test_openaix_worker_applies_model_generation_defaults(self) -> None:
        """Uses model-level generation config when request does not override it."""
        worker = OpenAIxWorker()
        worker._provider_id = "ollama_local"
        worker._core = _FakeCore(
            {
                "models": {
                    "providers": {
                        "ollama_local": {
                            "models": [
                                {
                                    "id": "qwen3",
                                    "temperature": 0.4,
                                    "top_p": 0.77,
                                    "repeat_last_n": 32,
                                    "num_predict": 256,
                                }
                            ]
                        }
                    }
                }
            }
        )

        payload = worker._apply_model_generation_defaults(
            {
                "model": "qwen3",
                "messages": [{"role": "user", "content": "Hi"}],
                "temperature": 0.9,
                "max_tokens": 111,
            },
            provider_id="ollama_local",
        )

        self.assertEqual(payload["temperature"], 0.9)
        self.assertEqual(payload["max_tokens"], 111)
        self.assertEqual(payload["top_p"], 0.77)
        self.assertEqual(payload["repeat_last_n"], 32)
        self.assertNotIn("num_predict", payload)

    def test_call_ollama_worker_applies_model_generation_defaults(self) -> None:
        """Uses model-level generation config as Ollama options unless request overrides it."""
        worker = CallOllamaWorker()
        worker._provider_id = "ollama_local"
        worker._core = _FakeCore(
            {
                "models": {
                    "providers": {
                        "ollama_local": {
                            "models": [
                                {
                                    "id": "qwen3",
                                    "temperature": 0.4,
                                    "top_p": 0.77,
                                    "repeat_last_n": 32,
                                    "num_predict": 256,
                                }
                            ]
                        }
                    }
                }
            }
        )

        payload = worker._apply_model_generation_defaults(
            {
                "model": "qwen3",
                "options": {"temperature": 0.9, "num_predict": 111},
            },
            provider_id="ollama_local",
        )

        self.assertEqual(
            payload["options"],
            {
                "temperature": 0.9,
                "num_predict": 111,
                "top_p": 0.77,
                "repeat_last_n": 32,
            },
        )


class TestOpenAIxModelRouting(unittest.TestCase):
    """Regression checks for alias-based provider/model resolution."""

    def test_build_task_for_payload_prefers_worker_provider_for_duplicate_model_ids(self) -> None:
        """Uses worker provider as priority provider when several providers expose the same model id."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "ollama_local",
                        }
                    },
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
                "models": {
                    "providers": {
                        "ollama_remote": {
                            "models": [
                                {"id": "qwen3.5:9b", "resources": {"remote": {"VRAM": 9000}}},
                            ]
                        },
                        "ollama_local": {
                            "models": [
                                {"id": "qwen3.5:9b", "resources": {"local": {"VRAM": 16000}}},
                            ]
                        },
                    }
                },
            }
        )

        task = endpoint._build_task_for_payload(
            {
                "model": "qwen3.5:9b",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "qwen3.5:9b")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_local")
        self.assertEqual(task.config["route"]["resolved_model"], "qwen3.5:9b")

    def test_build_task_for_payload_resolves_alias_to_concrete_provider_and_model(self) -> None:
        """Resolves explicit alias before raw model ids and stores concrete route metadata."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "ollama_local",
                        }
                    },
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
                "models": {
                    "providers": {
                        "ollama_local": {
                            "models": [
                                {"id": "qwen3.5:9b"},
                            ]
                        },
                        "ollama_remote": {
                            "models": [
                                {"id": "fred-large", "alias": "smart_chat"},
                            ]
                        },
                    }
                },
            }
        )

        task = endpoint._build_task_for_payload(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "fred-large")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_remote")
        self.assertEqual(task.config["route"]["resolved_model"], "fred-large")
        self.assertEqual(task.config["route"]["selection"], "alias")

    def test_collect_models_exposes_alias_when_present(self) -> None:
        """Lists alias as the external model id while keeping unaliased ids visible."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "models": {
                    "providers": {
                        "ollama_local": {
                            "models": [
                                {"id": "qwen3.5:9b", "alias": "smart_chat"},
                                {"id": "qwen3.5:0.8b"},
                            ]
                        },
                        "ollama_remote": {
                            "models": [
                                {"id": "qwen3.5:9b"},
                            ]
                        },
                    }
                }
            }
        )

        self.assertEqual(endpoint._collect_models(), ["smart_chat", "qwen3.5:0.8b", "qwen3.5:9b"])

    def test_openai_models_response_includes_real_id_for_alias(self) -> None:
        """Lists callable external id and resolved internal id in the OpenAI models response."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "ollama_remote",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "ollama_local": {
                            "models": [
                                {"id": "qwen3.5:9b", "alias": "smart_chat"},
                            ]
                        },
                        "ollama_remote": {
                            "models": [
                                {"id": "fred-large", "alias": "smart_chat"},
                            ]
                        },
                    }
                },
            }
        )

        response = endpoint._openai_models_response()

        self.assertEqual(
            response["data"],
            [
                {
                    "id": "smart_chat",
                    "real_id": "fred-large",
                    "object": "model",
                    "created": response["data"][0]["created"],
                    "owned_by": "aidir",
                }
            ],
        )

    def test_ollama_tags_response_includes_real_id_for_alias(self) -> None:
        """Lists callable external id and resolved internal id in the Ollama tags response."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "ollama_remote",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "ollama_local": {
                            "models": [
                                {"id": "qwen3.5:9b", "alias": "smart_chat"},
                            ]
                        },
                        "ollama_remote": {
                            "models": [
                                {"id": "fred-large", "alias": "smart_chat"},
                            ]
                        },
                    }
                },
            }
        )

        response = endpoint._ollama_tags_response()

        self.assertEqual(response["models"][0]["name"], "smart_chat")
        self.assertEqual(response["models"][0]["model"], "smart_chat")
        self.assertEqual(response["models"][0]["real_id"], "fred-large")

    def test_worker_uses_task_route_provider_override(self) -> None:
        """Uses resolved provider override for base URL and model context lookup."""
        worker = OpenAIxWorker()
        worker._provider_id = "ollama_local"
        worker._base_url = "http://127.0.0.1:11434"
        worker._core = _FakeCore(
            {
                "models": {
                    "providers": {
                        "ollama_local": {
                            "baseUrl": "http://127.0.0.1:11434",
                            "models": [{"id": "qwen3.5:9b", "contextWindow": 128000}],
                        },
                        "ollama_remote": {
                            "baseUrl": "http://10.0.0.2:21434",
                            "models": [{"id": "fred-large", "contextWindow": 64000}],
                        },
                    }
                }
            }
        )
        task = Task_agent(payload={"model": "fred-large"}, stream=False)
        task.config = {"route": {"resolved_provider": "ollama_remote", "resolved_model": "fred-large"}}

        self.assertEqual(worker._resolve_task_provider_id(task), "ollama_remote")
        self.assertEqual(worker._resolve_base_url("ollama_remote"), "http://10.0.0.2:21434")
        self.assertEqual(worker._resolve_model_context_window("fred-large", provider_id="ollama_remote"), 64000)

    def test_scheduler_resource_resolution_prefers_task_route_provider(self) -> None:
        """Resolves resource requirements from the routed provider instead of worker default provider."""
        scheduler = Scheduler(
            queue=None,
            workers={},
            workers_cfg={"openaix": {"provider": "ollama_local"}},
            resources=None,
            full_config={
                "models": {
                    "providers": {
                        "ollama_local": {
                            "models": [{"id": "qwen3.5:9b", "resources": {"local": {"VRAM": 16000}}}],
                        },
                        "ollama_remote": {
                            "models": [{"id": "qwen3.5:9b", "resources": {"remote": {"VRAM": 9000}}}],
                        },
                    }
                }
            },
        )
        task = Task_agent(payload={"model": "qwen3.5:9b"}, stream=False)
        task.config = {"route": {"resolved_provider": "ollama_remote", "resolved_model": "qwen3.5:9b"}}

        self.assertEqual(
            scheduler._resolve_resource_requirements(task, "openaix"),
            {"remote": {"VRAM": 9000}},
        )


class TestOpenAIxModelOnlyQueueState(unittest.IsolatedAsyncioTestCase):
    """Regression checks for alias-aware model-only queue-state endpoints."""

    async def test_model_only_queue_state_resolves_alias_to_concrete_provider_and_model(self) -> None:
        """Uses the same alias-aware route resolution as inference before computing queue state."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "ollama_local",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "ollama_local": {
                            "models": [
                                {"id": "qwen3.5:9b", "resources": {"local": {"VRAM": 16000}}},
                            ]
                        },
                        "ollama_remote": {
                            "models": [
                                {"id": "fred-large", "alias": "smart_chat", "resources": {"remote": {"VRAM": 9000}}},
                            ]
                        },
                    }
                },
            },
            queue=_FakeQueue(
                {
                    "queued_count_total": 2,
                    "queued_count_below_priority": 1,
                    "priority_counts": [
                        {"priority": 5, "count": 1},
                        {"priority": 10, "count": 1},
                    ],
                }
            ),
            resources=_FakeResources(True),
        )

        response = await endpoint._model_only_queue_state_response(
            protocol="openai",
            model_id="smart_chat",
            priority=5,
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["requested_model"], "smart_chat")
        self.assertEqual(payload["provider"], "ollama_remote")
        self.assertEqual(payload["model"], "fred-large")
        self.assertFalse(payload["can_run_now"])

    async def test_model_only_queue_state_prefers_worker_provider_for_duplicate_ids(self) -> None:
        """Matches inference provider preference when several providers expose the same external model id."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "ollama_local",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "ollama_remote": {
                            "models": [
                                {"id": "qwen3.5:9b", "resources": {"remote": {"VRAM": 9000}}},
                            ]
                        },
                        "ollama_local": {
                            "models": [
                                {"id": "qwen3.5:9b", "resources": {"local": {"VRAM": 16000}}},
                            ]
                        },
                    }
                },
            },
            queue=_FakeQueue(
                {
                    "queued_count_total": 0,
                    "queued_count_below_priority": 0,
                    "priority_counts": [],
                }
            ),
            resources=_FakeResources(True),
        )

        response = await endpoint._model_only_queue_state_response(
            protocol="ollama",
            model_id="qwen3.5:9b",
            priority=5,
        )

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["provider"], "ollama_local")
        self.assertEqual(payload["model"], "qwen3.5:9b")
        self.assertTrue(payload["can_run_now"])


class TestOpenAIxSmartRouting(unittest.IsolatedAsyncioTestCase):
    """Regression checks for initial smart-provider first_available routing."""

    async def test_build_task_for_payload_async_resolves_first_immediate_candidate(self) -> None:
        """Selects the first candidate that can run immediately and materializes a concrete route."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "smart",
                        }
                    },
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "alias": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "ollama_busy", "model": "slow-model", "fallback_prio": 5},
                                        {"provider": "ollama_ready", "model": "fast-model", "fallback_prio": 10},
                                    ],
                                }
                            ],
                        },
                        "ollama_busy": {
                            "api": "ollama",
                            "models": [
                                {"id": "slow-model", "resources": {"busy": {"VRAM": 8000}}},
                            ],
                        },
                        "ollama_ready": {
                            "api": "ollama",
                            "models": [
                                {"id": "fast-model", "resources": {"ready": {"VRAM": 4000}}},
                            ],
                        },
                    }
                },
            },
            queue=_SmartFakeQueue(
                {
                    '{"busy":{"VRAM":8000}}': {
                        "queued_count_total": 1,
                        "queued_count_below_priority": 0,
                        "priority_counts": [{"priority": 5, "count": 1}],
                    },
                    '{"ready":{"VRAM":4000}}': {
                        "queued_count_total": 0,
                        "queued_count_below_priority": 0,
                        "priority_counts": [],
                    },
                }
            ),
            resources=_SmartFakeResources({
                '{"busy":{"VRAM":8000}}': True,
                '{"ready":{"VRAM":4000}}': True,
            }),
        )

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return True

        endpoint._probe_ollama_model_availability = fake_ollama_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
                "priority": 5,
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "fast-model")
        self.assertEqual(task.config["route"]["requested_provider"], "smart")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_ready")
        self.assertEqual(task.config["route"]["resolved_model"], "fast-model")
        self.assertEqual(task.config["route"]["resolved_worker"], "openaix")
        self.assertEqual(task.config["route"]["selection_reason"], "immediate")
        self.assertEqual(task.config["route"]["candidate_index"], 1)

    async def test_build_task_for_payload_async_uses_busy_fallback_priority(self) -> None:
        """Chooses the lowest fallback_prio among responsive busy candidates when none can run now."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "smart",
                        }
                    },
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "ollama_a", "model": "model-a", "fallback_prio": 20},
                                        {"provider": "ollama_b", "model": "model-b", "fallback_prio": 3},
                                    ],
                                }
                            ],
                        },
                        "ollama_a": {
                            "api": "ollama",
                            "models": [
                                {"id": "model-a", "resources": {"res_a": {"VRAM": 8000}}},
                            ],
                        },
                        "ollama_b": {
                            "api": "ollama",
                            "models": [
                                {"id": "model-b", "resources": {"res_b": {"VRAM": 8000}}},
                            ],
                        },
                    }
                },
            },
            queue=_SmartFakeQueue(
                {
                    '{"res_a":{"VRAM":8000}}': {
                        "queued_count_total": 1,
                        "queued_count_below_priority": 0,
                        "priority_counts": [{"priority": 5, "count": 1}],
                    },
                    '{"res_b":{"VRAM":8000}}': {
                        "queued_count_total": 2,
                        "queued_count_below_priority": 0,
                        "priority_counts": [{"priority": 5, "count": 1}, {"priority": 10, "count": 1}],
                    },
                }
            ),
            resources=_SmartFakeResources({
                '{"res_a":{"VRAM":8000}}': True,
                '{"res_b":{"VRAM":8000}}': True,
            }),
        )

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return True

        endpoint._probe_ollama_model_availability = fake_ollama_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
                "priority": 5,
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "model-b")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_b")
        self.assertEqual(task.config["route"]["selection_reason"], "busy_fallback")
        self.assertEqual(task.config["route"]["fallback_prio"], 3)

    async def test_build_task_for_payload_async_skips_failed_probe_candidate(self) -> None:
        """Skips failed remote probe candidates and continues with the next responsive option."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "smart",
                        }
                    },
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "remote_bad", "model": "smart_chat", "fallback_prio": 1},
                                        {"provider": "remote_ok", "model": "smart_chat", "fallback_prio": 2},
                                    ],
                                }
                            ],
                        },
                        "remote_bad": {
                            "api": "openaix",
                            "baseUrl": "http://bad.example:21434",
                            "models": [{"id": "smart_chat"}],
                        },
                        "remote_ok": {
                            "api": "openaix",
                            "baseUrl": "http://ok.example:21434",
                            "models": [{"id": "smart_chat"}],
                        },
                    }
                },
            },
            queue=None,
            resources=None,
        )

        async def fake_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            self.assertEqual(model_id, "smart_chat")
            self.assertEqual(priority, 5)
            if provider_id == "remote_bad":
                return None
            return {
                "can_run_now": True,
                "queued_count_total": 0,
                "queued_count_below_priority": 0,
                "priority_counts": [],
            }

        endpoint._probe_remote_model_queue_state = fake_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
        )

        self.assertEqual(task.config["route"]["resolved_provider"], "remote_ok")
        self.assertEqual(task.config["route"]["candidate_index"], 1)
        self.assertEqual(task.config["route"]["candidate_probes"][0]["probe_error"], "probe_failed")

    async def test_build_task_for_payload_async_preserves_order_for_equal_fallback_priority(self) -> None:
        """Keeps config order when busy candidates have the same fallback_prio."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "smart",
                        }
                    },
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "ollama_a", "model": "model-a", "fallback_prio": 7},
                                        {"provider": "ollama_b", "model": "model-b", "fallback_prio": 7},
                                    ],
                                }
                            ],
                        },
                        "ollama_a": {
                            "api": "ollama",
                            "models": [
                                {"id": "model-a", "resources": {"res_a": {"VRAM": 8000}}},
                            ],
                        },
                        "ollama_b": {
                            "api": "ollama",
                            "models": [
                                {"id": "model-b", "resources": {"res_b": {"VRAM": 8000}}},
                            ],
                        },
                    }
                },
            },
            queue=_SmartFakeQueue(
                {
                    '{"res_a":{"VRAM":8000}}': {
                        "queued_count_total": 1,
                        "queued_count_below_priority": 0,
                        "priority_counts": [{"priority": 5, "count": 1}],
                    },
                    '{"res_b":{"VRAM":8000}}': {
                        "queued_count_total": 1,
                        "queued_count_below_priority": 0,
                        "priority_counts": [{"priority": 5, "count": 1}],
                    },
                }
            ),
            resources=_SmartFakeResources({
                '{"res_a":{"VRAM":8000}}': True,
                '{"res_b":{"VRAM":8000}}': True,
            }),
        )

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return True

        endpoint._probe_ollama_model_availability = fake_ollama_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
                "priority": 5,
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "model-a")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_a")
        self.assertEqual(task.config["route"]["candidate_index"], 0)

    async def test_build_task_for_payload_async_can_use_remote_openaix_probe(self) -> None:
        """Uses remote model-only queue-state probe when candidate schedulability is not locally visible."""
        endpoint = Endpoint_openaix({"id": "openaix", "worker": "openaix"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "openaix",
                    "items": {
                        "openaix": {
                            "provider": "smart",
                        }
                    },
                },
                "tasks": {"queue_timeout": 300, "run_timeout": 300},
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "remote_aidir", "model": "smart_chat", "fallback_prio": 1},
                                    ],
                                }
                            ],
                        },
                        "remote_aidir": {
                            "api": "openaix",
                            "baseUrl": "http://remote.example:21434",
                            "models": [
                                {"id": "smart_chat"},
                            ],
                        },
                    }
                },
            },
            queue=None,
            resources=None,
        )

        async def fake_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            self.assertEqual(provider_id, "remote_aidir")
            self.assertEqual(model_id, "smart_chat")
            self.assertEqual(priority, 5)
            self.assertEqual(incoming_bearer_token, "incoming-token")
            return {
                "can_run_now": True,
                "queued_count_total": 0,
                "queued_count_below_priority": 0,
                "priority_counts": [],
            }

        endpoint._probe_remote_model_queue_state = fake_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
            incoming_bearer_token="incoming-token",
        )

        self.assertEqual(task.payload["model"], "smart_chat")
        self.assertEqual(task.config["route"]["resolved_provider"], "remote_aidir")
        self.assertEqual(task.config["route"]["selection_reason"], "immediate")


class TestOllamaModelRouting(unittest.TestCase):
    """Regression checks for alias-based provider/model resolution on the Ollama path."""

    def test_build_task_for_payload_prefers_worker_provider_for_duplicate_model_ids(self) -> None:
        """Uses worker provider as priority provider when several providers expose the same model id."""
        endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "call_ollama",
                    "items": {
                        "call_ollama": {
                            "provider": "ollama_local",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "ollama_remote": {
                            "models": [
                                {"id": "qwen3.5:9b"},
                            ]
                        },
                        "ollama_local": {
                            "models": [
                                {"id": "qwen3.5:9b"},
                            ]
                        },
                    }
                },
            }
        )

        task = endpoint._build_task_for_payload(
            {
                "model": "qwen3.5:9b",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "qwen3.5:9b")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_local")

    def test_build_task_for_payload_resolves_alias_to_concrete_provider_and_model(self) -> None:
        """Resolves explicit alias before raw model ids and stores concrete route metadata."""
        endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "call_ollama",
                    "items": {
                        "call_ollama": {
                            "provider": "ollama_local",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "ollama_local": {
                            "models": [
                                {"id": "qwen3.5:9b"},
                            ]
                        },
                        "ollama_remote": {
                            "models": [
                                {"id": "fred-large", "alias": "smart_chat"},
                            ]
                        },
                    }
                },
            }
        )

        task = endpoint._build_task_for_payload(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "fred-large")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_remote")
        self.assertEqual(task.config["route"]["resolved_model"], "fred-large")

    def test_call_ollama_worker_uses_task_route_provider_override(self) -> None:
        """Uses resolved provider override for base URL and model context lookup."""
        worker = CallOllamaWorker()
        worker._provider_id = "ollama_local"
        worker._base_url = "http://127.0.0.1:11434"
        worker._core = _FakeCore(
            {
                "models": {
                    "providers": {
                        "ollama_local": {
                            "baseUrl": "http://127.0.0.1:11434",
                            "models": [{"id": "qwen3.5:9b", "contextWindow": 128000}],
                        },
                        "ollama_remote": {
                            "baseUrl": "http://10.0.0.3:11434",
                            "models": [{"id": "fred-large", "contextWindow": 64000}],
                        },
                    }
                }
            }
        )
        task = Task_agent(payload={"model": "fred-large"}, stream=False)
        task.config = {"route": {"resolved_provider": "ollama_remote", "resolved_model": "fred-large"}}

        self.assertEqual(worker._resolve_task_provider_id(task), "ollama_remote")
        self.assertEqual(worker._resolve_base_url("ollama_remote"), "http://10.0.0.3:11434")
        self.assertEqual(worker._resolve_model_context_window("fred-large", provider_id="ollama_remote"), 64000)


class TestOllamaSmartRouting(unittest.IsolatedAsyncioTestCase):
    """Regression checks for initial smart-provider first_available routing on the Ollama path."""

    async def test_build_task_for_payload_async_skips_unreachable_ollama_candidate(self) -> None:
        """Skips a locally schedulable Ollama candidate when the provider does not answer the model availability probe."""
        endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "call_ollama",
                    "items": {
                        "call_ollama": {
                            "provider": "smart",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "ollama_dead", "model": "model-a", "fallback_prio": 1},
                                        {"provider": "ollama_live", "model": "model-b", "fallback_prio": 2},
                                    ],
                                }
                            ],
                        },
                        "ollama_dead": {
                            "api": "ollama",
                            "baseUrl": "http://dead.example:11434",
                            "models": [
                                {"id": "model-a", "resources": {"res_a": {"VRAM": 8000}}},
                            ],
                        },
                        "ollama_live": {
                            "api": "ollama",
                            "baseUrl": "http://live.example:11434",
                            "models": [
                                {"id": "model-b", "resources": {"res_b": {"VRAM": 8000}}},
                            ],
                        },
                    }
                },
            },
            queue=_SmartFakeQueue(
                {
                    '{"res_a":{"VRAM":8000}}': {
                        "queued_count_total": 0,
                        "queued_count_below_priority": 0,
                        "priority_counts": [],
                    },
                    '{"res_b":{"VRAM":8000}}': {
                        "queued_count_total": 0,
                        "queued_count_below_priority": 0,
                        "priority_counts": [],
                    },
                }
            ),
            resources=_SmartFakeResources({
                '{"res_a":{"VRAM":8000}}': True,
                '{"res_b":{"VRAM":8000}}': True,
            }),
        )

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            self.assertEqual(timeout_ms, 1500)
            if provider_id == "ollama_dead":
                return False
            self.assertEqual(provider_id, "ollama_live")
            self.assertEqual(model_id, "model-b")
            return True

        endpoint._probe_ollama_model_availability = fake_ollama_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
                "priority": 5,
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "model-b")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_live")
        self.assertEqual(task.config["route"]["selection_reason"], "immediate")
        self.assertEqual(task.config["route"]["candidate_probes"][0]["probe_error"], "probe_failed")
        self.assertEqual(task.config["route"]["candidate_probes"][0]["probe_source"], "ollama_http")

    async def test_build_task_for_payload_async_switches_worker_to_openaix_for_remote_openaix_provider(self) -> None:
        """Uses the OpenAIx worker when smart routing resolves the Ollama endpoint to a remote openaix provider."""
        endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "call_ollama",
                    "items": {
                        "call_ollama": {
                            "provider": "smart",
                        },
                        "openaix": {
                            "provider": "remote_aidir",
                        },
                    },
                },
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "remote_aidir", "model": "smart_chat", "fallback_prio": 1},
                                    ],
                                }
                            ],
                        },
                        "remote_aidir": {
                            "api": "openaix",
                            "baseUrl": "http://remote.example:21434",
                            "models": [
                                {"id": "smart_chat"},
                            ],
                        },
                    }
                },
            },
            queue=None,
            resources=None,
        )

        async def fake_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            return {
                "can_run_now": True,
                "queued_count_total": 0,
                "queued_count_below_priority": 0,
                "priority_counts": [],
            }

        endpoint._probe_remote_model_queue_state = fake_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
        )

        self.assertEqual(task.worker_id, "openaix")
        self.assertEqual(task.config["route"]["resolved_provider"], "remote_aidir")

    async def test_build_task_for_payload_async_resolves_first_immediate_candidate(self) -> None:
        """Selects the first candidate that can run immediately and materializes a concrete route."""
        endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "call_ollama",
                    "items": {
                        "call_ollama": {
                            "provider": "smart",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "ollama_busy", "model": "slow-model", "fallback_prio": 5, "request_timeout_ms": 1500},
                                        {"provider": "ollama_ready", "model": "fast-model", "fallback_prio": 10, "request_timeout_ms": 1500},
                                    ],
                                }
                            ],
                        },
                        "ollama_busy": {
                            "api": "ollama",
                            "models": [
                                {"id": "slow-model", "resources": {"busy": {"VRAM": 8000}}},
                            ],
                        },
                        "ollama_ready": {
                            "api": "ollama",
                            "models": [
                                {"id": "fast-model", "resources": {"ready": {"VRAM": 4000}}},
                            ],
                        },
                    }
                },
            },
            queue=_SmartFakeQueue(
                {
                    '{"busy":{"VRAM":8000}}': {
                        "queued_count_total": 1,
                        "queued_count_below_priority": 0,
                        "priority_counts": [{"priority": 5, "count": 1}],
                    },
                    '{"ready":{"VRAM":4000}}': {
                        "queued_count_total": 0,
                        "queued_count_below_priority": 0,
                        "priority_counts": [],
                    },
                }
            ),
            resources=_SmartFakeResources({
                '{"busy":{"VRAM":8000}}': True,
                '{"ready":{"VRAM":4000}}': True,
            }),
        )

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            self.assertEqual(timeout_ms, 1500)
            return True

        endpoint._probe_ollama_model_availability = fake_ollama_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
                "priority": 5,
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "fast-model")
        self.assertEqual(task.config["route"]["requested_provider"], "smart")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_ready")
        self.assertEqual(task.config["route"]["resolved_model"], "fast-model")
        self.assertEqual(task.config["route"]["selection_reason"], "immediate")

    async def test_build_task_for_payload_async_uses_busy_fallback_priority(self) -> None:
        """Chooses the lowest fallback_prio among responsive busy candidates when none can run now."""
        endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "call_ollama",
                    "items": {
                        "call_ollama": {
                            "provider": "smart",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "ollama_a", "model": "model-a", "fallback_prio": 20, "request_timeout_ms": 1500},
                                        {"provider": "ollama_b", "model": "model-b", "fallback_prio": 3, "request_timeout_ms": 1500},
                                    ],
                                }
                            ],
                        },
                        "ollama_a": {
                            "api": "ollama",
                            "models": [
                                {"id": "model-a", "resources": {"res_a": {"VRAM": 8000}}},
                            ],
                        },
                        "ollama_b": {
                            "api": "ollama",
                            "models": [
                                {"id": "model-b", "resources": {"res_b": {"VRAM": 8000}}},
                            ],
                        },
                    }
                },
            },
            queue=_SmartFakeQueue(
                {
                    '{"res_a":{"VRAM":8000}}': {
                        "queued_count_total": 1,
                        "queued_count_below_priority": 0,
                        "priority_counts": [{"priority": 5, "count": 1}],
                    },
                    '{"res_b":{"VRAM":8000}}': {
                        "queued_count_total": 2,
                        "queued_count_below_priority": 0,
                        "priority_counts": [{"priority": 5, "count": 1}, {"priority": 10, "count": 1}],
                    },
                }
            ),
            resources=_SmartFakeResources({
                '{"res_a":{"VRAM":8000}}': True,
                '{"res_b":{"VRAM":8000}}': True,
            }),
        )

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            self.assertEqual(timeout_ms, 1500)
            return True

        endpoint._probe_ollama_model_availability = fake_ollama_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
                "priority": 5,
            },
            stream=False,
        )

        self.assertEqual(task.payload["model"], "model-b")
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_b")
        self.assertEqual(task.config["route"]["selection_reason"], "busy_fallback")
        self.assertEqual(task.config["route"]["fallback_prio"], 3)

    async def test_build_task_for_payload_async_can_use_remote_openaix_probe(self) -> None:
        """Uses remote model-only queue-state probe on the plain Ollama path when local visibility is missing."""
        endpoint = Endpoint_ollama({"id": "ollama", "worker": "call_ollama"})
        endpoint._core = _FakeCore(
            {
                "workers": {
                    "default": "call_ollama",
                    "items": {
                        "call_ollama": {
                            "provider": "smart",
                        }
                    },
                },
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_chat",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "remote_aidir", "model": "smart_chat", "fallback_prio": 1},
                                    ],
                                }
                            ],
                        },
                        "remote_aidir": {
                            "api": "openaix",
                            "baseUrl": "http://remote.example:21434",
                            "models": [
                                {"id": "smart_chat"},
                            ],
                        },
                    }
                },
            },
            queue=None,
            resources=None,
        )

        async def fake_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            self.assertEqual(provider_id, "remote_aidir")
            self.assertEqual(model_id, "smart_chat")
            self.assertEqual(priority, 5)
            self.assertEqual(incoming_bearer_token, "incoming-token")
            return {
                "can_run_now": True,
                "queued_count_total": 0,
                "queued_count_below_priority": 0,
                "priority_counts": [],
            }

        endpoint._probe_remote_model_queue_state = fake_probe

        task = await endpoint._build_task_for_payload_async(
            {
                "model": "smart_chat",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            stream=False,
            incoming_bearer_token="incoming-token",
        )

        self.assertEqual(task.payload["model"], "smart_chat")
        self.assertEqual(task.config["route"]["resolved_provider"], "remote_aidir")
        self.assertEqual(task.config["route"]["selection_reason"], "immediate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
