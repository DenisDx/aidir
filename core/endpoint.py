"""Base Endpoint class. Subclasses implement specific API protocols."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
from typing import TYPE_CHECKING, Any

from core import log

if TYPE_CHECKING:
    from core.app import Core


class BaseEndpoint(ABC):
    """Abstract base for all endpoints."""

    id: str = ""
    api: str = ""

    def _attach_ingress_logging(self, app: Any) -> None:
        """Log every incoming HTTP request before endpoint processing."""
        @app.middleware("http")
        async def ingress_logging_middleware(request, call_next):
            log(
                "http",
                "info",
                f"Incoming request method={request.method} path={request.url.path}",
                self.id or None,
            )
            return await call_next(request)

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

    async def _read_json_body(self, request: Any) -> tuple[Any, bytes]:
        """Read one JSON-object request body with its original bytes."""
        raw_body = await request.body()
        body = json.loads(raw_body)
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        return body, raw_body

    def _audit_pre_task_rejection(
        self,
        request: Any,
        *,
        protocol: str,
        status_code: int,
        error_code: str,
        reason: str,
    ) -> None:
        """Record one compact rejection that occurred before task creation."""
        core = getattr(self, "_core", None)
        audit_log = getattr(core, "audit_log", None)
        if audit_log is None:
            return
        try:
            from core.error_logging import get_or_create_request_id

            audit_log.record_rejected_request(
                request_id=get_or_create_request_id(request),
                protocol=protocol,
                endpoint=request.url.path,
                http={"method": request.method, "status_code": status_code},
                error_code=error_code,
                reason=reason,
            )
        except Exception as exc:
            log("audit", "error", f"Failed to audit pre-task rejection: {exc}", self.id or None)

    def _audit_rejection_response(
        self,
        request: Any,
        protocol: str,
        response: Any,
        reason: str,
    ) -> None:
        """Record compact rejection metadata from a JSON error response."""
        error_code = "INVALID_REQUEST"
        try:
            response_body = json.loads(response.body)
            error = response_body.get("error") if isinstance(response_body, dict) else {}
            if isinstance(error, dict) and error.get("code"):
                error_code = str(error["code"])
        except Exception:
            pass
        self._audit_pre_task_rejection(
            request,
            protocol=protocol,
            status_code=int(getattr(response, "status_code", 400)),
            error_code=error_code,
            reason=reason,
        )

    @abstractmethod
    def create_app(self, core: "Core") -> Any:
        """Create and return a FastAPI app for this endpoint."""

    @abstractmethod
    async def initialize(self, core: "Core") -> None:
        """Called once at startup."""
