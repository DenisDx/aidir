"""Remote OpenAI Chat Completions transport worker."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from core.call_log import save_llm_call
from core.generation_options import OLLAMA_TO_OPENAI_OPTION_FIELDS
from core.task import Task
from core.task_types.task_agent import Task_agent
from core.upstream_errors import build_upstream_error
from core.upstream_response import UpstreamChunk, capture_response, iter_sse_events, sse_data
from core.worker import WorkerResult
from workers.agent.openaix.app import OpenAIxWorker


class CallOpenAIWorker(OpenAIxWorker):
    """Forward agent tasks to a remote OpenAI Chat Completions provider."""

    task_type = "agent"

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Convert one agent task and proxy it to a remote OpenAI provider."""
        if not isinstance(task, Task_agent):
            return WorkerResult(ok=False, error={"code": "WRONG_TASK_TYPE", "message": f"Expected Task_agent, got {type(task).__name__}"})

        provider_id = self._resolve_task_provider_id(task)
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
                if task.stream and not self._extract_injected_tool_names(task):
                    return await self._forward_stream(client, url, payload, emit_chunk, task=task, save_call=save_call, task_id=task.id)
                return await self._run_with_internal_tools(client, url, payload, task, emit_chunk, save_call=save_call)
        except httpx.ConnectError as exc:
            await self._finalize_latest_started_llm_call(task, status="connect_error", error_code="UPSTREAM_UNREACHABLE")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_UNREACHABLE", "message": str(exc)})
        except httpx.TimeoutException as exc:
            await self._finalize_latest_started_llm_call(task, status="timeout", error_code="UPSTREAM_TIMEOUT")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_TIMEOUT", "message": self._build_timeout_message(exc)})
        except asyncio.CancelledError:
            await self._finalize_latest_started_llm_call(
                task,
                status="cancelled",
                error_code="CANCELLED",
                response_spool=getattr(task, "_audit_stream_spool", None),
                response_content_type="text/event-stream",
            )
            raise

    @classmethod
    def _to_openai_payload(cls, payload: dict, stream: bool) -> dict:
        """Convert the internal Ollama-shaped task payload to OpenAI chat-completions syntax."""
        internal_fields = {
            "context_builder", "envid", "log", "options", "priority", "queue_timeout",
            "request_kind", "timeout", "worker",
        }
        out = {
            key: value
            for key, value in payload.items()
            if key not in internal_fields and key not in {"model", "messages", "stream", "num_predict"}
        }
        out["model"] = payload.get("model", "")
        out["messages"] = list(payload.get("messages") or [])
        out["stream"] = bool(stream)

        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        for source, destination in OLLAMA_TO_OPENAI_OPTION_FIELDS.items():
            value = payload.get(source)
            if value is None:
                value = options.get(source)
            if value is not None and destination not in out:
                out[destination] = value
        return out

    async def _forward_sync(self, client, url: str, payload: dict, *, task=None, save_call: bool = False, task_id: str = "") -> WorkerResult:
        """Send an OpenAI-compatible llama.cpp request and normalize the response for endpoints."""
        provider_id = self._resolve_task_provider_id(task) if task is not None else self._provider_id
        upstream_payload = {**payload, "stream": False}
        request = self._build_json_request(client, url, upstream_payload)
        history_entry = await self._begin_llm_call(task, url=url, payload=upstream_payload, provider_id=provider_id, save_call=save_call, request_body=request.content) if task else None
        response = await self._send_json_request(client, request, upstream_payload)
        raw_response = await self._read_response_body(response)
        if response.status_code != 200:
            if task:
                if isinstance(history_entry, dict):
                    history_entry["raw_response"] = response.text
                await self._finalize_llm_call(task, history_entry, status="http_error", http_status=response.status_code, error_code="UPSTREAM_ERROR", response_body=raw_response, response_content_type=self._response_content_type(response, "application/octet-stream"))
            return WorkerResult(ok=False, error=build_upstream_error(
                response.status_code, raw_response, self._response_content_type(response, "application/octet-stream"),
            ))
        try:
            data = self._openai_response_to_ollama(response.json())
        except (ValueError, TypeError, KeyError) as exc:
            if task:
                if isinstance(history_entry, dict):
                    history_entry["raw_response"] = response.text
                await self._finalize_llm_call(task, history_entry, status="invalid_json", http_status=response.status_code, error_code="UPSTREAM_INVALID_JSON", response_body=raw_response, response_content_type=self._response_content_type(response, "application/octet-stream"))
            return WorkerResult(ok=False, error={"code": "UPSTREAM_INVALID_JSON", "message": str(exc) or "Upstream returned invalid JSON"})
        if task:
            if isinstance(history_entry, dict):
                history_entry["raw_response"] = response.text
            await self._finalize_llm_call(task, history_entry, status="ok", http_status=response.status_code, response=data, response_body=raw_response, response_content_type=self._response_content_type(response, "application/json"))
        if save_call:
            save_llm_call(self.id, task_id or (task.id if task else ""), {**payload, "stream": False}, data)
        return WorkerResult(
            ok=True, data=data, usage=data.get("usage"),
            upstream_response=capture_response("openai", response.status_code, self._response_content_type(response, "application/json"), raw_response),
        )

    async def _forward_stream(self, client, url: str, payload: dict, emit_chunk, task=None, *, save_call: bool = False, task_id: str = "") -> WorkerResult:
        """Translate llama.cpp OpenAI SSE chunks into internal Ollama-compatible chunks."""
        provider_id = self._resolve_task_provider_id(task) if task is not None else self._provider_id
        final_data: dict | None = None
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        upstream_payload = {**payload, "stream": True}
        response_spool = None
        request = self._build_json_request(client, url, upstream_payload)
        history_entry = await self._begin_llm_call(task, url=url, payload=upstream_payload, provider_id=provider_id, save_call=save_call, request_body=request.content) if task else None
        stream_context = await self._open_stream_request(client, request, upstream_payload)
        async with stream_context as response:
            if response.status_code != 200:
                raw_response = await response.aread()
                if task:
                    if isinstance(history_entry, dict):
                        history_entry["raw_response"] = raw_response.decode("utf-8", errors="replace")
                    await self._finalize_llm_call(task, history_entry, status="http_error", http_status=response.status_code, error_code="UPSTREAM_ERROR", response_body=raw_response, response_content_type=self._response_content_type(response, "application/octet-stream"))
                return WorkerResult(ok=False, error=build_upstream_error(
                    response.status_code, raw_response, self._response_content_type(response, "application/octet-stream"),
                ))
            audit_log = getattr(self._core, "audit_log", None)
            response_spool = audit_log.open_body_spool() if task is not None and audit_log is not None else None
            if task is not None:
                task._audit_stream_spool = response_spool
            content_type = self._response_content_type(response, "text/event-stream")
            async for event in iter_sse_events(response, response_spool):
                raw = sse_data(event)
                if raw is None:
                    if emit_chunk:
                        await emit_chunk(UpstreamChunk({}, protocol="openai", raw=event, content_type=content_type, original=None))
                    continue
                if raw.strip() == b"[DONE]":
                    if emit_chunk:
                        await emit_chunk(UpstreamChunk({}, protocol="openai", raw=event, content_type=content_type, original=None))
                    continue
                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("Executor chat stream event must be a JSON object")
                except (ValueError, UnicodeDecodeError) as exc:
                    if task:
                        await self._finalize_llm_call(
                            task, history_entry, status="invalid_json", http_status=response.status_code,
                            error_code="UPSTREAM_INVALID_JSON", response_spool=response_spool,
                            response_content_type=content_type,
                        )
                    return WorkerResult(ok=False, error={"code": "UPSTREAM_INVALID_JSON", "message": str(exc)})
                if isinstance(data, dict) and "error" in data:
                    detail = data["error"]
                    status_code = detail.get("code") if isinstance(detail, dict) else None
                    if not isinstance(status_code, int) or not 400 <= status_code <= 599:
                        status_code = 502
                    if task:
                        await self._finalize_llm_call(
                            task, history_entry, status="http_error", http_status=status_code,
                            error_code="UPSTREAM_ERROR", response_spool=response_spool,
                            response_content_type=self._response_content_type(response, "text/event-stream"),
                        )
                    return WorkerResult(ok=False, error=build_upstream_error(
                        status_code, raw, "application/json",
                    ))
                chunk = self._openai_response_to_ollama(data, streaming=True)
                message = chunk.get("message") if isinstance(chunk.get("message"), dict) else {}
                if message.get("content") is not None:
                    content_parts.append(str(message["content"]))
                if message.get("thinking") is not None:
                    thinking_parts.append(str(message["thinking"]))
                final_data = chunk
                if emit_chunk:
                    await emit_chunk(UpstreamChunk(chunk, protocol="openai", raw=event, content_type=content_type, original=data))
        if task:
            await self._finalize_llm_call(
                task,
                history_entry,
                status="ok",
                http_status=200,
                response={"final": final_data or {}},
                response_spool=response_spool,
                response_content_type=self._response_content_type(response, "text/event-stream"),
            )
        if save_call:
            save_llm_call(
                self.id,
                task_id or (task.id if task else ""),
                upstream_payload,
                {"final": final_data or {}},
            )
        final_data = self._assemble_stream_result(final_data, content_parts, thinking_parts)
        return WorkerResult(ok=True, data=final_data, usage=(final_data or {}).get("usage"))

    @staticmethod
    def _assemble_stream_result(
        final_data: dict | None,
        content_parts: list[str],
        thinking_parts: list[str],
    ) -> dict | None:
        """Return the final stream value with complete assistant content and reasoning."""
        if not isinstance(final_data, dict):
            return final_data
        message = final_data.get("message") if isinstance(final_data.get("message"), dict) else {}
        complete_message = {**message}
        if content_parts:
            complete_message["content"] = "".join(content_parts)
        if thinking_parts:
            complete_message["thinking"] = "".join(thinking_parts)
        return {**final_data, "message": complete_message}

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
            **data,
            "model": data.get("model", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": {**message, "role": message.get("role", "assistant"), "content": message.get("content")},
            "done": done if streaming else True,
            "done_reason": finish_reason or ("stop" if not streaming else ""),
        }
        if "thinking" not in result["message"]:
            for field in ("reasoning_content", "reasoning"):
                if field in message:
                    result["message"]["thinking"] = message[field]
                    break
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
        out = {**message, "role": "assistant", "content": message.get("content")}
        out.pop("thinking", None)
        calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        if calls:
            out["tool_calls"] = calls
        return out


worker = CallOpenAIWorker()