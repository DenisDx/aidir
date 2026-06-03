"""Base Endpoint class. Subclasses implement specific API protocols."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from core import log

if TYPE_CHECKING:
    from core.app import Core


class BaseEndpoint(ABC):
    """Abstract base for all endpoints."""

    id: str = ""
    api: str = ""

    def _warn_deprecated_request_timeout(self, endpoint_cfg: dict) -> None:
        """Warn when deprecated endpoint-level request_timeout is still configured."""
        if not isinstance(endpoint_cfg, dict) or "request_timeout" not in endpoint_cfg:
            return
        log(
            "http",
            "warning",
            (
                f"Endpoint {self.id} uses deprecated config field endpoints.*.request_timeout; "
                "task lifetime is controlled by tasks.queue_timeout/tasks.run_timeout, "
                "and one upstream call is controlled by worker request_timeout"
            ),
            self.id or None,
        )

    @staticmethod
    def _normalize_utc(value: datetime | None) -> datetime | None:
        """Return a timezone-aware UTC datetime or None when unavailable."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _task_timeout_phase(self, task) -> tuple[str, float | None]:
        """Return the active timeout phase and seconds remaining for the task."""
        now = datetime.now(timezone.utc)
        started_at = self._normalize_utc(getattr(task, "started_at", None))
        if started_at is not None:
            try:
                limit = int(getattr(task, "run_timeout", 0) or 0)
            except (TypeError, ValueError):
                limit = 0
            if limit <= 0:
                return "run", None
            elapsed = max(0.0, (now - started_at).total_seconds())
            return "run", float(limit) - elapsed

        created_at = self._normalize_utc(getattr(task, "created_at", None))
        try:
            limit = int(getattr(task, "queue_timeout", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0 or created_at is None:
            return "queue", None
        elapsed = max(0.0, (now - created_at).total_seconds())
        return "queue", float(limit) - elapsed

    async def _wait_for_task_terminal(self, task) -> str | None:
        """Wait until task finishes, returning timeout phase when a phase deadline expires."""
        while not task._done_event.is_set():
            phase, remaining = self._task_timeout_phase(task)
            if remaining is not None and remaining <= 0:
                return phase

            wait_timeout = 1.0 if remaining is None else max(0.01, min(remaining, 1.0))
            try:
                await asyncio.wait_for(task._done_event.wait(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                continue
        return None

    async def _terminate_task_on_timeout(self, task) -> None:
        """Terminate a live task through Core when available, otherwise mark it canceled."""
        core = getattr(self, "_core", None)
        if core is not None:
            await core.terminate_task(task.id)
            return

        queue = getattr(core, "queue", None)
        if queue is not None:
            await queue.mark_canceled(task)

    @abstractmethod
    def create_app(self, core: "Core") -> Any:
        """Create and return a FastAPI app for this endpoint."""

    @abstractmethod
    async def initialize(self, core: "Core") -> None:
        """Called once at startup."""
