"""Focused regressions for the llama.cpp worker and local server lifecycle."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from core.local_server_manager import LocalServerError, LocalServerManager
from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.resources import Resources
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