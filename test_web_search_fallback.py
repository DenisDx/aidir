"""
Tests for web_search and web_fetch provider fallback chain.
Covers: brave primary, searxng fallback, searxng-only, all-down, blackout/cooldown.
"""
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Stub httpx if not installed
if "httpx" not in sys.modules:
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=None, Response=object)

from workers.tool.web_search.app import WebSearchWorker, _ProviderState
from workers.tool.web_fetch.app import WebFetchWorker
from core.task import Task
from core.worker import WorkerResult


# ── helpers ───────────────────────────────────────────────────────────────────

def _search_task(query: str = "python async", **kwargs) -> Task:
    args = {"query": query}
    args.update(kwargs)
    return Task(type="tool", payload={"arguments": args})


def _fetch_task(url: str = "https://example.com/page", **kwargs) -> Task:
    args = {"url": url}
    args.update(kwargs)
    return Task(type="tool", payload={"arguments": args})


def _brave_cfg(api_key: str = "test-key") -> dict:
    return {"id": "brave", "type": "brave", "enabled": True, "apiKey": api_key}


def _searxng_cfg(host: str = "127.0.0.1", port: int = 18080, empty_is_error: bool = False, engines: list | None = None) -> dict:
    return {
        "id": "local_searxng", "type": "searxng", "enabled": True,
        "host": host, "port": port, "engines": engines or [], "categories": ["general"],
        "empty_is_error": empty_is_error,
        "language": "all", "safesearch": 0,
    }


def _brave_search_response(items: int = 3, more: bool = False) -> MagicMock:
    results = [{"title": f"r{i}", "url": f"https://x.com/{i}", "description": f"desc{i}"} for i in range(items)]
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"web": {"results": results}, "query": {"more_results_available": more}}
    return r


def _searxng_search_response(items: int = 3) -> MagicMock:
    results = [{"title": f"sr{i}", "url": f"https://sx.com/{i}", "content": f"content{i}"} for i in range(items)]
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"results": results}
    return r


