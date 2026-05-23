"""
call_ollama worker.
Transparently proxies /api/chat requests to an upstream Ollama instance.
Handles both streaming (stream=true) and non-streaming (stream=false) modes.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import httpx

from core.call_log import save_llm_call
from core.worker import BaseWorker, WorkerResult
from core.task import Task
from core.task_types.task_agent import Task_agent
from core import log


class CallOllamaWorker(BaseWorker):
    """Proxy worker: forwards agent tasks to upstream Ollama API."""

    task_type = "agent"

    def __init__(self) -> None:
        self._base_url: str = "http://127.0.0.1:11434"
        self._timeout: int = 300
        self._provider_id: str = "ollama_local"
        self._core = None

    async def initialize(self, config: dict) -> None:
        """
        Resolve upstream Ollama URL from worker config or main models.providers.
        config is the merged dict from workers.items.call_ollama + {"_core": core}.
        """
        core = config.get("_core")
        self._core = core
        provider_id: str = config.get("provider", "ollama_local")
        self._provider_id = provider_id

        if core is not None:
            base_url = core.config.get(f"models.providers.{provider_id}.baseUrl")
            if base_url:
                self._base_url = base_url.rstrip("/")

        log("worker", "info",
            f"call_ollama initialized; upstream={self._base_url}", "call_ollama")

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """
        Forward the ollama /api/chat request to upstream.
        For stream=True: calls emit_chunk for each response chunk.
        For stream=False: returns full response in WorkerResult.data.
        """
        if not isinstance(task, Task_agent):
            return WorkerResult(
                ok=False,
                error={"code": "WRONG_TASK_TYPE", "message": f"Expected Task_agent, got {type(task).__name__}"},
            )

        payload: dict = dict(task.payload)    # shallow copy; we may mutate stream/options
        payload = self._apply_model_context_window(payload)
        stream: bool = task.stream
        url = f"{self._base_url}/api/chat"
        log_opts: dict = (payload.get("log") or {}).get("options") or {}
        save_call: bool = bool(log_opts.get("save_llm_request", False))

        log("worker", "debug",
            f"Forwarding task {task.id} to {url} stream={stream}", "call_ollama")

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                if stream:
                    return await self._forward_stream(client, url, payload, task, emit_chunk, save_call)
                else:
                    return await self._forward_sync(client, url, payload, task, save_call)

        except httpx.ConnectError as exc:
            log("worker", "error", f"Upstream Ollama unreachable: {exc}", "call_ollama")
            return WorkerResult(
                ok=False,
                error={"code": "UPSTREAM_UNREACHABLE", "message": str(exc)},
            )
        except httpx.TimeoutException as exc:
            log("worker", "error", f"Upstream Ollama timed out: {exc}", "call_ollama")
            return WorkerResult(
                ok=False,
                error={"code": "UPSTREAM_TIMEOUT", "message": str(exc)},
            )
        except Exception as exc:
            log("worker", "error", f"Unexpected error: {exc}", "call_ollama")
            return WorkerResult(
                ok=False,
                error={"code": "EXCEPTION", "message": str(exc)},
            )

    def _apply_model_context_window(self, payload: dict) -> dict:
        """Set options.num_ctx from model config contextWindow when not explicitly provided."""
        if not isinstance(payload, dict):
            return payload

        model_name = payload.get("model")
        if not isinstance(model_name, str) or not model_name.strip():
            return payload

        options = payload.get("options")
        if not isinstance(options, dict):
            options = {}

        # Explicit request value always wins.
        if options.get("num_ctx") is not None:
            return payload

        cfg_window = self._resolve_model_context_window(model_name)
        if cfg_window is None:
            return payload

        options["num_ctx"] = cfg_window
        payload["options"] = options
        return payload

    def _resolve_model_context_window(self, model_name: str) -> int | None:
        """Return configured contextWindow for model from active provider, if available."""
        if self._core is None:
            return None

        provider_models = self._core.config.get(f"models.providers.{self._provider_id}.models") or []
        if not isinstance(provider_models, list):
            return None

        for model_cfg in provider_models:
            if not isinstance(model_cfg, dict):
                continue
            cfg_id = model_cfg.get("id")
            cfg_name = model_cfg.get("name")
            if model_name not in {cfg_id, cfg_name}:
                continue

            raw_ctx = model_cfg.get("contextWindow")
            if raw_ctx is None:
                return None
            try:
                parsed = int(raw_ctx)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        return None

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _forward_sync(
        self, client: httpx.AsyncClient, url: str, payload: dict, task: Task_agent, save_call: bool = False
    ) -> WorkerResult:
        """Non-streaming: send request, wait for full response, return it."""
        upstream_payload = {**payload, "stream": False}
        resp = await client.post(url, json=upstream_payload)

        if resp.status_code != 200:
            return WorkerResult(
                ok=False,
                error={
                    "code": "UPSTREAM_ERROR",
                    "message": f"Upstream returned HTTP {resp.status_code}",
                    "body": resp.text[:512],
                },
            )

        data = resp.json()
        if save_call:
            save_llm_call(self.id, task.id, upstream_payload, data)
        return WorkerResult(ok=True, data=data, usage=data.get("usage"))

    async def _forward_stream(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict,
        task: Task_agent,
        emit_chunk: Callable[[dict], Awaitable[None]] | None,
        save_call: bool = False,
    ) -> WorkerResult:
        """Streaming: forward chunks from upstream to emit_chunk; return final state."""
        upstream_payload = {**payload, "stream": True}
        final_data: dict | None = None
        chunks: list[dict] = [] if save_call else []

        async with client.stream("POST", url, json=upstream_payload) as resp:
            if resp.status_code != 200:
                return WorkerResult(
                    ok=False,
                    error={
                        "code": "UPSTREAM_ERROR",
                        "message": f"Upstream returned HTTP {resp.status_code}",
                    },
                )

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk: dict = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if emit_chunk:
                    await emit_chunk(chunk)
                if save_call:
                    chunks.append(chunk)

                if chunk.get("done"):
                    final_data = chunk

        if save_call:
            save_llm_call(self.id, task.id, upstream_payload, {"stream_chunks": chunks})
        return WorkerResult(ok=True, data=final_data, usage=(final_data or {}).get("usage"))


# Module-level export required by workers_loader
worker = CallOllamaWorker()
