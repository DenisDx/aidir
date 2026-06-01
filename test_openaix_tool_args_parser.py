"""Regression tests for OpenAIx tool arguments parsing."""

from __future__ import annotations

import json
import unittest

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