def _http_error_response(code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = code
    r.text = f"HTTP {code}"
    r.json.side_effect = ValueError("not json")
    return r


# ── web_search tests ──────────────────────────────────────────────────────────

class TestWebSearchFallback(unittest.IsolatedAsyncioTestCase):

    async def _make_worker(self, providers: list) -> WebSearchWorker:
        w = WebSearchWorker()
        await w.initialize({"request_timeout": 5, "provider_cooldown_seconds": 60, "providers": providers})
        return w

    async def test_brave_primary_success(self):
        """Brave returns results; SearXNG should not be called."""
        w = await self._make_worker([_brave_cfg(), _searxng_cfg()])

        healthz = MagicMock()
        healthz.status_code = 200

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw):
                if "/healthz" in url:
                    return healthz
                return _brave_search_response(2)

        with patch("workers.tool.web_search.app.httpx.AsyncClient", FakeClient):
            result = await w.execute(_search_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["provider"], "brave")
        self.assertEqual(len(result.data["items"]), 2)

    async def test_brave_fails_falls_back_to_searxng(self):
        """Brave returns 429; worker falls back to SearXNG."""
        w = await self._make_worker([_brave_cfg(), _searxng_cfg()])

        healthz = MagicMock(status_code=200)
        brave_resp = _http_error_response(429)
        searxng_resp = _searxng_search_response(3)

        calls: list[str] = []

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw):
                calls.append(url)
                if "/healthz" in url:
                    return healthz
                if "api.search.brave.com" in url:
                    return brave_resp
                return searxng_resp

        with patch("workers.tool.web_search.app.httpx.AsyncClient", FakeClient):
            result = await w.execute(_search_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["provider"], "searxng")
        self.assertEqual(len(result.data["items"]), 3)
        self.assertTrue(any("brave" in c for c in calls))
        self.assertTrue(any("searxng" not in c and "healthz" not in c or "/search" in c for c in calls))

    async def test_searxng_unavailable_falls_back_to_brave(self):
        """SearXNG /healthz fails; worker uses Brave (second in list)."""
        w = await self._make_worker([_searxng_cfg(), _brave_cfg()])

        brave_resp = _brave_search_response(1)

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw):
                if "/healthz" in url:
                    raise ConnectionRefusedError("searxng down")
                return brave_resp

        with patch("workers.tool.web_search.app.httpx.AsyncClient", FakeClient):
            result = await w.execute(_search_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["provider"], "brave")

    async def test_all_providers_fail_returns_error(self):
        """All providers fail; worker returns error."""
        w = await self._make_worker([_brave_cfg(""), _searxng_cfg()])

        healthz = MagicMock(status_code=503)

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw):
                if "/healthz" in url:
                    return healthz
                return _http_error_response(500)

        with patch("workers.tool.web_search.app.httpx.AsyncClient", FakeClient):
            result = await w.execute(_search_task())

        self.assertFalse(result.ok)

    async def test_searxng_only_config(self):
        """SearXNG-only config without Brave."""
        w = await self._make_worker([_searxng_cfg()])

        healthz = MagicMock(status_code=200)
        searxng_resp = _searxng_search_response(5)

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw):
                if "/healthz" in url:
                    return healthz
                return searxng_resp

        with patch("workers.tool.web_search.app.httpx.AsyncClient", FakeClient):
            result = await w.execute(_search_task(count=5))

        self.assertTrue(result.ok)
        self.assertEqual(result.data["provider"], "searxng")

    async def test_empty_searxng_falls_through_to_next_provider(self):
        """Empty searxng result should try the next provider when empty_is_error is enabled."""
        w = await self._make_worker([
            _searxng_cfg(empty_is_error=True, engines=["bing"]),
            _searxng_cfg(empty_is_error=True, engines=["mojeek"]),
        ])

        empty_result = WorkerResult(ok=True, data={"provider": "searxng", "items": []})
        filled_result = WorkerResult(ok=True, data={"provider": "searxng", "items": [{"title": "ok"}]})

        with patch.object(w, "_check_searxng_health", AsyncMock(return_value=True)), \
             patch.object(w, "_call_searxng_search", AsyncMock(side_effect=[empty_result, filled_result])):
            result = await w.execute(_search_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["items"][0]["title"], "ok")

    async def test_all_empty_searxng_returns_empty(self):
        """If all searxng providers are empty, return the empty result."""
        w = await self._make_worker([
            _searxng_cfg(empty_is_error=True, engines=["bing"]),
            _searxng_cfg(empty_is_error=True, engines=["mojeek"]),
        ])

        empty_result_1 = WorkerResult(ok=True, data={"provider": "searxng", "items": []})
        empty_result_2 = WorkerResult(ok=True, data={"provider": "searxng", "items": []})

        with patch.object(w, "_check_searxng_health", AsyncMock(return_value=True)), \
             patch.object(w, "_call_searxng_search", AsyncMock(side_effect=[empty_result_1, empty_result_2])):
            result = await w.execute(_search_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["items"], [])

    async def test_provider_blackout_respected(self):
        """After Brave fails it is blacklisted; second call skips it directly."""
        w = await self._make_worker([_brave_cfg(), _searxng_cfg()])

        # Force brave provider into blacklisted state
        brave_state = w._providers[0]
        brave_state.blacklist(3600)  # 1h cooldown

        healthz = MagicMock(status_code=200)
        searxng_resp = _searxng_search_response(2)

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw):
                if "/healthz" in url:
                    return healthz
                if "brave" in url:
                    raise AssertionError("Brave should be blacklisted and skipped")
                return searxng_resp

        with patch("workers.tool.web_search.app.httpx.AsyncClient", FakeClient):
            result = await w.execute(_search_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["provider"], "searxng")

    async def test_blackout_expires(self):
        """After cooldown expires the provider becomes available again."""
        state = _ProviderState({"id": "brave", "type": "brave", "enabled": True})
        state.blacklist(0.01)  # 10ms
        self.assertFalse(state.is_available())
        time.sleep(0.02)
        self.assertTrue(state.is_available())

    async def test_legacy_single_provider_config(self):
        """Old single-provider config (provider + apiKey) still works."""
        w = WebSearchWorker()
        await w.initialize({
            "request_timeout": 5,
            "provider": "brave",
            "apiKey": "legacy-key",
        })
        self.assertEqual(len(w._providers), 1)
        self.assertEqual(w._providers[0].type, "brave")
        self.assertEqual(w._providers[0].cfg["apiKey"], "legacy-key")

    async def test_no_query_returns_error(self):
        """Empty query is rejected before any provider is tried."""
        w = await self._make_worker([_brave_cfg()])
        result = await w.execute(_search_task(""))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.get("code"), "INVALID_ARGUMENT")


# ── web_fetch tests ───────────────────────────────────────────────────────────

