"""Dedicated OpenCode Go Chat Completions worker."""
from __future__ import annotations

from typing import Awaitable, Callable

from redis.exceptions import RedisError

from core import log
from core.opencode_session import OpenCodeSessionError, OpenCodeSessionStore
from core.task import Task
from core.worker import WorkerResult
from workers.agent.call_openai.app import CallOpenAIWorker


class CallOpenCodeGoWorker(CallOpenAIWorker):
    """Add OpenCode Go session and User-Agent policy to the OpenAI transport."""

    task_type = "agent"

    async def initialize(self, config: dict) -> None:
        """Load the configured non-generic User-Agent for OpenCode Go calls."""
        await super().initialize(config)
        self._user_agent = str(config.get("user_agent") or "").strip()

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Resolve the required OpenCode session before forwarding one task."""
        if not self._user_agent or self._user_agent.lower().startswith(("python-", "httpx", "curl/")):
            return WorkerResult(
                ok=False,
                error={"code": "OPENCODE_GO_INVALID_CONFIG", "message": "A coding-agent user_agent is required"},
            )
        if self._core is None or self._core.redis is None:
            return WorkerResult(
                ok=False,
                error={"code": "OPENCODE_SESSION_STORE_UNAVAILABLE", "message": "Redis is unavailable"},
            )

        provider_id = self._resolve_task_provider_id(task)
        model_id = str((task.payload or {}).get("model") or "").strip()
        session_cfg = (task.config or {}).get("opencode_session")
        session_cfg = session_cfg if isinstance(session_cfg, dict) else {}
        try:
            store = OpenCodeSessionStore.from_environment(
                self._core.redis,
                str(self._core.config.get("instance", "aidir")),
            )
            caller_reference = str(session_cfg.get("caller_reference") or "").strip()
            if caller_reference:
                value = await store.resolve_caller_session(caller_reference)
                managed = False
            else:
                identity_digest = str(session_cfg.get("identity_digest") or "").strip()
                ttl_seconds = int(session_cfg.get("ttl_seconds") or 0)
                value, managed = await store.resolve_managed_session(
                    provider_id,
                    model_id,
                    identity_digest,
                    ttl_seconds,
                )
        except (OpenCodeSessionError, ValueError) as exc:
            return WorkerResult(
                ok=False,
                error={"code": "OPENCODE_SESSION_STORE_UNAVAILABLE", "message": str(exc)},
            )

        task._opencode_session_value = value
        try:
            result = await super().execute(task, emit_chunk)
            if result.ok and managed:
                await store.refresh_managed_session(
                    provider_id,
                    model_id,
                    str(session_cfg.get("identity_digest") or ""),
                    int(session_cfg.get("ttl_seconds") or 0),
                )
            return result
        except OpenCodeSessionError as exc:
            return WorkerResult(
                ok=False,
                error={"code": "OPENCODE_SESSION_STORE_UNAVAILABLE", "message": str(exc)},
            )
        finally:
            if caller_reference:
                try:
                    await store.delete_caller_session(caller_reference)
                except RedisError as exc:
                    log(
                        "worker",
                        "warning",
                        f"Failed to delete OpenCode caller session for task={task.id}: {exc}",
                        self.id,
                    )
            task.__dict__.pop("_opencode_session_value", None)

    def _resolve_request_headers(self, task: Task, provider_id: str) -> dict[str, str]:
        """Add OpenCode-specific headers without affecting generic OpenAI providers."""
        headers = super()._resolve_request_headers(task, provider_id)
        headers["User-Agent"] = self._user_agent
        headers["x-opencode-session"] = str(getattr(task, "_opencode_session_value"))
        return headers

    def _resolve_base_url(self, provider_id: str) -> str:
        """Normalize a legacy OpenCode Go `/v1` base URL to its API root."""
        base_url = super()._resolve_base_url(provider_id)
        return base_url[:-3] if base_url.endswith("/v1") else base_url


worker = CallOpenCodeGoWorker()
