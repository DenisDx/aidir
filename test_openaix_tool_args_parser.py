"""Regression tests for OpenAIx tool arguments parsing."""

from __future__ import annotations

import json
import unittest

from workers.agent.openaix.app import OpenAIxWorker


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
