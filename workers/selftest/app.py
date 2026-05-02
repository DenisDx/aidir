"""
selftest tool worker.
Runs health checks for core runtime and returns a structured report.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.task import Task
from core.worker import BaseWorker, WorkerResult


class SelftestWorker(BaseWorker):
    """Tool worker that checks health of redis, queue, workers, and resources."""

    task_type = "tool"

    async def initialize(self, config: dict) -> None:
        """Store core reference and optional worker settings."""
        self._core = config.get("_core")
        self._include_workers = bool(config.get("includeWorkers", True))
        self._include_resources = bool(config.get("includeResources", True))

    async def execute(self, task: Task, emit_chunk=None) -> WorkerResult:
        """Run health checks and return report with summary and checks list."""
        if self._core is None:
            return WorkerResult(
                ok=False,
                error={"code": "SELFTEST_NOT_INITIALIZED", "message": "Core reference is not available"},
            )

        checks: list[dict] = []
        errors = 0

        # Redis ping
        try:
            await self._core.redis.ping()
            checks.append({"name": "redis_ping", "ok": True, "details": "pong"})
        except Exception as exc:
            errors += 1
            checks.append({"name": "redis_ping", "ok": False, "details": str(exc)})

        # Queue sizes by type
        try:
            queue_stats = {}
            ns = self._core.config.get("instance", "aidir")
            for ttype in ("agent", "tool", "request"):
                key = f"{ns}:queue:{ttype}"
                queue_stats[ttype] = int(await self._core.redis.zcard(key))
            checks.append({"name": "queue_sizes", "ok": True, "details": queue_stats})
        except Exception as exc:
            errors += 1
            checks.append({"name": "queue_sizes", "ok": False, "details": str(exc)})

        # Workers registry
        if self._include_workers:
            try:
                workers = {
                    wid: {
                        "task_type": w.task_type,
                        "enabled": bool(w.enabled),
                    }
                    for wid, w in self._core.workers.items()
                }
                checks.append({"name": "workers_loaded", "ok": True, "details": workers})
            except Exception as exc:
                errors += 1
                checks.append({"name": "workers_loaded", "ok": False, "details": str(exc)})

        # Resources snapshot
        if self._include_resources:
            try:
                resources = self._core.resources.snapshot() if self._core.resources else []
                checks.append({"name": "resources", "ok": True, "details": resources})
            except Exception as exc:
                errors += 1
                checks.append({"name": "resources", "ok": False, "details": str(exc)})

        report = {
            "tool": "selftest",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ok": errors == 0,
            "errors": errors,
            "checks": checks,
        }
        return WorkerResult(ok=(errors == 0), data=report, error=None if errors == 0 else {"code": "SELFTEST_FAILED", "message": "One or more checks failed", "report": report})


worker = SelftestWorker()
