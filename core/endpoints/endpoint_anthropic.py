"""Anthropic Messages endpoint."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core import log
from core.endpoints.endpoint_ollama import Endpoint_ollama
from core.error_logging import attach_request_id_middleware, get_or_create_request_id, log_exception
from core.messages_protocol import (
    AnthropicStreamEncoder,
    estimate_input_tokens,
    internal_result_to_messages,
    messages_error_payload,
    messages_error_type_for_code,
    messages_error_type_for_status,
    messages_request_to_internal,
    sse_event_bytes,
)
from core.request_limits import RequestBodyLimitMiddleware
from core.smart_router import SmartRouteError
from core.task import STATUS_CANCELED, STATUS_COMPLETED, STATUS_FAILED
from core.upstream_errors import upstream_error_response
from core.upstream_response import UpstreamChunk, original_chunk, original_response


class Endpoint_anthropic(Endpoint_ollama):
    """Endpoint implementing the Anthropic Messages protocol."""

    api = "anthropic"

    def create_app(self, core) -> FastAPI:
        """Create FastAPI app exposing the Anthropic Messages surface."""
        self._core = core
        app = FastAPI(title=f"aidir-{self.id}", docs_url=None, redoc_url=None)
        app.add_middleware(
            RequestBodyLimitMiddleware,
            max_request_size=core.config.get("http.max_request_size", 104857600),
            audit_log=getattr(core, "audit_log", None),
        )
        attach_request_id_middleware(app)
        self._attach_ingress_logging(app)

        @app.exception_handler(Exception)
        async def unhandled_exception_handler(request: Request, exc: Exception):
            """Log unhandled endpoint exceptions with traceback and request id."""
            request_id = get_or_create_request_id(request)
            log_exception(
                "http",
                self.id,
                f"Unhandled exception method={request.method} path={request.url.path}",
                exc,
                request_id=request_id,
            )
            return JSONResponse(
                status_code=500,
                content=messages_error_payload("api_error", "Internal server error"),
                headers={"X-Request-ID": request_id, "request-id": request_id},
            )

        @app.post("/v1/messages")
        async def messages(request: Request):
            return await self._handle_messages(request)

        @app.post("/v1/messages/count_tokens")
        async def count_tokens(request: Request):
            return await self._handle_count_tokens(request)

        @app.get("/v1/models")
        async def models():
            return self._messages_models_response()

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        return app

    def _messages_error_response(
        self,
        request: Request,
        *,
        status_code: int,
        code: str = "",
        message: str,
        task_id: str | None = None,
        error_type: str | None = None,
    ) -> JSONResponse:
        """Build an Anthropic error response with request identifiers."""
        normalized_type = error_type or (messages_error_type_for_code(code) if code else messages_error_type_for_status(status_code))
        request_id = get_or_create_request_id(request)
        payload = messages_error_payload(normalized_type, message)
        if task_id:
            payload["error"]["task_id"] = task_id
        return JSONResponse(payload, status_code=status_code, headers={"X-Request-ID": request_id, "request-id": request_id})

    async def _handle_messages(self, request: Request) -> Response:
        """Handle one synchronous or streaming Messages request."""
        try:
            body, raw_body = await self._read_json_body(request)
        except Exception:
            self._audit_pre_task_rejection(request, protocol="anthropic", status_code=400, error_code="INVALID_REQUEST", reason="Invalid JSON body")
            return self._messages_error_response(request, status_code=400, code="INVALID_REQUEST", message="Invalid JSON body")

        auth_error = self._authorize_and_apply_envid(request, body)
        if auth_error is not None:
            self._audit_rejection_response(request, "anthropic", auth_error, "Authorization or envid validation failed")
            return self._convert_auth_error(request, auth_error)

        try:
            internal_payload = messages_request_to_internal(body)
        except ValueError as exc:
            self._audit_pre_task_rejection(request, protocol="anthropic", status_code=400, error_code="INVALID_REQUEST", reason=str(exc))
            return self._messages_error_response(request, status_code=400, code="INVALID_REQUEST", message=str(exc))

        stream = bool(internal_payload.get("stream", False))
        incoming_bearer_token = self._extract_bearer_token(request)
        route_trace = self._build_incoming_route_trace(request.headers)
        route_trace_error = self._validate_incoming_route_trace(route_trace)
        if route_trace_error is not None:
            code, status_code, message = route_trace_error
            self._audit_pre_task_rejection(request, protocol="anthropic", status_code=status_code, error_code=code, reason=message)
            return self._messages_error_response(request, status_code=status_code, code=code, message=message)

        try:
            task = await self._build_task_for_payload_async(
                internal_payload,
                stream,
                incoming_bearer_token=incoming_bearer_token,
                route_trace=route_trace,
                request=request,
            )
        except SmartRouteError as exc:
            self._audit_pre_task_rejection(request, protocol="anthropic", status_code=exc.status_code, error_code=exc.code, reason=str(exc))
            return self._messages_error_response(request, status_code=exc.status_code, code=exc.code, message=str(exc))
        except Exception as exc:
            self._audit_pre_task_rejection(request, protocol="anthropic", status_code=500, error_code=getattr(exc, "code", "INVALID_REQUEST"), reason=str(exc))
            return self._messages_error_response(request, status_code=500, code=getattr(exc, "code", ""), message=str(exc))

        route = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        if not isinstance(route, dict):
            self._audit_pre_task_rejection(request, protocol="anthropic", status_code=404, error_code="INVALID_MODEL", reason=str(internal_payload.get("model") or ""))
            return self._messages_error_response(
                request,
                status_code=404,
                code="INVALID_MODEL",
                message=f"Unknown model: {internal_payload.get('model') or ''}",
            )

        self._audit_client_request(task, request, body, protocol="anthropic", raw_body=raw_body)

        try:
            await self._core.on_task_added(task)
        except Exception as exc:
            self._audit_pre_task_rejection(request, protocol="anthropic", status_code=503, error_code=getattr(exc, "code", "QUEUE_ERROR"), reason=str(exc))
            return self._messages_error_response(request, status_code=503, code=getattr(exc, "code", "QUEUE_ERROR"), message=str(exc), task_id=task.id)

        if stream:
            return await self._messages_streaming_response(task, body)

        response = await self._messages_sync_response(task, body)
        self._audit_client_response(task, response)
        log(
            "http",
            "info",
            (
                f"{self.id} /v1/messages sync response: task={task.id} status={task.status} "
                f"http_status={response.status_code} error_code={(task.error or {}).get('code') if isinstance(task.error, dict) else ''}"
            ),
            self.id,
        )
        return response

    def _convert_auth_error(self, request: Request, response: JSONResponse) -> JSONResponse:
        """Convert an existing auth rejection into the Anthropic error envelope."""
        code = "UNAUTHORIZED"
        message = "Invalid API token"
        try:
            payload = json.loads(response.body)
            detail = payload.get("error") if isinstance(payload, dict) else {}
            if isinstance(detail, dict):
                code = str(detail.get("code") or code)
                message = str(detail.get("message") or message)
        except Exception:
            pass
        return self._messages_error_response(request, status_code=int(response.status_code), code=code, message=message)

    async def _handle_count_tokens(self, request: Request) -> JSONResponse:
        """Return a deterministic token estimate without queueing a task."""
        try:
            body, _ = await self._read_json_body(request)
        except Exception:
            return self._messages_error_response(request, status_code=400, code="INVALID_REQUEST", message="Invalid JSON body")

        auth_error = self._authorize_and_apply_envid(request, body)
        if auth_error is not None:
            self._audit_rejection_response(request, "anthropic", auth_error, "Authorization or envid validation failed")
            return self._convert_auth_error(request, auth_error)

        model = body.get("model")
        messages = body.get("messages")
        if not isinstance(model, str) or not model.strip():
            return self._messages_error_response(request, status_code=400, code="INVALID_REQUEST", message="Field 'model' is required")
        if not isinstance(messages, list) or not messages:
            return self._messages_error_response(request, status_code=400, code="INVALID_REQUEST", message="Field 'messages' must be a non-empty array")
        return JSONResponse({"input_tokens": estimate_input_tokens(body)})

    def _external_model_id(self, model_cfg: dict) -> str:
        """Return the client-facing alias or model id."""
        return self._model_alias(model_cfg) or self._model_id(model_cfg)

    def _messages_models_response(self) -> dict:
        """Build the Anthropic model list response."""
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        data: list[dict] = []
        seen: set[str] = set()
        for _, model_cfg in self._iter_provider_models():
            external_id = self._external_model_id(model_cfg)
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)
            data.append(
                {
                    "type": "model",
                    "id": external_id,
                    "display_name": str(model_cfg.get("name") or external_id),
                    "created_at": now,
                }
            )
        return {
            "data": data,
            "has_more": False,
            "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None,
        }

    async def _messages_sync_response(self, task, request_body: dict) -> Response:
        """Wait for a task and return an Anthropic Messages response."""
        timeout_phase = await self._wait_for_task_terminal(task)
        if timeout_phase is not None:
            await self._terminate_task_on_timeout(task)
            return self._messages_error_response_from_task(task, status_code=504, code="TIMEOUT", message="Request timed out")

        asyncio.create_task(self._core.delete_task(task.id))

        if task.status == STATUS_COMPLETED:
            native = original_response(task.upstream_response, "anthropic")
            if native is not None:
                return native
            result = task.result if isinstance(task.result, dict) else {}
            payload = internal_result_to_messages(
                result,
                model=str(request_body.get("model") or ""),
                task_id=task.id,
                include_thinking=isinstance(request_body.get("thinking"), dict),
            )
            return JSONResponse(payload)

        if task.status == STATUS_FAILED:
            error = task.error if isinstance(task.error, dict) else {}
            native = self._native_anthropic_error(error)
            if native is not None:
                return native
            if error.get("code") in {"TIMEOUT", "QUEUE_TIMEOUT", "UPSTREAM_TIMEOUT"}:
                return self._messages_error_response_from_task(task, status_code=504, code="TIMEOUT", message=str(error.get("message") or "Request timed out"))
            status_code = int(error.get("upstream_status") or 502)
            return self._messages_error_response_from_task(
                task,
                status_code=status_code if 400 <= status_code <= 599 else 502,
                code=str(error.get("code") or "UPSTREAM_ERROR"),
                message=str(error.get("message") or "Request failed"),
            )

        if task.status == STATUS_CANCELED:
            return self._messages_error_response_from_task(task, status_code=503, code="OVERLOADED", message="Task was canceled")

        return self._messages_error_response_from_task(task, status_code=500, code="INTERNAL_ERROR", message="Unknown task state")

    def _messages_error_response_from_task(self, task, *, status_code: int, code: str, message: str) -> JSONResponse:
        """Build an error response when no incoming Request object is available."""
        normalized_code = str(code or "").upper()
        if not normalized_code or normalized_code in {"UPSTREAM_ERROR", "UPSTREAM_INVALID_JSON"}:
            normalized_type = messages_error_type_for_status(status_code)
        else:
            normalized_type = messages_error_type_for_code(code)
        payload = messages_error_payload(normalized_type, message)
        payload["error"]["task_id"] = task.id
        return JSONResponse(payload, status_code=status_code)

    @staticmethod
    def _native_anthropic_error(error: dict) -> Response | None:
        """Return an upstream Anthropic error body unchanged when it is one."""
        response = upstream_error_response(error)
        if response is None:
            return None
        try:
            payload = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            return None
        if isinstance(payload, dict) and payload.get("type") == "error":
            return response
        return None

    async def _messages_streaming_response(self, task, request_body: dict) -> Response:
        """Open Anthropic SSE only after a first chunk or a terminal error."""
        while True:
            _, remaining = self._task_timeout_phase(task)
            if remaining is not None and remaining <= 0:
                await self._terminate_task_on_timeout(task)
                return self._messages_error_response_from_task(task, status_code=504, code="TIMEOUT", message="Request timed out")

            wait_timeout = 1.0 if remaining is None else max(0.01, min(remaining, 1.0))
            try:
                chunk = await asyncio.wait_for(task._chunk_queue.get(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                continue

            if chunk is None:
                if task.status != STATUS_COMPLETED:
                    response = await self._messages_sync_response(task, request_body)
                    self._audit_client_response(task, response)
                    return response
                return StreamingResponse(
                    self._messages_stream_response(task, request_body),
                    media_type="text/event-stream",
                )

            return StreamingResponse(
                self._messages_stream_response(task, request_body, first_chunk=chunk),
                headers={
                    "content-type": chunk.content_type
                    if isinstance(chunk, UpstreamChunk) and chunk.protocol == "anthropic"
                    else "text/event-stream"
                },
            )

    async def _messages_stream_response(
        self,
        task,
        request_body: dict,
        first_chunk: dict | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Stream native Anthropic events or synthesize them from internal chunks."""
        encoder = AnthropicStreamEncoder(
            model=str(request_body.get("model") or ""),
            message_id=f"msg_{task.id}",
            include_thinking=isinstance(request_body.get("thinking"), dict),
        )
        audit_log = getattr(self._core, "audit_log", None)
        audit_context = getattr(task, "_audit_client_context", None)
        spool = audit_log.open_body_spool() if audit_log is not None and isinstance(audit_context, dict) else None
        saw_native = False
        try:
            pending = [first_chunk] if first_chunk is not None else []
            while True:
                if pending:
                    chunk = pending.pop(0)
                else:
                    if task.status == STATUS_COMPLETED and task._chunk_queue.empty():
                        break
                    timeout_phase, remaining = self._task_timeout_phase(task)
                    if remaining is not None and remaining <= 0:
                        await self._terminate_task_on_timeout(task)
                        encoded = sse_event_bytes(messages_error_payload("timeout_error", "Request timed out"))
                        yield encoded
                        if spool is not None:
                            spool.write(encoded)
                        return
                    try:
                        wait_timeout = 1.0 if remaining is None else max(0.01, min(remaining, 1.0))
                        chunk = await asyncio.wait_for(task._chunk_queue.get(), timeout=wait_timeout)
                    except asyncio.TimeoutError:
                        continue

                if chunk is None:
                    if task.status == STATUS_FAILED:
                        error = task.error if isinstance(task.error, dict) else {}
                        encoded = sse_event_bytes(
                            messages_error_payload(
                                messages_error_type_for_code(str(error.get("code") or "")),
                                str(error.get("message") or "Request failed"),
                            )
                        )
                        yield encoded
                        if spool is not None:
                            spool.write(encoded)
                    break

                raw = original_chunk(chunk, "anthropic")
                if raw is not None:
                    saw_native = True
                    yield raw
                    if spool is not None:
                        spool.write(raw)
                    continue
                events = encoder.feed(chunk)
                for event in events:
                    encoded = sse_event_bytes(event)
                    yield encoded
                    if spool is not None:
                        spool.write(encoded)

            if not saw_native:
                for event in encoder.finish():
                    encoded = sse_event_bytes(event)
                    yield encoded
                    if spool is not None:
                        spool.write(encoded)
        finally:
            if spool is not None:
                try:
                    event = audit_log.finalize_body_spool(
                        "client_response", spool, content_type="text/event-stream",
                        task_id=task.id, request_id=audit_context["request_id"], protocol=audit_context["protocol"],
                        endpoint=audit_context["endpoint"], worker_id=task.worker_id,
                        http={"status_code": 200, "content_type": "text/event-stream"},
                        terminal_status=task.status,
                    )
                    queue = getattr(self._core, "queue", None)
                    reconcile = getattr(queue, "record_client_response_reconciliation", None)
                    if callable(reconcile):
                        reconcile(task, event["event_id"])
                except Exception as exc:
                    log("audit", "error", f"Failed to audit Anthropic stream response task={task.id}: {exc}", self.id)
            asyncio.create_task(self._core.delete_task(task.id))
