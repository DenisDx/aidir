"""Anthropic Messages transport worker extending the shared agent workflow."""
from __future__ import annotations

import json

import httpx

from core.call_log import save_llm_call
from core.messages_protocol import (
    ANTHROPIC_VERSION,
    AnthropicStreamDecoder,
    internal_request_to_messages,
    messages_response_to_internal,
)
from core.task import Task
from core.upstream_errors import build_upstream_error
from core.upstream_response import UpstreamChunk, capture_response, iter_sse_events, sse_data
from core.worker import WorkerResult
from workers.agent.call_openai.app import CallOpenAIWorker


class CallAnthropicWorker(CallOpenAIWorker):
    """Forward agent tasks to an Anthropic Messages provider."""

    task_type = "agent"

    def _chat_url(self, provider_id: str, task: Task | None = None) -> str:
        """Return the Anthropic Messages inference URL."""
        return f"{self._resolve_base_url(provider_id)}/v1/messages"

    def _resolve_request_headers(self, task: Task, provider_id: str) -> dict[str, str]:
        """Add the Anthropic version header to configured authentication headers."""
        headers = super()._resolve_request_headers(task, provider_id)
        provider_cfg = self._provider_cfg(provider_id)
        headers["anthropic-version"] = str(provider_cfg.get("anthropic_version") or ANTHROPIC_VERSION)
        beta = provider_cfg.get("anthropic_beta")
        if isinstance(beta, str) and beta.strip():
            headers["anthropic-beta"] = beta.strip()
        return headers

    def _provider_cfg(self, provider_id: str) -> dict:
        """Return the provider config mapping for the current task."""
        if self._core is None:
            return {}
        value = self._core.config.get(f"models.providers.{provider_id}")
        return value if isinstance(value, dict) else {}

    def _default_max_tokens(self, provider_id: str) -> int | None:
        """Return the configured fallback for Anthropic's required max_tokens."""
        provider_cfg = self._provider_cfg(provider_id)
        value = provider_cfg.get("max_output_tokens")
        if value is None:
            value = (provider_cfg.get("defaults") or {}).get("max_output_tokens") if isinstance(provider_cfg.get("defaults"), dict) else None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _final_stream_chunk(final: dict) -> dict:
        """Return a terminal chunk without replaying already-streamed content."""
        return {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": final.get("done_reason"),
            "stop_reason": final.get("stop_reason"),
            "stop_sequence": final.get("stop_sequence"),
            "usage": final.get("usage") or {},
        }

    def _messages_payload(self, provider_id: str, payload: dict, *, stream: bool) -> dict:
        """Convert the internal payload into a Messages request."""
        internal = {**payload, "stream": bool(stream)}
        return internal_request_to_messages(internal, default_max_tokens=self._default_max_tokens(provider_id))

    async def _forward_sync(self, client, url: str, payload: dict, *, task=None, save_call: bool = False, task_id: str = "") -> WorkerResult:
        """Send one synchronous Messages request and normalize the response."""
        provider_id = self._resolve_task_provider_id(task) if task is not None else self._provider_id
        upstream_payload = self._messages_payload(provider_id, payload, stream=False)
        request = self._build_json_request(client, url, upstream_payload)
        history_entry = await self._begin_llm_call(task, url=url, payload=upstream_payload, provider_id=provider_id, save_call=save_call, request_body=request.content) if task else None
        response = await self._send_json_request(client, request, upstream_payload)
        raw_response = await self._read_response_body(response)
        content_type = self._response_content_type(response, "application/json")
        if response.status_code != 200:
            if task:
                if isinstance(history_entry, dict):
                    history_entry["raw_response"] = response.text
                await self._finalize_llm_call(task, history_entry, status="http_error", http_status=response.status_code, error_code="UPSTREAM_ERROR", response_body=raw_response, response_content_type=content_type)
            return WorkerResult(ok=False, error=build_upstream_error(response.status_code, raw_response, content_type))
        try:
            data = messages_response_to_internal(response.json())
        except (ValueError, TypeError, KeyError) as exc:
            if task:
                if isinstance(history_entry, dict):
                    history_entry["raw_response"] = response.text
                await self._finalize_llm_call(task, history_entry, status="invalid_json", http_status=response.status_code, error_code="UPSTREAM_INVALID_JSON", response_body=raw_response, response_content_type=content_type)
            return WorkerResult(ok=False, error={"code": "UPSTREAM_INVALID_JSON", "message": str(exc) or "Upstream returned invalid JSON"})
        if task:
            if isinstance(history_entry, dict):
                history_entry["raw_response"] = response.text
            await self._finalize_llm_call(task, history_entry, status="ok", http_status=response.status_code, response=data, response_body=raw_response, response_content_type=content_type)
        if save_call:
            save_llm_call(self.id, task_id or (task.id if task else ""), upstream_payload, data)
        return WorkerResult(
            ok=True,
            data=data,
            usage=data.get("usage"),
            upstream_response=capture_response("anthropic", response.status_code, content_type, raw_response),
        )

    async def _forward_stream(self, client, url: str, payload: dict, emit_chunk, task=None, *, save_call: bool = False, task_id: str = "") -> WorkerResult:
        """Translate Anthropic SSE events into internal chunks while retaining raw events."""
        provider_id = self._resolve_task_provider_id(task) if task is not None else self._provider_id
        upstream_payload = self._messages_payload(provider_id, payload, stream=True)
        request = self._build_json_request(client, url, upstream_payload)
        history_entry = await self._begin_llm_call(task, url=url, payload=upstream_payload, provider_id=provider_id, save_call=save_call, request_body=request.content) if task else None
        response_spool = None
        decoder = AnthropicStreamDecoder()
        saw_stop = False
        stream_context = await self._open_stream_request(client, request, upstream_payload)
        async with stream_context as response:
            content_type = self._response_content_type(response, "text/event-stream")
            if response.status_code != 200:
                raw_response = await response.aread()
                if task:
                    if isinstance(history_entry, dict):
                        history_entry["raw_response"] = raw_response.decode("utf-8", errors="replace")
                    await self._finalize_llm_call(task, history_entry, status="http_error", http_status=response.status_code, error_code="UPSTREAM_ERROR", response_body=raw_response, response_content_type=content_type)
                return WorkerResult(ok=False, error=build_upstream_error(response.status_code, raw_response, content_type))
            audit_log = getattr(self._core, "audit_log", None)
            response_spool = audit_log.open_body_spool() if task is not None and audit_log is not None else None
            if task is not None:
                task._audit_stream_spool = response_spool
            async for event in iter_sse_events(response, response_spool):
                raw = sse_data(event)
                if raw is None:
                    if emit_chunk:
                        await emit_chunk(UpstreamChunk({}, protocol="anthropic", raw=event, content_type=content_type, original=None))
                    continue
                try:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("Anthropic stream event must be a JSON object")
                except (ValueError, UnicodeDecodeError) as exc:
                    if task:
                        await self._finalize_llm_call(task, history_entry, status="invalid_json", http_status=response.status_code, error_code="UPSTREAM_INVALID_JSON", response_spool=response_spool, response_content_type=content_type)
                    return WorkerResult(ok=False, error={"code": "UPSTREAM_INVALID_JSON", "message": str(exc)})
                if str(data.get("type") or "") == "error":
                    detail = data.get("error") if isinstance(data.get("error"), dict) else {}
                    message = str(detail.get("message") or "Anthropic stream error")
                    if task:
                        await self._finalize_llm_call(task, history_entry, status="http_error", http_status=502, error_code="UPSTREAM_ERROR", response_spool=response_spool, response_content_type=content_type)
                    return WorkerResult(ok=False, error={"code": "UPSTREAM_ERROR", "message": message, "body": json.dumps(data, ensure_ascii=False)})
                try:
                    delta = decoder.feed(data)
                except RuntimeError as exc:
                    if task:
                        await self._finalize_llm_call(task, history_entry, status="http_error", http_status=502, error_code="UPSTREAM_ERROR", response_spool=response_spool, response_content_type=content_type)
                    return WorkerResult(ok=False, error={"code": "UPSTREAM_ERROR", "message": str(exc)})
                event_type = str(data.get("type") or "")
                if emit_chunk:
                    if event_type == "message_stop":
                        final = decoder.result()
                        await emit_chunk(UpstreamChunk(self._final_stream_chunk(final), protocol="anthropic", raw=event, content_type=content_type, original=data))
                    elif delta is not None:
                        await emit_chunk(UpstreamChunk(delta, protocol="anthropic", raw=event, content_type=content_type, original=data))
                    else:
                        await emit_chunk(UpstreamChunk({}, protocol="anthropic", raw=event, content_type=content_type, original=None))
                if event_type == "message_stop":
                    saw_stop = True
        if not saw_stop and emit_chunk:
            await emit_chunk(self._final_stream_chunk(decoder.result()))
        data = decoder.result()
        if task:
            await self._finalize_llm_call(task, history_entry, status="ok", http_status=200, response={"final": data}, response_spool=response_spool, response_content_type="text/event-stream")
        if save_call:
            save_llm_call(self.id, task_id or (task.id if task else ""), upstream_payload, {"final": data})
        return WorkerResult(ok=True, data=data, usage=data.get("usage"))


worker = CallAnthropicWorker()
