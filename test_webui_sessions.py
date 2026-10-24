"""WebUI session namespace tests."""
from __future__ import annotations

import json
import unittest

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
