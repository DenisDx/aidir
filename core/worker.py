"""
Base worker interface.
All workers must expose a module-level `worker` instance of a BaseWorker subclass.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from core import log
from core.task import Task
from core.task import STATUS_CANCELED, STATUS_COMPLETED, STATUS_FAILED
from core.upstream_response import UpstreamResponse


@dataclass
class WorkerResult:
    """Return value of BaseWorker.execute()."""
    ok: bool
    data: dict | None = None    # final response payload (non-streaming or summary)
    error: dict | None = None   # error dict if ok=False
    usage: dict | None = None   # token usage stats, if available
    upstream_response: UpstreamResponse | None = None


class BaseWorker:
    """
    Abstract base for all workers.
    Subclasses must set `task_type` and implement `execute()`.
    """

    id: str = ""
    task_type: str = ""
    enabled: bool = True
    is_async: bool = False
    tags: list[str] = field(default_factory=list)
    _core: Any | None = None
    _MAX_LLM_CALL_HISTORY: int = 20
    _LLM_DIAGNOSTICS_MIN_INTERVAL_SECONDS: float = 1.0

    async def initialize(self, config: dict) -> None:
        """Called by core at startup with the worker's config section."""
        self._core = config.get("_core")

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """
        Execute a task.
        For stream tasks call emit_chunk(chunk_dict) for each response chunk.
        Raise an exception or return WorkerResult(ok=False) on error.
        """
        raise NotImplementedError(f"Worker {self.id} has no execute() implementation")

    def _provider_config(self, provider_id: str) -> dict:
        """Return one provider config, safe for ids that contain dots."""
        config = getattr(self._core, "config", None) if self._core is not None else None
        getter = getattr(config, "get", None)
        if callable(getter):
            try:
                providers = getter("models.providers")
            except Exception:
                providers = None
            if isinstance(providers, dict):
                provider = providers.get(str(provider_id or "").strip())
                return provider if isinstance(provider, dict) else {}
        raw = getattr(config, "raw", None)
        if callable(raw):
            try:
                data = raw()
            except Exception:
                data = None
            providers = ((data or {}).get("models") or {}).get("providers") if isinstance(data, dict) else None
            if isinstance(providers, dict):
                provider = providers.get(str(provider_id or "").strip())
                return provider if isinstance(provider, dict) else {}
        return {}

    def bind_child_task(
        self,
        child_task: Task,
        parent_task: Task | None = None,
        parent_context: dict[str, Any] | None = None,
        parent_worker_id: str | None = None,
    ) -> Task:
        """Attach the current worker as callback parent and preserve the previous chain in history."""
        frame: dict[str, Any] = {
            "parent_worker": getattr(parent_task, "parent_worker", None),
            "parent_context": dict(getattr(parent_task, "parent_context", {}) or {}),
        }

        context = dict(parent_context or {})
        context["history"] = frame

        if parent_task is not None:
            context.setdefault("task_id", parent_task.id)
            context.setdefault("source_worker", self.id)

        child_task.parent_worker = parent_worker_id or self.id
        child_task.parent_context = context
        return child_task

    async def on_child_task(self, task: Task) -> None:
        """Handle a child task status change and propagate terminal callbacks upward."""
        if task.status not in {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED}:
            return None

        parent_worker_id = task.parent_worker
        if not parent_worker_id:
            return None

        core = self._core
        workers = getattr(core, "workers", None) if core is not None else None
        parent_worker = workers.get(parent_worker_id) if isinstance(workers, dict) else None
        if parent_worker is None:
            log("system", "critical", f"parent_worker {parent_worker_id} not found for task {task.id}")
            return None

        history = (task.parent_context or {}).get("history") if isinstance(task.parent_context, dict) else None
        if not isinstance(history, dict):
            return None

        task.parent_worker = history.get("parent_worker") or None
        task.parent_context = history.get("parent_context") or {}

        await parent_worker.on_child_task(task)
        return None

    async def _register_llm_call(self, task: Task) -> int:
        """Increment task-local LLM call counter and persist it when queue access is available."""
        core = self._core
        queue = getattr(core, "queue", None) if core is not None else None
        increment = getattr(queue, "increment_llm_call_count", None)
        if callable(increment):
            try:
                return await increment(task)
            except Exception as exc:
                log("worker", "warning", f"Failed to persist llm_call_count for task {task.id}: {exc}", self.id or "worker")

        task.llm_call_count = int(getattr(task, "llm_call_count", 0) or 0) + 1
        return task.llm_call_count

    async def _persist_llm_call_diagnostics(self, task: Task, *, force: bool = False) -> None:
        """Persist task LLM diagnostics at a bounded rate, or immediately when forced."""
        now = time.monotonic()
        last_persisted = float(getattr(task, "_last_llm_diagnostics_persist_at", 0.0) or 0.0)
        if not force and now - last_persisted < self._LLM_DIAGNOSTICS_MIN_INTERVAL_SECONDS:
            return
        task._last_llm_diagnostics_persist_at = now

        core = self._core
        queue = getattr(core, "queue", None) if core is not None else None
        persist = getattr(queue, "persist_llm_call_diagnostics", None)
        if callable(persist):
            try:
                await persist(task)
            except Exception as exc:
                log("worker", "warning", f"Failed to persist llm_call_history for task {task.id}: {exc}", self.id or "worker")

    async def _begin_llm_call(
        self,
        task: Task,
        *,
        url: str,
        payload: dict | None = None,
        provider_id: str = "",
        save_call: bool = False,
        request_body: bytes | None = None,
        request_content_type: str = "application/json",
    ) -> dict[str, Any]:
        """Append one compact LLM-call summary entry to the task and persist it."""
        request_payload = payload if isinstance(payload, dict) else {}
        preview_payload = request_payload
        if not request_payload.get("messages") and isinstance(getattr(task, "payload", None), dict):
            preview_payload = {**request_payload, "messages": task.payload.get("messages", [])}
        messages = preview_payload.get("messages") if isinstance(preview_payload.get("messages"), list) else []
        input_value = request_payload.get("input")
        input_count = 0
        if isinstance(input_value, str):
            input_count = 1
        elif isinstance(input_value, list):
            input_count = len(input_value)

        started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        task.llm_call_count = int(getattr(task, "llm_call_count", 0) or 0) + 1

        parsed_url = urlparse(url)
        request_kind = ""
        if isinstance(getattr(task, "config", None), dict):
            request_kind = str(task.config.get("request_kind") or "").strip()

        entry: dict[str, Any] = {
            "call_index": task.llm_call_count,
            "started_at": started_at,
            "mono_started_at": time.monotonic(),
            "worker_id": self.id,
            "provider_id": provider_id,
            "request_kind": request_kind or "chat",
            "url_path": parsed_url.path or url,
            "model": str(request_payload.get("model") or ""),
            "stream": bool(request_payload.get("stream")),
            "message_count": len(messages),
            "last_role": str(messages[-1].get("role") or "") if messages and isinstance(messages[-1], dict) else "",
            "has_tools": bool(request_payload.get("tools")),
            "input_count": input_count,
            "request_text": self._extract_llm_request_text(preview_payload),
            "request_preview": self._summarize_llm_request(preview_payload),
            "save_llm_request": bool(save_call),
            "status": "started",
        }
        if save_call:
            entry["request"] = request_payload

        core = getattr(self, "_core", None)
        audit_log = getattr(core, "audit_log", None) if core is not None else None
        if audit_log is not None:
            try:
                event = audit_log.record_body_event(
                    "llm_request",
                    request_body if request_body is not None else json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    task_id=task.id,
                    exchange_id=f"{task.id}:llm:{task.llm_call_count}",
                    exchange_number=task.llm_call_count,
                    worker_id=self.id,
                    provider_id=provider_id,
                    model=entry["model"],
                    http={"method": "POST", "url": url, "content_type": request_content_type},
                )
                entry["audit_request_event_id"] = event["event_id"]
            except Exception as exc:
                log("audit", "error", f"Failed to audit LLM request task={task.id}: {exc}", self.id or "worker")

        history = list(getattr(task, "llm_call_history", []) or [])
        history.append(entry)
        if not save_call and len(history) > self._MAX_LLM_CALL_HISTORY:
            history = history[-self._MAX_LLM_CALL_HISTORY :]
        task.llm_call_history = history
        entry = task.llm_call_history[-1]
        await self._persist_llm_call_diagnostics(task, force=True)
        return entry

    @staticmethod
    def _mark_first_delta(entry: dict[str, Any] | None) -> None:
        """Record the moment of the first generated delta of a streamed call."""
        if isinstance(entry, dict) and not entry.get("first_delta_at"):
            entry["first_delta_at"] = time.monotonic()

    @staticmethod
    def _ttft_ms(entry: dict[str, Any]) -> int:
        """Return the time to the first token of a streamed call, in milliseconds."""
        first = entry.get("first_delta_at")
        started = entry.get("mono_started_at")
        if not first or not started:
            return 0
        try:
            return max(0, int((float(first) - float(started)) * 1000))
        except (TypeError, ValueError):
            return 0

    async def _finalize_llm_call(
        self,
        task: Task,
        entry: dict[str, Any] | None,
        *,
        status: str,
        http_status: int | None = None,
        error_code: str = "",
        response: dict | None = None,
        response_body: bytes | None = None,
        response_spool=None,
        response_content_type: str = "application/json",
        ttft_ms: int | None = None,
    ) -> None:
        """Finalize one compact LLM-call summary entry and persist it."""
        if not isinstance(entry, dict):
            return

        entry["status"] = status
        finished_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        entry["finished_at"] = finished_at

        started_at_raw = entry.get("started_at")
        try:
            started_at = datetime.fromisoformat(str(started_at_raw))
            finished_dt = datetime.fromisoformat(finished_at)
            entry["duration_ms"] = max(0, int((finished_dt - started_at).total_seconds() * 1000))
        except Exception:
            entry["duration_ms"] = 0

        if http_status is not None:
            entry["http_status"] = int(http_status)
        if error_code:
            entry["error_code"] = error_code
        if ttft_ms is None:
            ttft_ms = self._ttft_ms(entry)
        if ttft_ms:
            entry["ttft_ms"] = int(ttft_ms)

        summary = self._summarize_llm_response(response)
        if summary:
            entry["response_summary"] = summary
        if entry.get("save_llm_request") and isinstance(response, dict):
            entry["response"] = response

        await self._record_usage_event(
            task,
            entry,
            status=status,
            http_status=http_status,
            error_code=error_code,
            response=response,
            response_body=response_body,
            ttft_ms=ttft_ms,
        )

        core = getattr(self, "_core", None)
        audit_log = getattr(core, "audit_log", None) if core is not None else None
        if audit_log is not None:
            try:
                event_fields = {
                    "task_id": task.id,
                    "exchange_id": f"{task.id}:llm:{entry.get('call_index', 0)}",
                    "exchange_number": entry.get("call_index"),
                    "worker_id": self.id,
                    "provider_id": entry.get("provider_id"),
                    "model": entry.get("model"),
                    "http": {"status_code": http_status, "url_path": entry.get("url_path"), "content_type": response_content_type},
                    "terminal_status": status,
                    "error_code": error_code or None,
                }
                if response_spool is not None:
                    event = audit_log.finalize_body_spool(
                        "llm_response",
                        response_spool,
                        content_type=response_content_type,
                        **event_fields,
                    )
                else:
                    event = audit_log.record_body_event(
                        "llm_response",
                        response_body if response_body is not None else json.dumps(response or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                        content_type=response_content_type,
                        **event_fields,
                    )
                entry["audit_response_event_id"] = event["event_id"]
            except Exception as exc:
                log("audit", "error", f"Failed to audit LLM response task={task.id}: {exc}", self.id or "worker")

        await self._persist_llm_call_diagnostics(task, force=True)

    def _model_alias(self, provider_id: str, model_id: str) -> str:
        """Return the client-facing alias of a concrete provider model."""
        provider = self._provider_config(provider_id)
        for model_cfg in provider.get("models") or []:
            if not isinstance(model_cfg, dict):
                continue
            if str(model_cfg.get("id") or "").strip() != str(model_id or "").strip():
                continue
            return str(model_cfg.get("alias") or "").strip()
        return ""

    def _smart_alias(self, task: Task) -> tuple[str, str]:
        """Return the smart alias a task was routed through, if it was one."""
        config = getattr(task, "config", None)
        route = config.get("route") if isinstance(config, dict) else None
        if not isinstance(route, dict):
            return "", ""
        if str(route.get("selection") or "") != "smart_route":
            return "", ""
        provider_id = str(route.get("requested_provider") or "").strip()
        model_id = str(route.get("requested_alias") or route.get("requested_model") or "").strip()
        if not provider_id or not model_id:
            return "", ""
        return provider_id, model_id

    async def _record_usage_event(
        self,
        task: Task,
        entry: dict[str, Any],
        *,
        status: str,
        http_status: int | None,
        error_code: str,
        response: dict | None,
        response_body: bytes | None,
        ttft_ms: int | None,
    ) -> None:
        """Send one finished LLM call to the usage store.

        The store keeps aggregates only. A call that produced no answer adds
        its failure class and its duration, and no tokens.
        """
        core = getattr(self, "_core", None)
        store = getattr(core, "usage_store", None) if core is not None else None
        if store is None or not getattr(store, "enabled", False):
            return

        from core.billing import classify_failure, extract_usage_fields

        call_index = int(entry.get("call_index") or 0)
        provider_id = str(entry.get("provider_id") or "")
        model_id = str(entry.get("model") or "")
        fields = extract_usage_fields(response)
        status_group = "success" if status == "ok" else ("canceled" if status == "cancelled" else "failure")
        error_text = ""
        if isinstance(response_body, (bytes, bytearray)):
            error_text = bytes(response_body[:4096]).decode("utf-8", errors="replace")
        failure_class = ""
        if status_group == "failure":
            failure_class = classify_failure(
                error_code=error_code,
                http_status=http_status,
                finish_reason=fields.get("finish_reason") or "",
                error_body=error_text,
                empty_response=bool(fields.get("empty_response")),
            )

        computed_cost = 0
        cache_savings = 0
        if fields.get("prompt_tokens"):
            from core.usage_store import resolve_model_prices

            prices = resolve_model_prices(
                self._core.config.raw() if self._core is not None else {},
                provider_id=provider_id,
                model_id=model_id,
                prompt_tokens=int(fields.get("prompt_tokens") or 0),
            )
            from core.billing import cache_savings_micros, compute_cost_micros

            computed_cost = compute_cost_micros(
                prices,
                prompt_tokens=int(fields.get("prompt_tokens") or 0),
                cached_prompt_tokens=int(fields.get("cached_prompt_tokens") or 0),
                cache_write_tokens=int(fields.get("cache_write_tokens") or 0),
                completion_tokens=int(fields.get("completion_tokens") or 0),
            )
            cache_savings = cache_savings_micros(
                prices,
                prompt_tokens=int(fields.get("prompt_tokens") or 0),
                cached_prompt_tokens=int(fields.get("cached_prompt_tokens") or 0),
            )

        reported_cost = fields.get("reported_cost_micros")
        allowance = fields.get("allowance_micros")
        effective_money = allowance if allowance is not None else (reported_cost if reported_cost is not None else computed_cost)

        alias_provider, alias_model = self._smart_alias(task)
        route = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        protocol = str((route or {}).get("resolved_protocol") or "") if isinstance(route, dict) else ""
        request_kind = str(entry.get("request_kind") or "chat")

        event = {
            "usage_event_id": f"{task.id}:{call_index}",
            "task_id": task.id,
            "call_index": call_index,
            "provider_id": provider_id,
            "model_id": model_id,
            "model_alias": self._model_alias(provider_id, model_id),
            "alias_provider_id": alias_provider,
            "alias_model_id": alias_model,
            "protocol": protocol,
            "request_kind": request_kind,
            "stream": bool(entry.get("stream")),
            "started_at": str(entry.get("started_at") or ""),
            "finished_at": str(entry.get("finished_at") or ""),
            "status": status,
            "status_group": status_group,
            "failure_class": failure_class,
            "error_code": error_code,
            "http_status": http_status,
            "scope": "external" if call_index <= 1 else "internal",
            "client_request": call_index <= 1,
            "total_ms": int(entry.get("duration_ms") or 0),
            "ttft_ms": int(ttft_ms if ttft_ms is not None else entry.get("ttft_ms") or 0),
            "queue_ms": 0,
            "prefill_ms": int(fields.get("prefill_ms") or 0),
            "generation_ms": int(fields.get("generation_ms") or 0),
            "timing_source": fields.get("timing_source") or "total_only",
            "prompt_tokens": int(fields.get("prompt_tokens") or 0),
            "cached_prompt_tokens": int(fields.get("cached_prompt_tokens") or 0),
            "cache_write_tokens": int(fields.get("cache_write_tokens") or 0),
            "completion_tokens": int(fields.get("completion_tokens") or 0),
            "reasoning_tokens": int(fields.get("reasoning_tokens") or 0),
            "answer_tokens": int(fields.get("answer_tokens") or 0),
            "tool_calls": int(fields.get("tool_calls") or 0),
            "tool_argument_tokens_estimated": int(fields.get("tool_argument_tokens_estimated") or 0),
            "input_files": 0,
            "input_file_bytes": 0,
            "reported_cost_micros": reported_cost,
            "computed_cost_micros": computed_cost,
            "allowance_micros": allowance,
            "list_cost_micros": fields.get("list_cost_micros"),
            "effective_money_micros": effective_money,
            "cache_savings_micros": cache_savings,
            "billing_source": fields.get("billing_source") or "",
            "cost_estimated": bool(fields.get("cost_estimated")),
            "envid": self._call_envid(task),
        }
        try:
            store.record_call(event)
        except Exception as exc:
            log("core", "warning", f"Failed to record usage event: {exc}", self.id or "worker")

    def _call_envid(self, task: Task) -> str:
        """Return the envid of a call, when it is a known envid of the registry."""
        raw = ""
        payload = getattr(task, "payload", None)
        if isinstance(payload, dict):
            raw = str(payload.get("envid") or "")
        if not raw:
            config = getattr(task, "config", None)
            client = config.get("client") if isinstance(config, dict) else None
            if isinstance(client, dict):
                raw = str(client.get("envid") or "")
        envid = raw.strip()
        if not envid:
            return ""
        registry = getattr(self._core, "envid_registry", None) if self._core is not None else None
        if registry is None:
            return ""
        try:
            if registry.get(envid) is None:
                return ""
        except Exception:
            return ""
        return envid

    async def _finalize_latest_started_llm_call(
        self,
        task: Task,
        *,
        status: str,
        error_code: str = "",
        response_body: bytes | None = None,
        response_spool=None,
        response_content_type: str = "application/json",
    ) -> None:
        """Finalize the most recent started LLM-call entry when an outer exception interrupts execution."""
        history = list(getattr(task, "llm_call_history", []) or [])
        for entry in reversed(history):
            if isinstance(entry, dict) and entry.get("status") == "started":
                await self._finalize_llm_call(
                    task,
                    entry,
                    status=status,
                    error_code=error_code,
                    response_body=response_body,
                    response_spool=response_spool,
                    response_content_type=response_content_type,
                )
                return

    @staticmethod
    def _summarize_llm_response(response: dict | None) -> dict[str, Any]:
        """Build a compact response summary safe to persist inside task metadata."""
        if not isinstance(response, dict):
            return {}

        summary: dict[str, Any] = {}
        for key in ("done_reason", "total_duration", "load_duration", "prompt_eval_count", "eval_count"):
            value = response.get(key)
            if value is not None:
                summary[key] = value

        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = response.get(key)
            if value is None:
                value = usage.get(key)
            if value is not None:
                summary[key] = value

        message = response.get("message") if isinstance(response.get("message"), dict) else {}
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        if tool_calls:
            summary["tool_call_count"] = len(tool_calls)

        embeddings = response.get("embeddings") if isinstance(response.get("embeddings"), list) else []
        if embeddings:
            summary["embeddings_count"] = len(embeddings)

        stream_chunks = response.get("stream_chunks") if isinstance(response.get("stream_chunks"), list) else []
        if stream_chunks:
            summary["stream_chunk_count"] = len(stream_chunks)

        # Provider billing and token details, kept so the viewer and the usage
        # store do not have to parse the raw body again.
        details = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        opencode = details.get("opencode") if isinstance(details.get("opencode"), dict) else {}
        for key in ("cost", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = details.get(key)
            if value is not None:
                summary[key] = value
        for key in ("billing_source", "list_cost", "allowance", "estimated"):
            value = opencode.get(key)
            if value is not None:
                summary[key] = value
        prompt_details = details.get("prompt_tokens_details") if isinstance(details.get("prompt_tokens_details"), dict) else {}
        completion_details = (
            details.get("completion_tokens_details") if isinstance(details.get("completion_tokens_details"), dict) else {}
        )
        if prompt_details.get("cached_tokens") is not None:
            summary["cached_prompt_tokens"] = prompt_details["cached_tokens"]
        if completion_details.get("reasoning_tokens") is not None:
            summary["reasoning_tokens"] = completion_details["reasoning_tokens"]

        timings = response.get("timings") if isinstance(response.get("timings"), dict) else {}
        for key in ("prompt_ms", "predicted_ms"):
            if timings.get(key) is not None:
                summary[key] = timings[key]

        return summary

    @classmethod
    def _summarize_llm_request(cls, payload: dict[str, Any] | None) -> str:
        """Build a compact preview of the outbound LLM request."""
        return cls._extract_llm_request_text(payload, limit=240)

    @classmethod
    def _extract_llm_request_text(cls, payload: dict[str, Any] | None, limit: int = 4000) -> str:
        """Extract a readable text form of the outbound LLM request."""
        if not isinstance(payload, dict):
            return ""

        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        if messages:
            last_message = messages[-1] if isinstance(messages[-1], dict) else {}
            role = str(last_message.get("role") or "").strip()
            content_preview = cls._summarize_content(last_message.get("content"), limit=limit)
            if content_preview and role:
                return cls._clip_text(f"{role}: {content_preview}", limit)
            if content_preview:
                return cls._clip_text(content_preview, limit)

        input_preview = cls._summarize_content(payload.get("input"), limit=limit)
        if input_preview:
            return cls._clip_text(input_preview, limit)

        model = str(payload.get("model") or "").strip()
        if model:
            return cls._clip_text(f"model={model}", limit)
        return ""

    @classmethod
    def _summarize_content(cls, content: Any, limit: int = 240) -> str:
        """Flatten common message/input structures into a short plain-text preview."""
        if isinstance(content, str):
            return cls._clip_text(content, limit)

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                part = cls._summarize_content(item, limit=limit)
                if part:
                    parts.append(part)
                if len(" ".join(parts)) >= limit:
                    break
            return cls._clip_text(" ".join(parts), limit)

        if isinstance(content, dict):
            item_type = str(content.get("type") or "").strip()
            if item_type in {"text", "input_text"}:
                return cls._summarize_content(content.get("text"), limit=limit)
            if item_type in {"image", "input_image", "image_url"}:
                return "[image]"
            if item_type in {"tool_result", "tool_response"}:
                tool_name = str(content.get("tool_name") or content.get("name") or "tool").strip()
                return f"[{tool_name} result]"
            if "content" in content:
                return cls._summarize_content(content.get("content"), limit=limit)
            if "text" in content:
                return cls._summarize_content(content.get("text"), limit=limit)
            if item_type:
                return f"[{item_type}]"

        return ""

    @staticmethod
    def _clip_text(value: Any, limit: int) -> str:
        """Normalize whitespace and clip text to a bounded length."""
        text = " ".join(str(value or "").split())
        if not text:
            return ""
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"


class BaseToolWorker(BaseWorker):
    """Base class for tool workers that can self-describe their MCP contract."""

    task_type: str = "tool"

    def get_tool_description(self) -> list[dict[str, Any]]:
        """
        Return a list of MCP-style tool metadata dicts.
        Each dict: name, description, inputSchema, etc.
        For single-tool workers, return a list with one dict.
        """
        return [{
            "name": self.id,
            "description": f"Tool {self.id}",
            "inputSchema": {"type": "object", "properties": {}},
        }]
