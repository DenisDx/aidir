"""Regression tests for terminal external-task retention."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

from core import cron


class _Redis:
    """Minimal Redis fake for external task cleanup."""

    def __init__(self, hashes):
        """Store task hashes by Redis key."""
        self.hashes = hashes
        self.deleted = []

    async def scan(self, cursor, match, count):
        """Return all seeded task keys in one scan page."""
        return 0, list(self.hashes)

    async def hmget(self, key, *fields):
        """Return requested fields in Redis order."""
        task = self.hashes[key]
        return [task.get(field) for field in fields]

    async def delete(self, key):
        """Delete one expired terminal task hash."""
        self.deleted.append(key)


class ExternalTaskRetentionTests(unittest.IsolatedAsyncioTestCase):
    """Verify only expired terminal external tasks are eligible for cleanup."""

    async def test_cleanup_preserves_active_and_recent_external_tasks(self):
        """Delete an expired completed task while leaving queued/running tasks untouched."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(seconds=120)).isoformat()
        recent = (now - timedelta(seconds=10)).isoformat()
        redis = _Redis({
            "aidir:task:old": {"external": "1", "finished_at": old, "status": "completed"},
            "aidir:task:recent": {"external": "1", "finished_at": recent, "status": "failed"},
            "aidir:task:queued": {"external": "1", "finished_at": "", "status": "queued"},
            "aidir:task:running": {"external": "1", "finished_at": "", "status": "running"},
            "aidir:task:internal": {"external": "0", "finished_at": old, "status": "completed"},
        })

        def get_config(key, default=None):
            """Return the short test lifetime and namespace."""
            values = {"cron_period": 60, "tasks.external_task_live": 60, "instance": "aidir"}
            return values.get(key, default)

        with patch.object(cron, "_should_run", return_value=True), patch.object(cron.config, "get", side_effect=get_config):
            await cron.cleanup_expired_tasks(redis)

        self.assertEqual(redis.deleted, ["aidir:task:old"])


if __name__ == "__main__":
    unittest.main()