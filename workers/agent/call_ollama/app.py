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
    _GENERATION_OPTION_FIELDS = {
        "temperature": "temperature",
        "top_p": "top_p",
        "repeat_penalty": "repeat_penalty",
        "repetition_penalty": "repeat_penalty",
        "repeat_last_n": "repeat_last_n",
        "num_predict": "num_predict",
        "max_tokens": "num_predict",
        "seed": "seed",
        "presence_penalty": "presence_penalty",
        "frequency_penalty": "frequency_penalty",
        "top_k": "top_k",
        "min_p": "min_p",
    }

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
        provider_id = self._resolve_task_provider_id(task)
        base_url = self._resolve_base_url(provider_id)
        payload = self._apply_model_generation_defaults(payload, provider_id=provider_id)
        payload = self._apply_model_context_window(payload, provider_id=provider_id)
        stream: bool = task.stream
        url = f"{base_url}/api/chat"
        log_opts: dict = (payload.get("log") or {}).get("options") or {}
        save_call: bool = bool(log_opts.get("save_llm_request", False))

        log("worker", "debug",
            f"Forwarding task {task.id} to {url} provider={provider_id} stream={stream}", "call_ollama")

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                if stream:
                    return await self._forward_stream(client, url, payload, task, emit_chunk, save_call)
                else:
                    return await self._forward_sync(client, url, payload, task, save_call)

        except httpx.ConnectError as exc:
            log("worker", "warning", f"Upstream Ollama unreachable: {exc}", "call_ollama")
            return WorkerResult(
                ok=False,
                error={"code": "UPSTREAM_UNREACHABLE", "message": str(exc)},
            )
        except httpx.TimeoutException as exc:
            log("worker", "warning", f"Upstream Ollama timed out: {exc}", "call_ollama")
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

    def _apply_model_context_window(self, payload: dict, provider_id: str | None = None) -> dict:
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

        cfg_window = self._resolve_model_context_window(model_name, provider_id=provider_id)
        if cfg_window is None:
            return payload

        options["num_ctx"] = cfg_window
        payload["options"] = options
        return payload

    def _apply_model_generation_defaults(self, payload: dict, provider_id: str | None = None) -> dict:
        """Apply per-model generation defaults into Ollama options unless request overrides them."""
        if not isinstance(payload, dict):
            return payload

        model_name = payload.get("model")
        if not isinstance(model_name, str) or not model_name.strip():
            return payload

        model_cfg = self._resolve_model_cfg(model_name, provider_id=provider_id)
        if not isinstance(model_cfg, dict):
            return payload

        out = dict(payload)
        options = out.get("options") if isinstance(out.get("options"), dict) else {}
        for field_name, option_name in self._GENERATION_OPTION_FIELDS.items():
            if self._payload_has_generation_value(out, option_name) or options.get(option_name) is not None:
                continue

            default_value = model_cfg.get(field_name)
            if default_value is None and option_name != field_name:
                default_value = model_cfg.get(option_name)
            if default_value is not None:
                options[option_name] = default_value

        if options:
            out["options"] = options
        return out

    @classmethod
    def _payload_has_generation_value(cls, payload: dict, option_name: str) -> bool:
        """Return whether payload already defines any top-level alias for one Ollama option name."""
        if not isinstance(payload, dict):
            return False
        for field_name, mapped_option in cls._GENERATION_OPTION_FIELDS.items():
            if mapped_option != option_name:
                continue
            if payload.get(field_name) is not None:
                return True
        return False

    def _resolve_model_context_window(self, model_name: str, provider_id: str | None = None) -> int | None:
        """Return configured contextWindow for model from effective provider, if available."""
        model_cfg = self._resolve_model_cfg(model_name, provider_id=provider_id)
        if not isinstance(model_cfg, dict):
            return None

        raw_ctx = model_cfg.get("contextWindow")
        if raw_ctx is None:
            return None
        try:
            parsed = int(raw_ctx)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _resolve_model_cfg(self, model_name: str, provider_id: str | None = None) -> dict | None:
        """Return configured model dictionary for the effective provider, if available."""
        if self._core is None:
            return None

        effective_provider_id = str(provider_id or self._provider_id or "").strip()
        provider_models = self._core.config.get(f"models.providers.{effective_provider_id}.models") or []
        if not isinstance(provider_models, list):
            return None

        for model_cfg in provider_models:
            if not isinstance(model_cfg, dict):
                continue
            cfg_id = model_cfg.get("id")
            cfg_name = model_cfg.get("name")
            if model_name not in {cfg_id, cfg_name}:
                continue

            return model_cfg

        return None

    def _resolve_task_provider_id(self, task: Task) -> str:
        """Return effective provider id for a task using route metadata when present."""
        route_cfg = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        if isinstance(route_cfg, dict):
            provider_id = str(route_cfg.get("resolved_provider") or "").strip()
            if provider_id:
                return provider_id
        return self._provider_id

    def _resolve_base_url(self, provider_id: str) -> str:
        """Resolve provider base URL for the effective task provider."""
        if self._core is None:
            return self._base_url
        base_url = self._core.config.get(f"models.providers.{provider_id}.baseUrl")
        if not base_url:
            return self._base_url
        return str(base_url).rstrip("/")

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _forward_sync(
        self, client: httpx.AsyncClient, url: str, payload: dict, task: Task_agent, save_call: bool = False
    ) -> WorkerResult:
        """Non-streaming: send request, wait for full response, return it."""
        upstream_payload = {**payload, "stream": False}
        resp = await client.post(url, json=upstream_payload)

        if resp.status_code != 200:
            log(
                "worker",
                "warning",
                f"Task {task.id} upstream sync error: status={resp.status_code} body={resp.text[:512]!r}",
                "call_ollama",
            )
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
                log(
                    "worker",
                    "warning",
                    f"Task {task.id} upstream stream error: status={resp.status_code}",
                    "call_ollama",
                )
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
