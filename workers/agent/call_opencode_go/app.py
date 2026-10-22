"""Dedicated OpenCode Go worker for Chat Completions and Messages models."""
from __future__ import annotations

from typing import Awaitable, Callable

from redis.exceptions import RedisError

from core import log
from core.opencode_session import OpenCodeSessionError, OpenCodeSessionStore
from core.task import Task
from core.worker import WorkerResult
from workers.agent.call_anthropic.app import CallAnthropicWorker
from workers.agent.call_openai.app import CallOpenAIWorker


class CallOpenCodeGoWorker(CallAnthropicWorker):
    """Add OpenCode Go session and User-Agent policy to the selected transport."""

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

    @staticmethod
    def _task_protocol(task: Task) -> str:
        """Return the resolved model protocol, defaulting to Chat Completions."""
        route = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        if isinstance(route, dict):
            value = str(route.get("resolved_protocol") or "").strip()
            if value:
                return value
        return "chat-completions"

    def _chat_url(self, provider_id: str, task: Task | None = None) -> str:
        """Select the OpenCode Go path for the resolved model protocol."""
        base_url = self._resolve_base_url(provider_id)
        if task is not None and self._task_protocol(task) == "anthropic-messages":
            return f"{base_url}/v1/messages"
        return f"{base_url}/v1/chat/completions"

    async def _forward_sync(self, client, url: str, payload: dict, *, task=None, save_call: bool = False, task_id: str = "") -> WorkerResult:
        """Dispatch a synchronous request to the transport selected for this task."""
        if task is not None and self._task_protocol(task) == "anthropic-messages":
            return await CallAnthropicWorker._forward_sync(self, client, url, payload, task=task, save_call=save_call, task_id=task_id)
        return await CallOpenAIWorker._forward_sync(self, client, url, payload, task=task, save_call=save_call, task_id=task_id)

    async def _forward_stream(self, client, url: str, payload: dict, emit_chunk, task=None, *, save_call: bool = False, task_id: str = "") -> WorkerResult:
        """Dispatch a streaming request to the transport selected for this task."""
        if task is not None and self._task_protocol(task) == "anthropic-messages":
            return await CallAnthropicWorker._forward_stream(self, client, url, payload, emit_chunk, task=task, save_call=save_call, task_id=task_id)
        return await CallOpenAIWorker._forward_stream(self, client, url, payload, emit_chunk, task=task, save_call=save_call, task_id=task_id)

    def _resolve_request_headers(self, task: Task, provider_id: str) -> dict[str, str]:
        """Add OpenCode-specific headers without affecting generic providers."""
        if self._task_protocol(task) == "anthropic-messages":
            headers = CallAnthropicWorker._resolve_request_headers(self, task, provider_id)
            provider_cfg = self._provider_cfg(provider_id)
            auth = provider_cfg.get("auth") if isinstance(provider_cfg.get("auth"), dict) else {}
            token = auth.get("token") if auth.get("token") is not None else auth.get("key")
            if isinstance(token, str) and token.strip():
                headers.setdefault("x-api-key", token.strip())
        else:
            headers = CallOpenAIWorker._resolve_request_headers(self, task, provider_id)
        headers["User-Agent"] = self._user_agent
        headers["x-opencode-session"] = str(getattr(task, "_opencode_session_value"))
        return headers

    def _resolve_base_url(self, provider_id: str) -> str:
        """Normalize a legacy OpenCode Go `/v1` base URL to its API root."""
        base_url = super()._resolve_base_url(provider_id)
        return base_url[:-3] if base_url.endswith("/v1") else base_url


worker = CallOpenCodeGoWorker()
