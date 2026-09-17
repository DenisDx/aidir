"""Focused regressions for the llama.cpp worker and local server lifecycle."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.local_server_manager import LocalServerManager
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
        self.assertEqual(payload["max_tokens"], 12)
        self.assertEqual(payload["temperature"], 0.3)

    def test_converts_openai_response_to_ollama(self) -> None:
        """Maps OpenAI message and usage fields to the internal endpoint response shape."""
        result = CallLlamaCppWorker._openai_response_to_ollama(
            {"model": "model", "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}
        )

        self.assertTrue(result["done"])
        self.assertEqual(result["message"]["content"], "hello")
        self.assertEqual(result["prompt_eval_count"], 4)


class TestLocalServerManager(unittest.IsolatedAsyncioTestCase):
    """Validate persisted-PID ownership behavior."""

    async def test_stop_does_not_touch_unknown_process(self) -> None:
        """Returns false when no aidir-owned PID record exists for a provider."""
        with tempfile.TemporaryDirectory() as directory:
            manager = LocalServerManager({}, Path(directory))
            self.assertFalse(await manager.stop("llama_local"))


if __name__ == "__main__":
    unittest.main(verbosity=2)