"""Shared smart-routing policy used by HTTP endpoints before task finalization."""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Awaitable, Callable


class SmartRouteError(Exception):
    """Raised when smart-route resolution fails before queueing a task."""

    def __init__(self, *, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(slots=True)
class SmartRouteResolution:
    """Concrete routing result selected from a smart model alias."""

    resolved_provider: str
    resolved_model: str
    resolved_worker: str
    route: dict


class SmartRouter:
    """Resolve one smart-provider alias into a concrete provider/model/worker."""

    def __init__(
        self,
        *,
        endpoint_id: str,
        default_worker_id: str,
        find_provider_model_cfg: Callable[[str, str], dict],
        provider_api: Callable[[str], str],
        resolve_model_resource_requirements: Callable[[str, str], dict | None],
        get_local_queue_state: Callable[[dict, int], Awaitable[dict | None]] | None,
        check_resource_available: Callable[[dict], bool] | None,
        probe_remote_model_queue_state: Callable[..., Awaitable[dict | None]],
        probe_ollama_model_availability: Callable[..., Awaitable[bool]] | None,
        resolve_probe_timeout_ms: Callable[[dict], int],
        resolve_worker_id_for_route: Callable[[str, dict | None], str] | None = None,
        on_selection: Callable[[dict], None] | None = None,
        on_failure: Callable[[dict, str, list[dict]], None] | None = None,
    ) -> None:
        self._endpoint_id = str(endpoint_id or "").strip()
        self._default_worker_id = str(default_worker_id or "").strip()
        self._find_provider_model_cfg = find_provider_model_cfg
        self._provider_api = provider_api
        self._resolve_model_resource_requirements = resolve_model_resource_requirements
        self._get_local_queue_state = get_local_queue_state
        self._check_resource_available = check_resource_available
        self._probe_remote_model_queue_state = probe_remote_model_queue_state
        self._probe_ollama_model_availability = probe_ollama_model_availability
        self._resolve_probe_timeout_ms = resolve_probe_timeout_ms
        self._resolve_worker_id_for_route = resolve_worker_id_for_route
        self._on_selection = on_selection
        self._on_failure = on_failure

    async def resolve_route(
        self,
        route: dict,
        *,
        request_payload: dict | None,
        request_priority: int | None = None,
        incoming_bearer_token: str = "",
    ) -> SmartRouteResolution:
        """Resolve one smart alias route into a concrete provider/model/worker selection."""
        requested_provider = str(route.get("resolved_provider") or "").strip()
        requested_model = str(route.get("resolved_model") or "").strip()
        smart_model_cfg = self._find_provider_model_cfg(requested_provider, requested_model)
        if not isinstance(smart_model_cfg, dict):
            raise SmartRouteError(
                code="INVALID_MODEL",
                status_code=404,
                message=f"Unknown smart model: {requested_model}",
            )

        strategy = str(smart_model_cfg.get("type") or "first_available").strip() or "first_available"
        if strategy != "first_available":
            raise SmartRouteError(
                code="UNSUPPORTED_ROUTING_STRATEGY",
                status_code=400,
                message=f"Unsupported smart routing strategy: {strategy}",
            )

        items = smart_model_cfg.get("items")
        if not isinstance(items, list) or not items:
            raise SmartRouteError(
                code="ROUTING_FAILED",
                status_code=503,
                message=f"Smart model '{requested_model}' has no routing candidates",
            )

        effective_priority = self.resolve_request_priority(request_payload) if request_priority is None else self._normalize_priority(request_priority)
        busy_candidates: list[dict] = []
        candidate_probes: list[dict] = []

        for index, item in enumerate(items):
            candidate = await self.evaluate_candidate(
                item,
                request_priority=effective_priority,
                index=index,
                incoming_bearer_token=incoming_bearer_token,
            )
            if candidate is None:
                candidate_probes.append(self.candidate_probe_record(index, item, probe_ok=False, reason="invalid_candidate"))
                continue

            candidate_probes.append(self.candidate_probe_record(index, item, candidate=candidate))
            if candidate["can_run_now"]:
                resolution = self._build_resolution(
                    route,
                    candidate,
                    strategy=strategy,
                    reason="immediate",
                    candidate_probes=candidate_probes,
                )
                if self._on_selection is not None:
                    self._on_selection(resolution.route)
                return resolution

            if bool(candidate.get("routing_eligible", True)):
                busy_candidates.append(candidate)

        if busy_candidates:
            selected = min(
                busy_candidates,
                key=lambda item: (int(item["fallback_prio"]), int(item["index"])),
            )
            resolution = self._build_resolution(
                route,
                selected,
                strategy=strategy,
                reason="busy_fallback",
                candidate_probes=candidate_probes,
            )
            if self._on_selection is not None:
                self._on_selection(resolution.route)
            return resolution

        if self._on_failure is not None:
            self._on_failure(route, strategy=strategy, candidate_probes=candidate_probes)

        raise SmartRouteError(
            code="ROUTING_FAILED",
            status_code=503,
            message=f"No smart routing candidate is currently schedulable for model '{requested_model}'",
        )

    async def evaluate_candidate(
        self,
        item: object,
        *,
        request_priority: int,
        index: int,
        incoming_bearer_token: str = "",
    ) -> dict | None:
        """Evaluate one smart-routing candidate using local state or remote probing."""
        if not isinstance(item, dict):
            return None

        provider_id = str(item.get("provider") or "").strip()
        model_id = str(item.get("model") or "").strip()
        if not provider_id or not model_id:
            return None

        started_at = time.perf_counter()
        requirements = self._resolve_model_resource_requirements(provider_id, model_id)
        fallback_prio = item.get("fallback_prio", index)
        try:
            fallback_prio = int(fallback_prio)
        except Exception:
            fallback_prio = int(index)
        timeout_ms = self._resolve_probe_timeout_ms(item)

        if requirements is not None and self._get_local_queue_state is not None and self._check_resource_available is not None:
            queue_state = await self._get_local_queue_state(requirements, request_priority)
            if isinstance(queue_state, dict):
                resource_ready = bool(self._check_resource_available(requirements))
                blocked_by_same_or_higher = int(queue_state.get("queued_count_total") or 0) - int(queue_state.get("queued_count_below_priority") or 0)
                provider_api = self._provider_api(provider_id)
                if provider_api == "ollama" and self._probe_ollama_model_availability is not None:
                    probe_ok = bool(
                        await self._probe_ollama_model_availability(
                            provider_id,
                            model_id,
                            timeout_ms=timeout_ms,
                            incoming_bearer_token=incoming_bearer_token,
                        )
                    )
                    if not probe_ok:
                        return {
                            "provider": provider_id,
                            "model": model_id,
                            "index": index,
                            "fallback_prio": fallback_prio,
                            "can_run_now": False,
                            "queue_state": queue_state,
                            "probe_ok": False,
                            "probe_source": "ollama_http",
                            "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                            "probe_error": "probe_failed",
                            "routing_eligible": False,
                        }
                return {
                    "provider": provider_id,
                    "model": model_id,
                    "index": index,
                    "fallback_prio": fallback_prio,
                    "can_run_now": resource_ready and blocked_by_same_or_higher == 0,
                    "queue_state": queue_state,
                    "probe_ok": True,
                    "probe_source": "local" if provider_api != "ollama" else "ollama_http",
                    "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                    "routing_eligible": True,
                }

        if self._provider_api(provider_id) != "openaix":
            return {
                "provider": provider_id,
                "model": model_id,
                "index": index,
                "fallback_prio": fallback_prio,
                "can_run_now": False,
                "queue_state": None,
                "probe_ok": False,
                "probe_source": "unavailable",
                "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                "probe_error": "candidate_not_locally_visible_and_not_openaix",
                "routing_eligible": False,
            }

        queue_state = await self._probe_remote_model_queue_state(
            provider_id,
            model_id,
            priority=request_priority,
            timeout_ms=timeout_ms,
            incoming_bearer_token=incoming_bearer_token,
        )
        if not isinstance(queue_state, dict):
            return {
                "provider": provider_id,
                "model": model_id,
                "index": index,
                "fallback_prio": fallback_prio,
                "can_run_now": False,
                "queue_state": None,
                "probe_ok": False,
                "probe_source": "remote_openaix",
                "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                "probe_error": "probe_failed",
                "routing_eligible": False,
            }

        return {
            "provider": provider_id,
            "model": model_id,
            "index": index,
            "fallback_prio": fallback_prio,
            "can_run_now": bool(queue_state.get("can_run_now")),
            "queue_state": queue_state,
            "probe_ok": True,
            "probe_source": "remote_openaix",
            "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
            "routing_eligible": True,
        }

    def _build_resolution(
        self,
        route: dict,
        candidate: dict,
        *,
        strategy: str,
        reason: str,
        candidate_probes: list[dict],
    ) -> SmartRouteResolution:
        """Build the final route metadata and resolved worker for the selected candidate."""
        route_result = self.build_route_result(
            route,
            candidate,
            strategy=strategy,
            reason=reason,
            candidate_probes=candidate_probes,
        )
        resolved_worker = self._default_worker_id
        if self._resolve_worker_id_for_route is not None:
            resolved_worker = str(self._resolve_worker_id_for_route(self._default_worker_id, route_result) or self._default_worker_id)
        route_result["resolved_worker"] = resolved_worker
        return SmartRouteResolution(
            resolved_provider=str(route_result.get("resolved_provider") or ""),
            resolved_model=str(route_result.get("resolved_model") or ""),
            resolved_worker=resolved_worker,
            route=route_result,
        )

    @staticmethod
    def build_route_result(
        route: dict,
        candidate: dict,
        *,
        strategy: str,
        reason: str,
        candidate_probes: list[dict],
    ) -> dict:
        """Build task route metadata for a selected smart-routing candidate."""
        return {
            "requested_model": route.get("requested_model") or route.get("resolved_model"),
            "requested_alias": route.get("requested_alias") or route.get("requested_model"),
            "requested_provider": route.get("resolved_provider"),
            "resolved_provider": candidate["provider"],
            "resolved_model": candidate["model"],
            "selection": "smart_route",
            "strategy": strategy,
            "selection_reason": reason,
            "candidate_index": candidate["index"],
            "fallback_prio": candidate["fallback_prio"],
            "candidate_probes": candidate_probes,
        }

    @staticmethod
    def candidate_probe_record(
        index: int,
        item: object,
        *,
        probe_ok: bool | None = None,
        reason: str = "",
        candidate: dict | None = None,
    ) -> dict:
        """Build a compact candidate probe record for logs and task metadata."""
        item_dict = item if isinstance(item, dict) else {}
        out = {
            "index": index,
            "provider": str(item_dict.get("provider") or "").strip(),
            "model": str(item_dict.get("model") or "").strip(),
        }
        if candidate is not None:
            out.update(
                {
                    "probe_ok": bool(candidate.get("probe_ok")),
                    "can_run_now": bool(candidate.get("can_run_now")),
                    "probe_source": str(candidate.get("probe_source") or ""),
                    "probe_latency_ms": int(candidate.get("probe_latency_ms") or 0),
                    "fallback_prio": int(candidate.get("fallback_prio") or index),
                }
            )
            queue_state = candidate.get("queue_state")
            if isinstance(queue_state, dict):
                out["queued_count_total"] = int(queue_state.get("queued_count_total") or 0)
                out["queued_count_below_priority"] = int(queue_state.get("queued_count_below_priority") or 0)
            probe_error = candidate.get("probe_error")
            if isinstance(probe_error, str) and probe_error:
                out["probe_error"] = probe_error
            return out

        out["probe_ok"] = bool(probe_ok)
        if reason:
            out["probe_error"] = reason
        return out

    @staticmethod
    def resolve_request_priority(payload: dict | None) -> int:
        """Resolve request priority with the same default used by queued tasks."""
        try:
            return max(0, int((payload or {}).get("priority", 5)))
        except Exception:
            return 5

    @staticmethod
    def _normalize_priority(value: object) -> int:
        """Normalize an explicitly provided routing priority."""
        try:
            return max(0, int(value))
        except Exception:
            return 5