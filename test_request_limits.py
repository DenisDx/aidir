"""Regression tests for inbound ASGI request-size enforcement."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from core.request_limits import RequestBodyLimitMiddleware, resolve_max_request_size


class RequestBodyLimitMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """Verify request limits are applied before the wrapped endpoint runs."""

    async def _invoke(self, middleware, messages, headers=None):
        """Run one ASGI request and return sent messages plus endpoint call count."""
        sent = []
        endpoint_calls = 0

        async def app(scope, receive, send):
            """Capture an allowed body and return a successful response."""
            nonlocal endpoint_calls
            endpoint_calls += 1
            body = (await receive()).get("body", b"")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        middleware.app = app

        async def receive():
            """Deliver the test ASGI messages in order."""
            return messages.pop(0)

        async def send(message):
            """Capture each ASGI response message."""
            sent.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/test",
            "headers": headers or [],
        }
        await middleware(scope, receive, send)
        return sent, endpoint_calls

    async def test_allows_body_within_limit(self):
        """Pass an allowed body through to the wrapped endpoint unchanged."""
        middleware = RequestBodyLimitMiddleware(lambda *_: None, 5)
        sent, endpoint_calls = await self._invoke(
            middleware,
            [{"type": "http.request", "body": b"hello", "more_body": False}],
        )

        self.assertEqual(endpoint_calls, 1)
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(sent[1]["body"], b"hello")

    async def test_replay_waits_for_the_real_disconnect_after_the_body(self):
        """Avoid an empty-request busy loop in streaming response disconnect listeners."""
        source_messages = [{"type": "http.disconnect"}]

        async def receive():
            """Return the real disconnect event after the middleware consumed the body."""
            return source_messages.pop(0)

        replay = RequestBodyLimitMiddleware._replay_receive(b"{}", False, receive)

        self.assertEqual(await replay(), {"type": "http.request", "body": b"{}", "more_body": False})
        self.assertEqual(await replay(), {"type": "http.disconnect"})

    async def test_rejects_oversized_content_length_without_reading_body(self):
        """Return HTTP 413 before calling receive when Content-Length exceeds the limit."""
        middleware = RequestBodyLimitMiddleware(lambda *_: None, 5)
        with patch("core.request_limits.log"):
            sent, endpoint_calls = await self._invoke(
                middleware,
                [],
                headers=[(b"content-length", b"6")],
            )

        self.assertEqual(endpoint_calls, 0)
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(json.loads(sent[1]["body"])["error"]["code"], "REQUEST_TOO_LARGE")

    async def test_rejects_chunked_body_before_endpoint_runs(self):
        """Return HTTP 413 when chunked request bytes exceed the configured limit."""
        middleware = RequestBodyLimitMiddleware(lambda *_: None, 5)
        with patch("core.request_limits.log"):
            sent, endpoint_calls = await self._invoke(
                middleware,
                [
                    {"type": "http.request", "body": b"abc", "more_body": True},
                    {"type": "http.request", "body": b"def", "more_body": False},
                ],
            )

        self.assertEqual(endpoint_calls, 0)
        self.assertEqual(sent[0]["status"], 413)

    async def test_records_compact_audit_rejection(self):
        """Record request metadata without retaining an oversized request body."""
        recorded = []

        class AuditLog:
            """Capture rejected-request audit calls from the limiter."""

            def record_rejected_request(self, **fields):
                """Store one test audit record and return it."""
                recorded.append(fields)
                return fields

        middleware = RequestBodyLimitMiddleware(lambda *_: None, 5, audit_log=AuditLog())
        with patch("core.request_limits.log"):
            sent, endpoint_calls = await self._invoke(
                middleware,
                [],
                headers=[(b"content-length", b"6"), (b"x-request-id", b"request-1")],
            )

        self.assertEqual(endpoint_calls, 0)
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(recorded[0]["request_id"], "request-1")
        self.assertEqual(recorded[0]["error_code"], "REQUEST_TOO_LARGE")
        self.assertNotIn("data", recorded[0])

    def test_resolve_max_request_size_uses_default_for_invalid_values(self):
        """Use the documented 100 MiB default for missing or invalid config values."""
        self.assertEqual(resolve_max_request_size(None), 104857600)
        self.assertEqual(resolve_max_request_size(0), 104857600)
        self.assertEqual(resolve_max_request_size("50"), 50)