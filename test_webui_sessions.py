"""WebUI session namespace tests."""
from __future__ import annotations

import json
import unittest

from types import SimpleNamespace

from webui.backend import app as webui_app


class _Config:
    """Minimal configuration lookup for session tests."""

    def __init__(self, instance: str = "aidir") -> None:
        self.instance = instance

    def get(self, key: str, default=None):
        """Return the instance name or a default."""
        return self.instance if key == "instance" else default


class _Redis:
    """In-memory Redis stand-in for session storage."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store a value, ignoring the TTL."""
        self.values[key] = value

    async def get(self, key: str) -> str | None:
        """Return a stored value."""
        return self.values.get(key)


class _Core:
    """Minimal core object used by the session helpers."""

    def __init__(self, instance: str = "aidir") -> None:
        self.config = _Config(instance)
        self.redis = _Redis()


class TimeoutClampTests(unittest.TestCase):
    """Client-supplied test timeouts cannot exceed the configured cap."""

    def test_timeout_is_clamped(self):
        """Clamp below the minimum and above the maximum, and reject non-finite values."""
        self.assertEqual(webui_app._bounded_timeout(999999, default=45, maximum=300), 300)
        self.assertEqual(webui_app._bounded_timeout(0, default=45, maximum=300), 1)
        self.assertEqual(webui_app._bounded_timeout("nonsense", default=45, maximum=300), 45)
        self.assertEqual(webui_app._bounded_timeout(float("inf"), default=45, maximum=300), 45)


class ChatEndpointSelectionTests(unittest.TestCase):
    """Test LLM must use a configured chat endpoint, not only an api named ollama."""

    def test_openaix_endpoint_is_used(self):
        """An openaix endpoint serves /api/chat when no ollama endpoint exists."""
        endpoint = webui_app._select_chat_endpoint([
            {"id": "openaix", "api": "openaix", "port": 21434, "worker": "openaix"},
            {"id": "anthropic", "api": "anthropic", "port": 21435},
        ], "call_llama_cpp")
        self.assertEqual(endpoint["id"], "openaix")

    def test_worker_match_is_preferred(self):
        """Prefer the endpoint that names the selected worker."""
        endpoint = webui_app._select_chat_endpoint([
            {"id": "first", "api": "openaix", "port": 1, "worker": "other"},
            {"id": "second", "api": "ollama", "port": 2, "worker": "openaix"},
        ], "openaix")
        self.assertEqual(endpoint["id"], "second")

    def test_no_chat_endpoint(self):
        """Return None when only non-chat APIs are configured."""
        self.assertIsNone(webui_app._select_chat_endpoint([
            {"id": "anthropic", "api": "anthropic", "port": 1},
        ]))


class RequestTokenTests(unittest.TestCase):
    """The logout path and the auth dependency read the same session token."""

    def test_bearer_header_wins_over_the_cookie(self):
        """A header-authenticated session is the one logout must delete."""
        request = SimpleNamespace(headers={"Authorization": "Bearer header-token"})
        self.assertEqual(webui_app._request_session_token(request, "cookie-token"), "header-token")

    def test_cookie_is_the_fallback(self):
        """Use the cookie when no Bearer header is present."""
        request = SimpleNamespace(headers={})
        self.assertEqual(webui_app._request_session_token(request, "cookie-token"), "cookie-token")


class SessionNamespaceTests(unittest.IsolatedAsyncioTestCase):
    """Sessions are valid only under the current instance prefix."""

    async def test_created_session_is_found_under_the_instance_prefix(self) -> None:
        """A new session is stored and read with the instance namespace."""
        core = _Core("node-a")
        token = await webui_app._create_session(core, {"login": "alice", "permissions": ["all"]})
        self.assertIn(f"aidir:session:node-a:{token}", core.redis.values)
        session = await webui_app._get_session(core, token)
        self.assertEqual(session["login"], "alice")

    async def test_legacy_session_key_is_not_accepted(self) -> None:
        """A session from the old shared namespace is ignored."""
        core = _Core("node-a")
        payload = json.dumps({"login": "alice", "permissions": ["all"]})
        core.redis.values["aidir:session:legacy-token"] = payload
        self.assertIsNone(await webui_app._get_session(core, "legacy-token"))


if __name__ == "__main__":
    unittest.main()
