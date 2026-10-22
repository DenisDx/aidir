"""Pure conversion tests for the Anthropic Messages adapter."""
from __future__ import annotations

import json
import unittest

from core.messages_protocol import (
    AnthropicStreamDecoder,
    AnthropicStreamEncoder,
    estimate_input_tokens,
    internal_request_to_messages,
    internal_result_to_messages,
    messages_error_payload,
    messages_request_to_internal,
    messages_response_to_internal,
)


class MessagesRequestConversionTests(unittest.TestCase):
    """Verify Anthropic requests become the internal OpenAI-shaped task payload."""

    def test_system_text_tools_and_generation_fields(self) -> None:
        """Map system, tools, tool choice, limits, stop sequences, and options."""
        body = {
            "model": "claude",
            "max_tokens": 256,
            "system": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "search", "description": "Search", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "any"},
            "stop_sequences": ["END"],
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 7,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "metadata": {"user_id": "u"},
        }
        internal = messages_request_to_internal(body)
        self.assertEqual(internal["model"], "claude")
        self.assertEqual(internal["max_completion_tokens"], 256)
        self.assertEqual(internal["messages"][0], {"role": "system", "content": "one\ntwo"})
        self.assertEqual(internal["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual(internal["tools"][0]["function"]["name"], "search")
        self.assertEqual(internal["tool_choice"], "required")
        self.assertEqual(internal["stop"], ["END"])
        self.assertEqual(internal["temperature"], 0.2)
        self.assertEqual(internal["top_p"], 0.9)
        self.assertEqual(internal["top_k"], 7)
        self.assertEqual(internal["thinking"]["budget_tokens"], 1024)

    def test_tool_use_and_tool_result_round_trip(self) -> None:
        """Map assistant tool_use and user tool_result blocks without losing ids."""
        body = {
            "model": "claude",
            "max_tokens": 100,
            "messages": [
                {"role": "user", "content": "run it"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "calling"},
                        {"type": "tool_use", "id": "toolu_1", "name": "run", "input": {"x": 1}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                        {"type": "text", "text": "continue"},
                    ],
                },
            ],
        }
        internal = messages_request_to_internal(body)
        assistant = internal["messages"][1]
        self.assertEqual(assistant["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]), {"x": 1})
        self.assertEqual(internal["messages"][2]["role"], "tool")
        self.assertEqual(internal["messages"][2]["tool_call_id"], "toolu_1")
        self.assertEqual(internal["messages"][3], {"role": "user", "content": "continue"})

    def test_image_block_becomes_data_url(self) -> None:
        """Convert a base64 image source into an OpenAI image_url part."""
        body = {
            "model": "claude",
            "max_tokens": 10,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "see"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
                    ],
                }
            ],
        }
        internal = messages_request_to_internal(body)
        parts = internal["messages"][0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "see"})
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/png;base64,AAA")

    def test_thinking_blocks_are_preserved_opaque(self) -> None:
        """Keep signed thinking blocks for a later native replay."""
        body = {
            "model": "claude",
            "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "think"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "reason", "signature": "sig"},
                        {"type": "text", "text": "answer"},
                    ],
                },
            ],
        }
        internal = messages_request_to_internal(body)
        assistant = internal["messages"][1]
        self.assertEqual(assistant["reasoning_content"], "reason")
        self.assertEqual(assistant["anthropic_blocks"][0]["signature"], "sig")

    def test_invalid_requests_are_rejected(self) -> None:
        """Reject missing model, max_tokens, and malformed messages."""
        for body in (
            {"max_tokens": 10, "messages": [{"role": "user", "content": "x"}]},
            {"model": "m", "messages": [{"role": "user", "content": "x"}]},
            {"model": "m", "max_tokens": 10, "messages": []},
            {"model": "m", "max_tokens": 10, "messages": [{"role": "system", "content": "x"}]},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    messages_request_to_internal(body)

    def test_max_tokens_default_for_internal_clients(self) -> None:
        """Apply a configured default when an OpenAI or Ollama client omits max_tokens."""
        internal = messages_request_to_internal(
            {"model": "m", "messages": [{"role": "user", "content": "x"}]},
            default_max_tokens=123,
        )
        self.assertEqual(internal["max_completion_tokens"], 123)


class InternalToMessagesTests(unittest.TestCase):
    """Verify internal requests and results become Anthropic payloads."""

    def test_system_tool_results_and_limits(self) -> None:
        """Build a valid Messages request from OpenAI-style history."""
        payload = {
            "model": "m",
            "max_completion_tokens": 32,
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "calling",
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "run", "arguments": "{\"a\":1}"}}],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            ],
            "tools": [{"type": "function", "function": {"name": "run", "description": "Run", "parameters": {"type": "object"}}}],
            "stop": ["END"],
        }
        out = internal_request_to_messages(payload)
        self.assertEqual(out["system"], "sys")
        self.assertEqual(out["max_tokens"], 32)
        self.assertEqual(out["tools"][0]["input_schema"], {"type": "object"})
        self.assertEqual(out["stop_sequences"], ["END"])
        assistant = out["messages"][1]
        self.assertEqual(assistant["content"][1]["type"], "tool_use")
        self.assertEqual(assistant["content"][1]["input"], {"a": 1})
        self.assertEqual(out["messages"][2]["content"][0]["tool_use_id"], "call_1")

    def test_result_and_usage(self) -> None:
        """Convert an internal tool result into Anthropic content and usage."""
        result = {
            "message": {
                "role": "assistant",
                "content": "hello",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "run", "arguments": "{\"a\":1}"}}],
            },
            "done_reason": "tool_calls",
            "usage": {"prompt_tokens": 4, "completion_tokens": 5, "cache_read_input_tokens": 6},
        }
        out = internal_result_to_messages(result, model="alias", task_id="t1")
        self.assertEqual(out["id"], "msg_t1")
        self.assertEqual(out["type"], "message")
        self.assertEqual(out["content"][0], {"type": "text", "text": "hello"})
        self.assertEqual(out["content"][1]["type"], "tool_use")
        self.assertEqual(out["stop_reason"], "tool_use")
        self.assertEqual(out["usage"], {"input_tokens": 4, "output_tokens": 5, "cache_read_input_tokens": 6})

    def test_anthropic_response_to_internal(self) -> None:
        """Convert a native Messages response back into the internal result shape."""
        data = {
            "id": "msg_1",
            "model": "m",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "toolu_1", "name": "run", "input": {"x": 1}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }
        internal = messages_response_to_internal(data)
        self.assertEqual(internal["message"]["content"], "hi")
        self.assertEqual(internal["message"]["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(internal["done_reason"], "tool_calls")
        self.assertEqual(internal["usage"]["prompt_tokens"], 2)
        self.assertEqual(internal["usage"]["completion_tokens"], 3)

    def test_error_and_token_helpers(self) -> None:
        """Produce the Anthropic error envelope and a deterministic token estimate."""
        self.assertEqual(messages_error_payload("invalid_request_error", "bad")["type"], "error")
        body = {"system": "x" * 8, "messages": [{"role": "user", "content": "y" * 8}]}
        self.assertEqual(estimate_input_tokens(body), 4)


class MessagesStreamingTests(unittest.TestCase):
    """Verify the encoder and decoder state machines."""

    def test_encoder_emits_valid_sequence(self) -> None:
        """Emit message_start, one text block, message_delta, and message_stop."""
        encoder = AnthropicStreamEncoder(model="m", message_id="msg_1")
        self.assertEqual(encoder.feed({"message": {"content": "hi"}, "done": False})[0]["type"], "message_start")
        events = encoder.feed({"message": {"content": "!"}, "done": False})
        self.assertEqual([event["type"] for event in events], ["content_block_delta"])
        final = encoder.feed({"done": True, "done_reason": "length", "usage": {"completion_tokens": 2}})
        self.assertEqual([event["type"] for event in final], ["content_block_stop", "message_delta", "message_stop"])
        self.assertEqual(final[1]["delta"]["stop_reason"], "max_tokens")
        self.assertEqual(final[1]["usage"]["output_tokens"], 2)

    def test_encoder_tool_call(self) -> None:
        """Emit a complete tool_use block for an internal tool call."""
        encoder = AnthropicStreamEncoder(model="m", message_id="msg_1")
        call = {"id": "call_1", "type": "function", "function": {"name": "run", "arguments": "{\"x\":1}"}}
        events = encoder.feed({"message": {"tool_calls": [call]}, "done": False})
        types = [event["type"] for event in events]
        self.assertIn("content_block_start", types)
        self.assertIn("content_block_delta", types)
        self.assertEqual(events[1]["content_block"]["name"], "run")
        self.assertEqual(events[2]["delta"]["type"], "input_json_delta")

    def test_decoder_text_and_tool_use(self) -> None:
        """Assemble text, tool calls, stop reason, and usage from SSE events."""
        decoder = AnthropicStreamDecoder()
        self.assertIsNone(decoder.feed({"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 1}}}))
        delta = decoder.feed({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}})
        self.assertEqual(delta["message"]["content"], "hi")
        decoder.feed({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "run"}})
        decoder.feed({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"x\":1}"}})
        tool_delta = decoder.feed({"type": "content_block_stop", "index": 1})
        self.assertEqual(tool_delta["message"]["tool_calls"][0]["id"], "toolu_1")
        decoder.feed({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}})
        self.assertEqual(decoder.feed({"type": "message_stop"}), {"done": True})
        result = decoder.result()
        self.assertEqual(result["message"]["content"], "hi")
        self.assertEqual(result["message"]["tool_calls"][0]["function"]["name"], "run")
        self.assertEqual(result["done_reason"], "tool_calls")
        self.assertEqual(result["usage"]["completion_tokens"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
