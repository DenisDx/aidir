import os
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

from core.task import Task

if "httpx" not in sys.modules:
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=None, Response=object)

from workers.tool.http_api.app import HttpApiWorker


class _FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = int(status_code)
        self._body = body if body is not None else {}
        self.headers = headers or {"content-type": "application/json"}
        self.text = text if text is not None else ""

    def json(self):
        return self._body


class _FakeAsyncClient:
    responses = []
    calls = []

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        _FakeAsyncClient.calls.append({"method": "GET", "url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        if not _FakeAsyncClient.responses:
            raise RuntimeError("no fake response queued")
        return _FakeAsyncClient.responses.pop(0)

    async def post(self, url, params=None, json=None, headers=None):
        _FakeAsyncClient.calls.append(
            {
                "method": "POST",
                "url": url,
                "params": dict(params or {}),
                "json": json,
                "headers": dict(headers or {}),
            }
        )
        if not _FakeAsyncClient.responses:
            raise RuntimeError("no fake response queued")
        return _FakeAsyncClient.responses.pop(0)


class TestHttpApiWorker(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = HttpApiWorker()
        await self.worker.initialize(
            {
                "request_timeout": 12,
                "user_agent": "test-http-api/1.0",
                "max_response_chars": 4096,
                "connectors": {
                    "demo": {
                        "enabled": True,
                        "base_url": "https://example.test/api",
                        "auth": {"type": "none"},
                        "operations": {
                            "list": {
                                "method": "GET",
                                "path": "/items",
                                "query": {"q": "{q}"},
                                "pagination": {
                                    "type": "cursor",
                                    "request_param": "cursor",
                                    "response_field": "next_token",
                                },
                                "result_path": "items",
                            }
                        },
                    }
                },
            }
        )

    async def test_unknown_connector(self):
        task = Task(type="tool", payload={"arguments": {"connector": "missing", "operation": "list", "params": {}}})
        result = await self.worker.execute(task)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.get("code"), "HTTP_API_UNKNOWN_CONNECTOR")

    async def test_key_auth_missing_secret(self):
        await self.worker.initialize(
            {
                "connectors": {
                    "secure": {
                        "enabled": True,
                        "base_url": "https://example.test/api",
                        "auth": {"type": "bearer", "key": ""},
                        "operations": {"list": {"method": "GET", "path": "/items", "result_path": "items"}},
                    }
                }
            }
        )
        task = Task(type="tool", payload={"arguments": {"connector": "secure", "operation": "list", "params": {}}})
        result = await self.worker.execute(task)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.get("code"), "HTTP_API_AUTH_MISSING")

    async def test_legacy_env_auth_type_unsupported(self):
        await self.worker.initialize(
            {
                "connectors": {
                    "secure": {
                        "enabled": True,
                        "base_url": "https://example.test/api",
                        "auth": {"type": "bearer_env", "env": "AIDIR_HTTP_API_TEST_TOKEN"},
                        "operations": {"list": {"method": "GET", "path": "/items", "result_path": "items"}},
                    }
                }
            }
        )
        task = Task(type="tool", payload={"arguments": {"connector": "secure", "operation": "list", "params": {}}})
        result = await self.worker.execute(task)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.get("code"), "HTTP_API_AUTH_UNSUPPORTED")

    async def test_hook_file_invalid(self):
        await self.worker.initialize(
            {
                "connectors": {
                    "demo": {
                        "enabled": True,
                        "base_url": "https://example.test/api",
                        "auth": {"type": "none"},
                        "operations": {
                            "list": {
                                "method": "GET",
                                "path": "/items",
                                "response_hook_file": "./does_not_exist_hook.py",
                                "result_path": "items",
                            }
                        },
                    }
                }
            }
        )
        task = Task(type="tool", payload={"arguments": {"connector": "demo", "operation": "list", "params": {}}})
        result = await self.worker.execute(task)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.get("code"), "HTTP_API_HOOK_NOT_FOUND")

    async def test_cursor_auto_pagination_with_limit(self):
        _FakeAsyncClient.calls = []
        _FakeAsyncClient.responses = [
            _FakeResponse(body={"items": [{"id": 1}, {"id": 2}], "next_token": "t2"}),
            _FakeResponse(body={"items": [{"id": 3}, {"id": 4}], "next_token": "t3"}),
        ]

        task = Task(
            type="tool",
            payload={
                "arguments": {
                    "connector": "demo",
                    "operation": "list",
                    "params": {"q": "hello"},
                    "limit": 3,
                    "max_pages": 4,
                }
            },
        )

        with patch("workers.tool.http_api.app.httpx.AsyncClient", _FakeAsyncClient):
            result = await self.worker.execute(task)

        self.assertTrue(result.ok)
        self.assertEqual(len(result.data.get("items") or []), 3)
        self.assertEqual(result.data.get("paging", {}).get("pages_fetched"), 2)
        self.assertTrue(result.data.get("paging", {}).get("has_more"))
        self.assertEqual(_FakeAsyncClient.calls[0]["params"].get("q"), "hello")
        self.assertEqual(_FakeAsyncClient.calls[1]["params"].get("cursor"), "t2")

    async def test_response_hook_transform_applied(self):
        with tempfile.NamedTemporaryFile("w", suffix="_hook.py", delete=False) as handle:
            handle.write(
                textwrap.dedent(
                    """
                    def transform_response(response, context):
                        return {
                            "items": [{"id": "hooked"}],
                            "next_token": None
                        }
                    """
                )
            )
            hook_path = handle.name

        try:
            await self.worker.initialize(
                {
                    "connectors": {
                        "demo": {
                            "enabled": True,
                            "base_url": "https://example.test/api",
                            "auth": {"type": "none"},
                            "operations": {
                                "list": {
                                    "method": "GET",
                                    "path": "/items",
                                    "response_hook_file": hook_path,
                                    "pagination": {
                                        "type": "cursor",
                                        "request_param": "cursor",
                                        "response_field": "next_token",
                                    },
                                    "result_path": "items",
                                }
                            },
                        }
                    }
                }
            )

            _FakeAsyncClient.calls = []
            _FakeAsyncClient.responses = [
                _FakeResponse(body={"items": [{"id": "raw"}], "next_token": "raw-next"}),
            ]

            task = Task(type="tool", payload={"arguments": {"connector": "demo", "operation": "list", "params": {}}})
            with patch("workers.tool.http_api.app.httpx.AsyncClient", _FakeAsyncClient):
                result = await self.worker.execute(task)

            self.assertTrue(result.ok)
            self.assertEqual((result.data.get("items") or [])[0].get("id"), "hooked")
            self.assertIsNone(result.data.get("paging", {}).get("next_page_token"))
        finally:
            try:
                os.unlink(hook_path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
