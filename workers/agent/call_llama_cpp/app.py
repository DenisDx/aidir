"""llama.cpp worker using the OpenAI-compatible llama-server API."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from core.local_server_manager import LocalServerError
from core.task import Task
from core.task_types.task_agent import Task_agent
from core.worker import WorkerResult
from workers.agent.openaix.app import OpenAIxWorker


class CallLlamaCppWorker(OpenAIxWorker):
    """Run agent tasks through a local or remote OpenAI-compatible llama.cpp server."""

    task_type = "agent"

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Build context, ensure a local server when configured, and proxy one agent task."""
        if not isinstance(task, Task_agent):
            return WorkerResult(ok=False, error={"code": "WRONG_TASK_TYPE", "message": f"Expected Task_agent, got {type(task).__name__}"})

        provider_id = self._resolve_task_provider_id(task)
        try:
            await self._core.llama_cpp_server_manager.ensure_running(
                provider_id,
                str((task.payload or {}).get("model") or ""),
            )
        except LocalServerError as exc:
            return WorkerResult(ok=False, error={"code": exc.code, "message": str(exc)})

        payload = self._apply_generation_defaults(dict(task.payload or {}))
        task.payload = payload
        context_result = await self._apply_context_chain(task)
        if not context_result.ok:
            return context_result

        payload = self._apply_model_generation_defaults(dict(task.payload or {}), provider_id=provider_id)
        payload = self._apply_model_context_window(payload, provider_id=provider_id)
        payload = self._to_openai_payload(payload, task.stream)
        url = f"{self._resolve_base_url(provider_id)}/v1/chat/completions"
        save_call = self._resolve_save_llm_request(task.payload or {})
        headers = self._resolve_request_headers(task, provider_id)

        try:
            async with httpx.AsyncClient(timeout=self._resolve_upstream_timeout(task), headers=headers) as client:
                if task.stream and not payload.get("tools"):
                    return await self._forward_stream(client, url, payload, emit_chunk, task=task, save_call=save_call, task_id=task.id)
                return await self._run_with_internal_tools(client, url, payload, task, emit_chunk, save_call=save_call)
        except httpx.ConnectError as exc:
            await self._finalize_latest_started_llm_call(task, status="connect_error", error_code="UPSTREAM_UNREACHABLE")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_UNREACHABLE", "message": str(exc)})
        except httpx.TimeoutException as exc:
            await self._finalize_latest_started_llm_call(task, status="timeout", error_code="UPSTREAM_TIMEOUT")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_TIMEOUT", "message": self._build_timeout_message(exc)})

    @classmethod
    def _to_openai_payload(cls, payload: dict, stream: bool) -> dict:
        """Convert the internal Ollama-shaped task payload to OpenAI chat-completions syntax."""
        out = {"model": payload.get("model", ""), "messages": list(payload.get("messages") or []), "stream": bool(stream)}
        for key in ("tools", "tool_choice", "stop", "response_format"):
            if key in payload:
                out[key] = payload[key]

        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        option_map = {
            "temperature": "temperature",
            "top_p": "top_p",
            "num_predict": "max_tokens",
            "seed": "seed",
            "presence_penalty": "presence_penalty",
            "frequency_penalty": "frequency_penalty",
        }
        for source, destination in option_map.items():
            value = payload.get(source, options.get(source))
            if value is not None:
                out[destination] = value
        return out

    async def _forward_sync(self, client, url: str, payload: dict, *, task=None, save_call: bool = False, task_id: str = "") -> WorkerResult:
        """Send an OpenAI-compatible llama.cpp request and normalize the response for endpoints."""
        provider_id = self._resolve_task_provider_id(task) if task is not None else self._provider_id
        history_entry = await self._begin_llm_call(task, url=url, payload=payload, provider_id=provider_id, save_call=save_call) if task else None
        response = await client.post(url, json={**payload, "stream": False})
        if response.status_code != 200:
            if task:
                await self._finalize_llm_call(task, history_entry, status="http_error", http_status=response.status_code, error_code="UPSTREAM_ERROR")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_ERROR", "message": f"Upstream returned HTTP {response.status_code}", "body": response.text[:512]})
        try:
            data = self._openai_response_to_ollama(response.json())
        except (ValueError, TypeError, KeyError) as exc:
            if task:
                await self._finalize_llm_call(task, history_entry, status="invalid_json", http_status=response.status_code, error_code="UPSTREAM_INVALID_JSON")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_INVALID_JSON", "message": str(exc) or "Upstream returned invalid JSON"})
        if task:
            await self._finalize_llm_call(task, history_entry, status="ok", http_status=response.status_code, response=data)
        return WorkerResult(ok=True, data=data, usage=data.get("usage"))

    async def _forward_stream(self, client, url: str, payload: dict, emit_chunk, task=None, *, save_call: bool = False, task_id: str = "") -> WorkerResult:
        """Translate llama.cpp OpenAI SSE chunks into internal Ollama-compatible chunks."""
        provider_id = self._resolve_task_provider_id(task) if task is not None else self._provider_id
        history_entry = await self._begin_llm_call(task, url=url, payload=payload, provider_id=provider_id, save_call=save_call) if task else None
        final_data: dict | None = None
        async with client.stream("POST", url, json={**payload, "stream": True}) as response:
            if response.status_code != 200:
                if task:
                    await self._finalize_llm_call(task, history_entry, status="http_error", http_status=response.status_code, error_code="UPSTREAM_ERROR")
                return WorkerResult(ok=False, error={"code": "UPSTREAM_ERROR", "message": f"Upstream returned HTTP {response.status_code}"})
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    chunk = self._openai_response_to_ollama(json.loads(raw), streaming=True)
                except (ValueError, TypeError, KeyError):
                    continue
                final_data = chunk
                if emit_chunk:
                    await emit_chunk(chunk)
        if task:
            await self._finalize_llm_call(task, history_entry, status="ok", http_status=200, response=final_data or {})
        return WorkerResult(ok=True, data=final_data, usage=(final_data or {}).get("usage"))

    @staticmethod
    def _openai_response_to_ollama(data: dict, streaming: bool = False) -> dict:
        """Convert one OpenAI response or SSE delta into aidir's Ollama-compatible result shape."""
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("delta") if streaming else choice.get("message")
        message = message if isinstance(message, dict) else {}
        finish_reason = choice.get("finish_reason")
        done = bool(finish_reason) and streaming
        result = {
            "model": data.get("model", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": {"role": message.get("role", "assistant"), "content": message.get("content") or ""},
            "done": done if streaming else True,
            "done_reason": finish_reason or ("stop" if not streaming else ""),
        }
        if isinstance(message.get("tool_calls"), list):
            result["message"]["tool_calls"] = message["tool_calls"]
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if usage:
            result["usage"] = usage
            result["prompt_eval_count"] = usage.get("prompt_tokens")
            result["eval_count"] = usage.get("completion_tokens")
        return result

    @staticmethod
    def _normalize_assistant_message_for_history(message: dict) -> dict:
        """Produce OpenAI-compatible assistant tool-call history for the next llama.cpp turn."""
        out = {"role": "assistant", "content": message.get("content") or ""}
        calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        if calls:
            out["tool_calls"] = calls
        return out


worker = CallLlamaCppWorker()