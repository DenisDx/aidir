"""Regression tests for OpenAIx tool arguments parsing."""

from __future__ import annotations

import json
import unittest

import httpx

from core.task_types.task_agent import Task_agent
from core.endpoints.endpoint_openaix import Endpoint_openaix
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

    def __init__(self, config_data: dict) -> None:
        self.config = _FakeConfig(config_data)


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
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(payload["seed"], 7)
        self.assertEqual(payload["presence_penalty"], 0.4)
        self.assertEqual(payload["frequency_penalty"], 0.5)
        self.assertEqual(payload["top_k"], 33)
        self.assertEqual(payload["min_p"], 0.06)
        self.assertEqual(payload["options"], {"seed": 7})

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
        self.assertEqual(task.payload["max_tokens"], 777)
        self.assertEqual(task.payload["seed"], 123)
        self.assertEqual(task.payload["presence_penalty"], 0.25)
        self.assertEqual(task.payload["frequency_penalty"], 0.35)
        self.assertEqual(task.payload["top_k"], 44)
        self.assertEqual(task.payload["min_p"], 0.02)

    def test_normalize_payload_maps_openaix_generation_fields_to_ollama_options(self) -> None:
        """Converts OpenAI/OpenAIx generation aliases into Ollama options names."""
        normalized = OpenAIxWorker._normalize_payload(
            {
                "model": "qwen3",
                "messages": [{"role": "user", "content": "Hi"}],
                "temperature": 0.33,
                "top_p": 0.88,
                "repetition_penalty": 1.17,
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
                "num_predict": 123,
                "seed": 9,
                "presence_penalty": 0.45,
                "frequency_penalty": 0.55,
                "top_k": 27,
                "min_p": 0.04,
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
