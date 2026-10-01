"""Shared HTTP request-body limits for endpoint ASGI applications."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from core import log

ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]
DEFAULT_MAX_REQUEST_SIZE = 104857600


def resolve_max_request_size(value: object) -> int:
    """Return a positive request limit in bytes or the default 100 MiB value."""
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_REQUEST_SIZE
    return resolved if resolved > 0 else DEFAULT_MAX_REQUEST_SIZE


class RequestBodyLimitMiddleware:
    """Reject HTTP request bodies exceeding a configured byte limit before parsing."""

    def __init__(
        self,
        app: ASGIApp,
        max_request_size: object = DEFAULT_MAX_REQUEST_SIZE,
        audit_log: Any | None = None,
    ) -> None:
        """Store the wrapped app, normalized size limit, and optional audit writer."""
        self.app = app
        self.max_request_size = resolve_max_request_size(max_request_size)
        self.audit_log = audit_log

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        """Buffer an allowed HTTP body or return HTTP 413 before the wrapped app runs."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        declared_size = self._declared_size(scope)
        if declared_size is not None and declared_size > self.max_request_size:
            await self._reject(scope, send, declared_size)
            return

        body, disconnected, received_size = await self._read_body(receive)
        if received_size > self.max_request_size:
            await self._reject(scope, send, received_size)
            return

        await self.app(scope, self._replay_receive(body, disconnected), send)

    @staticmethod
    def _declared_size(scope: dict) -> int | None:
        """Return a valid Content-Length header value when one is present."""
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed >= 0 else None
        return None

    async def _read_body(self, receive: Callable[[], Awaitable[dict]]) -> tuple[bytes, bool, int]:
        """Read incoming ASGI request chunks and report whether the client disconnected."""
        chunks: list[bytes] = []
        received_size = 0
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                return b"".join(chunks), True, received_size
            if message_type != "http.request":
                continue
            chunk = message.get("body", b"")
            received_size += len(chunk)
            if received_size > self.max_request_size:
                return b"", False, received_size
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks), False, received_size

    @staticmethod
    def _replay_receive(body: bytes, disconnected: bool) -> Callable[[], Awaitable[dict]]:
        """Return a receive callable that replays a buffered body once to the wrapped app."""
        delivered = False

        async def replay() -> dict:
            """Deliver the buffered body, then report normal request completion."""
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"} if disconnected else {"type": "http.request", "body": b"", "more_body": False}

        return replay

    async def _reject(self, scope: dict, send: Callable[[dict], Awaitable[None]], received_size: int) -> None:
        """Send a compact HTTP 413 response and write an operational warning."""
        path = scope.get("path", "")
        method = scope.get("method", "")
        log(
            "http",
            "warning",
            (
                f"Rejected oversized request method={method} path={path} "
                f"bytes={received_size} limit={self.max_request_size}"
            ),
            "request_limit",
        )
        self._record_rejection(scope, received_size)
        body = json.dumps(
            {
                "error": {
                    "code": "REQUEST_TOO_LARGE",
                    "message": "Request body exceeds the configured maximum size",
                }
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    def _record_rejection(self, scope: dict, received_size: int) -> None:
        """Write a compact oversized-request audit event without retaining its body."""
        if self.audit_log is None:
            return
        try:
            self.audit_log.record_rejected_request(
                request_id=self._request_id(scope),
                protocol="http",
                endpoint=scope.get("path", ""),
                http={"method": scope.get("method", ""), "status_code": 413},
                error_code="REQUEST_TOO_LARGE",
                reason="Request body exceeds the configured maximum size",
                received_size=received_size,
                max_request_size=self.max_request_size,
            )
        except Exception as exc:
            log("http", "error", f"Failed to audit oversized request: {exc}", "request_limit")

    @staticmethod
    def _request_id(scope: dict) -> str | None:
        """Return the middleware-assigned or caller-supplied request ID when available."""
        state = scope.get("state")
        if isinstance(state, dict) and isinstance(state.get("request_id"), str):
            return state["request_id"]
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-request-id":
                return value.decode("utf-8", errors="replace")
        return None