class TestWebFetchFallback(unittest.IsolatedAsyncioTestCase):

    async def _make_worker(self, providers: list) -> WebFetchWorker:
        w = WebFetchWorker()
        await w.initialize({"request_timeout": 5, "provider_cooldown_seconds": 60, "providers": providers})
        return w

    async def test_brave_fetch_primary(self):
        """Brave LLM Context returns snippets successfully."""
        w = await self._make_worker([_brave_cfg()])

        brave_body = {
            "grounding": {
                "generic": [{"url": "https://example.com/page", "title": "Example", "snippets": ["snippet text"]}]
            },
            "sources": {},
        }
        brave_resp = MagicMock()
        brave_resp.status_code = 200
        brave_resp.json.return_value = brave_body

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kw):
                return brave_resp

        with patch("workers.tool.web_fetch.app.httpx.AsyncClient", FakeClient):
            result = await w.execute(_fetch_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["provider"], "brave")
        self.assertIn("snippet text", result.data["text"])

    async def test_brave_fetch_fails_searxng_used(self):
        """When Brave LLM Context fails, SearXNG fetch is used."""
        w = await self._make_worker([_brave_cfg(), _searxng_cfg()])

        brave_error = MagicMock(status_code=429, text="rate limit")
        healthz = MagicMock(status_code=200)
        searxng_search = MagicMock(status_code=200)
        searxng_search.json.return_value = {"results": [{"url": "https://example.com/page", "title": "T", "content": "ctx text"}]}
        page_fetch = MagicMock(status_code=200, text="<html><body><p>page body content</p></body></html>")

        calls: list = []

        class FakeClient:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def post(self, url, **kw):
                calls.append(("POST", url))
                return brave_error
            async def get(self, url, **kw):
                calls.append(("GET", url))
                if "/healthz" in url:
                    return healthz
                if "/search" in url:
                    return searxng_search
                return page_fetch  # direct page fetch

        with patch("workers.tool.web_fetch.app.httpx.AsyncClient", FakeClient):
            result = await w.execute(_fetch_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["provider"], "searxng")

    async def test_fetch_missing_url_returns_error(self):
        """Missing url argument is caught before any provider call."""
        w = await self._make_worker([_brave_cfg()])
        result = await w.execute(Task(type="tool", payload={"arguments": {}}))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.get("code"), "INVALID_ARGUMENT")

    async def test_fetch_relative_url_returns_error(self):
        """Relative URL is rejected with INVALID_ARGUMENT."""
        w = await self._make_worker([_brave_cfg()])
        result = await w.execute(_fetch_task("/relative/path"))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.get("code"), "INVALID_ARGUMENT")

    async def test_legacy_single_provider_config(self):
        """Old single-provider config for web_fetch still works."""
        w = WebFetchWorker()
        await w.initialize({
            "request_timeout": 5,
            "provider": "brave",
            "apiKey": "legacy-key",
        })
        self.assertEqual(len(w._providers), 1)
        self.assertEqual(w._providers[0].type, "brave")

    async def test_empty_searxng_falls_through_to_next_provider(self):
        """Empty searxng fetch result should try the next provider when empty_is_error is enabled."""
        w = await self._make_worker([
            _searxng_cfg(empty_is_error=True, engines=["bing"]),
            _searxng_cfg(empty_is_error=True, engines=["mojeek"]),
        ])

        empty_result = WorkerResult(ok=True, data={"provider": "searxng", "text": "", "snippets": [], "matches": []})
        filled_result = WorkerResult(ok=True, data={"provider": "searxng", "text": "filled", "snippets": ["filled"], "matches": [{"url": "https://example.com"}]})

        with patch.object(w, "_check_searxng_health", AsyncMock(return_value=True)), \
             patch.object(w, "_call_searxng_fetch", AsyncMock(side_effect=[empty_result, filled_result])):
            result = await w.execute(_fetch_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["text"], "filled")

    async def test_all_empty_searxng_fetch_returns_empty(self):
        """If all searxng fetch providers are empty, return the empty result."""
        w = await self._make_worker([
            _searxng_cfg(empty_is_error=True, engines=["bing"]),
            _searxng_cfg(empty_is_error=True, engines=["mojeek"]),
        ])

        empty_result_1 = WorkerResult(ok=True, data={"provider": "searxng", "text": "", "snippets": [], "matches": []})
        empty_result_2 = WorkerResult(ok=True, data={"provider": "searxng", "text": "", "snippets": [], "matches": []})

        with patch.object(w, "_check_searxng_health", AsyncMock(return_value=True)), \
             patch.object(w, "_call_searxng_fetch", AsyncMock(side_effect=[empty_result_1, empty_result_2])):
            result = await w.execute(_fetch_task())

        self.assertTrue(result.ok)
        self.assertEqual(result.data["text"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
