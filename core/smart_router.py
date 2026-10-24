"""Shared smart-routing policy used by HTTP endpoints before task finalization."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import copy
import time
from typing import Awaitable, Callable

from core.kv_affinity import KvAffinityStore, KvFingerprint, ModelReadinessTracker


_REMOTE_PROBE_CACHE_TTL_SECONDS = 1.0


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
        check_resource_available_after_unload: Callable[[dict], bool] | None,
        probe_remote_model_queue_state: Callable[..., Awaitable[dict | None]],
        probe_ollama_model_availability: Callable[..., Awaitable[bool]] | None,
        resolve_probe_timeout_ms: Callable[[dict], int],
        check_resource_available_for_reuse: Callable[[dict, str, str], bool] | None = None,
        probe_openai_model_availability: Callable[..., Awaitable[bool]] | None = None,
        resolve_worker_id_for_route: Callable[[str, dict | None], str] | None = None,
        resolve_model_protocol: Callable[[str, str], str] | None = None,
        is_candidate_allowed: Callable[[str, str], bool] | None = None,
        on_selection: Callable[[dict], None] | None = None,
        on_failure: Callable[[dict, str, list[dict]], None] | None = None,
        kv_affinity: KvAffinityStore | None = None,
        readiness: ModelReadinessTracker | None = None,
        affinity_config: dict | None = None,
        get_model_queue_state: Callable[[str, str, int], Awaitable[dict | None]] | None = None,
        evaluate_model_admission: Callable[[dict, str, str], dict | None] | None = None,
    ) -> None:
        self._endpoint_id = str(endpoint_id or "").strip()
        self._default_worker_id = str(default_worker_id or "").strip()
        self._find_provider_model_cfg = find_provider_model_cfg
        self._provider_api = provider_api
        self._resolve_model_resource_requirements = resolve_model_resource_requirements
        self._get_local_queue_state = get_local_queue_state
        self._check_resource_available = check_resource_available
        self._check_resource_available_after_unload = check_resource_available_after_unload
        self._check_resource_available_for_reuse = check_resource_available_for_reuse
        self._probe_remote_model_queue_state = probe_remote_model_queue_state
        self._probe_ollama_model_availability = probe_ollama_model_availability
        self._probe_openai_model_availability = probe_openai_model_availability
        self._resolve_probe_timeout_ms = resolve_probe_timeout_ms
        self._resolve_worker_id_for_route = resolve_worker_id_for_route
        self._resolve_model_protocol = resolve_model_protocol
        self._is_candidate_allowed = is_candidate_allowed
        self._on_selection = on_selection
        self._on_failure = on_failure
        self._kv_affinity = kv_affinity
        self._readiness = readiness
        self._affinity_config = affinity_config if isinstance(affinity_config, dict) else {}
        self._get_model_queue_state = get_model_queue_state
        self._evaluate_model_admission = evaluate_model_admission
        self._remote_probe_cache: dict[tuple[str, str, int], tuple[float, dict | None]] = {}
        self._remote_probe_inflight: dict[tuple[str, str, int], asyncio.Task] = {}
        self._candidate_inflight: dict[tuple[str, str, int], asyncio.Task] = {}

    async def resolve_route(
        self,
        route: dict,
        *,
        request_payload: dict | None,
        request_priority: int | None = None,
        incoming_bearer_token: str = "",
        excluded_candidates: set[tuple[str, str]] | None = None,
    ) -> SmartRouteResolution:
        """Resolve one smart alias route, applying KV affinity when it is enabled."""
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
        if strategy not in {"first_available", "direct"}:
            raise SmartRouteError(
                code="UNSUPPORTED_ROUTING_STRATEGY",
                status_code=400,
                message=f"Unsupported smart routing strategy: {strategy}",
            )

        items = smart_model_cfg.get("items")
        if strategy == "direct" and (not isinstance(items, list) or len(items) != 1):
            raise SmartRouteError(
                code="INVALID_ROUTING_CONFIG",
                status_code=400,
                message=f"Direct smart model '{requested_model}' must have exactly one candidate",
            )
        if not isinstance(items, list) or not items:
            raise SmartRouteError(
                code="ROUTING_FAILED",
                status_code=503,
                message=f"Smart model '{requested_model}' has no routing candidates",
            )

        effective_priority = (
            self.resolve_request_priority(request_payload)
            if request_priority is None
            else self._normalize_priority(request_priority)
        )
        excluded = {
            (str(provider_id).strip(), str(model_id).strip())
            for provider_id, model_id in (excluded_candidates or set())
        }
        affinity_enabled = bool(self._kv_affinity is not None and self._kv_affinity.enabled)
        fingerprint = None
        affinity_record = None
        if affinity_enabled:
            fingerprint = self._kv_affinity.fingerprint(request_payload)
            if fingerprint is not None:
                affinity_record = self._kv_affinity.lookup(fingerprint)
                if affinity_record is None:
                    affinity_record = await self._kv_affinity.lookup_remote(fingerprint)

        candidates: list[dict] = []
        busy_candidates: list[dict] = []
        candidate_probes: list[dict] = []

        for index, item in enumerate(items):
            if isinstance(item, dict):
                candidate_key = (
                    str(item.get("provider") or "").strip(),
                    str(item.get("model") or "").strip(),
                )
                if candidate_key in excluded:
                    candidate_probes.append(
                        self.candidate_probe_record(
                            index,
                            item,
                            probe_ok=False,
                            reason="excluded_after_execution_failure",
                        )
                    )
                    continue
            candidate = await self._evaluate_candidate_shared(
                item,
                request_priority=effective_priority,
                index=index,
                incoming_bearer_token=incoming_bearer_token,
            )
            if candidate is None:
                candidate_probes.append(
                    self.candidate_probe_record(index, item, probe_ok=False, reason="invalid_candidate")
                )
                continue

            candidate_probes.append(self.candidate_probe_record(index, item, candidate=candidate))
            candidates.append(candidate)
            if not affinity_enabled and candidate["can_run_now"]:
                return self._finish_selection(
                    route,
                    candidate,
                    strategy=strategy,
                    reason="immediate",
                    candidate_probes=candidate_probes,
                    fingerprint=fingerprint,
                    affinity_record=affinity_record,
                    candidates=candidates,
                )
            if bool(candidate.get("routing_eligible", True)):
                busy_candidates.append(candidate)

        if affinity_enabled:
            for candidate in candidates:
                self._record_readiness(candidate)
            if isinstance(affinity_record, dict):
                preferred_provider = str(affinity_record.get("provider") or "")
                preferred_model = str(affinity_record.get("model") or "")
                preferred = next(
                    (
                        candidate
                        for candidate in candidates
                        if str(candidate.get("provider") or "") == preferred_provider
                        and str(candidate.get("model") or "") == preferred_model
                    ),
                    None,
                )
                if (
                    preferred is None
                    or not preferred.get("probe_ok")
                    or not bool(preferred.get("routing_eligible", True))
                    or str(preferred.get("cache_group") or "") != str(affinity_record.get("cache_group") or "")
                ):
                    self._kv_affinity.forget_candidate(preferred_provider, preferred_model)
                    affinity_record = None
            selected = self._select_with_affinity(candidates, affinity_record)
            if selected is not None:
                candidate, reason = selected
                return self._finish_selection(
                    route,
                    candidate,
                    strategy=strategy,
                    reason=reason,
                    candidate_probes=candidate_probes,
                    fingerprint=fingerprint,
                    affinity_record=affinity_record,
                    candidates=candidates,
                )

        if affinity_enabled:
            for candidate in candidates:
                if bool(candidate.get("can_run_now")):
                    return self._finish_selection(
                        route,
                        candidate,
                        strategy=strategy,
                        reason="immediate",
                        candidate_probes=candidate_probes,
                        fingerprint=fingerprint,
                        affinity_record=affinity_record,
                        candidates=candidates,
                    )

        if busy_candidates:
            selected = min(
                busy_candidates,
                key=lambda item: (int(item["fallback_prio"]), int(item["index"])),
            )
            return self._finish_selection(
                route,
                selected,
                strategy=strategy,
                reason="busy_fallback",
                candidate_probes=candidate_probes,
                fingerprint=fingerprint,
                affinity_record=affinity_record,
                candidates=candidates,
            )

        if self._on_failure is not None:
            self._on_failure(route, strategy=strategy, candidate_probes=candidate_probes)

        raise SmartRouteError(
            code="ROUTING_FAILED",
            status_code=503,
            message=f"No smart routing candidate is currently schedulable for model '{requested_model}'",
        )

    def _finish_selection(
        self,
        route: dict,
        candidate: dict,
        *,
        strategy: str,
        reason: str,
        candidate_probes: list[dict],
        fingerprint: KvFingerprint | None,
        affinity_record: dict | None,
        candidates: list[dict],
    ) -> SmartRouteResolution:
        """Build a resolution, remember it, and report the selection."""
        affinity_meta = self._affinity_metadata(
            fingerprint,
            affinity_record,
            candidate,
            reason,
            candidates,
        )
        if fingerprint is not None and self._kv_affinity is not None:
            self._kv_affinity.remember(
                fingerprint,
                provider=str(candidate.get("provider") or ""),
                model=str(candidate.get("model") or ""),
                cache_group=str(candidate.get("cache_group") or candidate.get("model") or ""),
                reason=reason,
            )
        resolution = self._build_resolution(
            route,
            candidate,
            strategy=strategy,
            reason=reason,
            candidate_probes=candidate_probes,
            affinity_meta=affinity_meta,
        )
        return resolution

    def _select_with_affinity(
        self,
        candidates: list[dict],
        record: dict | None,
    ) -> tuple[dict, str] | None:
        """Apply the affinity preference and return (candidate, reason)."""
        if not isinstance(record, dict):
            return None
        provider = str(record.get("provider") or "")
        model = str(record.get("model") or "")
        preferred = next(
            (
                candidate
                for candidate in candidates
                if str(candidate.get("provider") or "") == provider
                and str(candidate.get("model") or "") == model
            ),
            None,
        )
        if preferred is None or not preferred.get("probe_ok"):
            return None
        if not bool(preferred.get("routing_eligible", True)):
            return None
        if str(preferred.get("cache_group") or "") != str(record.get("cache_group") or ""):
            return None

        admission = self._candidate_admission(preferred)
        over_immediate = bool(self._affinity_config.get("prefer_affinity_over_immediate", True))
        if admission == "ready":
            if not self._queue_overloaded(preferred, candidates):
                return preferred, "affinity"
            if not over_immediate:
                return None
            alternative = self._best_alternative(candidates, preferred)
            return (alternative, "affinity_queue_diverted") if alternative else (preferred, "affinity")
        if admission == "concurrency":
            if not over_immediate:
                return None
            if not self._queue_overloaded(preferred, candidates):
                return preferred, "affinity_wait_queue"
            alternative = self._best_alternative(candidates, preferred)
            return (alternative, "affinity_queue_diverted") if alternative else (preferred, "affinity_wait_queue")
        if admission in {"session_memory", "base_memory", "waiting_unload"}:
            if not over_immediate:
                return None
            if not self._resource_wait_expired(preferred):
                return preferred, "affinity_wait_resource"
            alternative = self._best_alternative(candidates, preferred, require_ready=True)
            return (
                (alternative, "affinity_resource_timeout")
                if alternative
                else (preferred, "affinity_wait_no_alternative")
            )
        return None

    def _candidate_admission(self, candidate: dict) -> str:
        """Return the admission state of one evaluated candidate."""
        reason = str(candidate.get("admission") or "").strip()
        if reason:
            return reason
        if candidate.get("can_run_now"):
            return "ready"
        if candidate.get("probe_ok") and candidate.get("queue_state") is not None:
            return "concurrency"
        return "provider_down"

    def _queue_overloaded(self, preferred: dict, candidates: list[dict]) -> bool:
        """Return True when the preferred queue is beyond both configured thresholds."""
        try:
            max_diff_len = int(self._affinity_config.get("max_queue_diff_len", 8))
        except (TypeError, ValueError):
            max_diff_len = 8
        try:
            max_diff_part = float(self._affinity_config.get("max_queue_diff_part", 3.0))
        except (TypeError, ValueError):
            max_diff_part = 3.0
        if max_diff_part <= 1:
            max_diff_part = 3.0
        preferred_depth = int(preferred.get("queue_depth") or 0)
        other_depths = [
            int(candidate.get("queue_depth") or 0)
            for candidate in candidates
            if candidate is not preferred
            and bool(candidate.get("probe_ok"))
            and bool(candidate.get("routing_eligible", True))
        ]
        if not other_depths:
            return False
        minimum = min(other_depths)
        return (
            preferred_depth - minimum > max_diff_len
            and preferred_depth >= minimum * max_diff_part
        )

    def _best_alternative(
        self,
        candidates: list[dict],
        preferred: dict,
        *,
        require_ready: bool = False,
    ) -> dict | None:
        """Return the best admissible alternative, preferring immediate starts."""
        alternatives = [
            candidate
            for candidate in candidates
            if candidate is not preferred
            and bool(candidate.get("probe_ok"))
            and bool(candidate.get("routing_eligible", True))
            and (not require_ready or bool(candidate.get("can_run_now")))
        ]
        if not alternatives:
            return None
        return min(
            alternatives,
            key=lambda candidate: (
                0 if candidate.get("can_run_now") else 1,
                int(candidate.get("queue_depth") or 0),
                int(candidate.get("fallback_prio") or 0),
                int(candidate.get("index") or 0),
            ),
        )

    def _resource_wait_expired(self, candidate: dict) -> bool:
        """Return whether the readiness history says affinity should give up."""
        if self._readiness is None:
            return True
        try:
            warm = int(self._affinity_config.get("resource_wait_timeout_seconds", 60))
        except (TypeError, ValueError):
            warm = 60
        try:
            cold = int(self._affinity_config.get("cold_model_wait_timeout_seconds", 0))
        except (TypeError, ValueError):
            cold = 0
        try:
            ratio = float(self._affinity_config.get("resource_blocked_ratio", 0.5))
        except (TypeError, ValueError):
            ratio = 0.5
        return self._readiness.should_divert(
            str(candidate.get("provider") or ""),
            str(candidate.get("model") or ""),
            warm_timeout_seconds=warm,
            cold_timeout_seconds=cold,
            ratio_threshold=ratio,
        )

    def _record_readiness(self, candidate: dict) -> None:
        """Record one candidate admission observation."""
        if self._readiness is None:
            return
        admission = str(candidate.get("admission") or "").strip()
        if not admission:
            return
        self._readiness.record(
            str(candidate.get("provider") or ""),
            str(candidate.get("model") or ""),
            {
                "admission": admission,
                "model_loaded": candidate.get("model_loaded"),
                "free": candidate.get("free") or {},
                "needed": candidate.get("needed") or {},
            },
        )

    def _affinity_metadata(
        self,
        fingerprint: KvFingerprint | None,
        record: dict | None,
        selected: dict,
        reason: str,
        candidates: list[dict],
    ) -> dict:
        """Build compact route metadata for logs and diagnostics."""
        meta: dict = {
            "enabled": bool(self._kv_affinity is not None and self._kv_affinity.enabled),
            "hit": isinstance(record, dict),
            "selection_reason": reason,
            "selected": {
                "provider": str(selected.get("provider") or ""),
                "model": str(selected.get("model") or ""),
            },
            "queue_depths": {
                f"{candidate.get('provider')}/{candidate.get('model')}": int(candidate.get("queue_depth") or 0)
                for candidate in candidates
            },
        }
        if fingerprint is not None:
            meta.update(
                {
                    "total_bytes": fingerprint.total_bytes,
                    "anchor_bytes": fingerprint.anchor_bytes,
                    "hash": fingerprint.hash[:8],
                    "fingerprint": fingerprint.hash,
                }
            )
        if isinstance(record, dict):
            meta["preferred"] = {
                "provider": str(record.get("provider") or ""),
                "model": str(record.get("model") or ""),
                "anchor_bytes": int(record.get("anchor_bytes") or 0),
            }
        admission = str(selected.get("admission") or "")
        if admission:
            meta["admission"] = admission
            meta["model_loaded"] = selected.get("model_loaded")
            if self._readiness is not None:
                meta["blocked_for"] = round(
                    self._readiness.blocked_for(
                        str(selected.get("provider") or ""),
                        str(selected.get("model") or ""),
                    ),
                    3,
                )
                meta["blocked_ratio"] = round(
                    self._readiness.blocked_ratio(
                        str(selected.get("provider") or ""),
                        str(selected.get("model") or ""),
                    ),
                    3,
                )
        return meta

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

        try:
            fallback_prio = int(item.get("fallback_prio", index) or index)
        except (TypeError, ValueError):
            fallback_prio = index
        model_cfg = self._find_provider_model_cfg(provider_id, model_id) or {}
        cache_group = str(
            item.get("cache_group")
            or (model_cfg.get("cache_group") if isinstance(model_cfg, dict) else "")
            or model_id
        )
        base = {
            "provider": provider_id,
            "model": model_id,
            "index": index,
            "fallback_prio": fallback_prio,
            "cache_group": cache_group,
            "queue_depth": 0,
        }

        if self._is_candidate_allowed is not None and not self._is_candidate_allowed(provider_id, model_id):
            return {
                **base,
                "can_run_now": False,
                "queue_state": None,
                "probe_ok": False,
                "probe_source": "capability",
                "probe_latency_ms": 0,
                "probe_error": "candidate_not_allowed",
                "routing_eligible": False,
                "admission": "resource_disabled",
                "model_loaded": False,
            }

        started_at = time.perf_counter()
        requirements = self._resolve_model_resource_requirements(provider_id, model_id)
        timeout_ms = self._resolve_probe_timeout_ms(item)
        provider_api = self._provider_api(provider_id)

        if provider_api in {"openai", "opencode-go", "anthropic"} and self._probe_openai_model_availability is not None:
            probe_ok = bool(
                await self._probe_openai_model_availability(
                    provider_id,
                    model_id,
                    timeout_ms=timeout_ms,
                    incoming_bearer_token=incoming_bearer_token,
                )
            )
            if not probe_ok:
                return {
                    **base,
                    "can_run_now": False,
                    "queue_state": None,
                    "probe_ok": False,
                    "probe_source": f"{provider_api}_models",
                    "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                    "probe_error": "probe_failed",
                    "routing_eligible": False,
                    "admission": "provider_down",
                    "model_loaded": False,
                }
            return {
                **base,
                "can_run_now": True,
                "queue_state": None,
                "probe_ok": True,
                "probe_source": f"{provider_api}_models",
                "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                "routing_eligible": True,
                "admission": "ready",
                "model_loaded": True,
            }

        admission_result = None
        if requirements is not None and self._evaluate_model_admission is not None:
            try:
                admission_result = self._evaluate_model_admission(requirements, model_id, provider_id)
            except Exception:
                admission_result = None

        if (
            requirements is not None
            and provider_api != "openaix"
            and self._get_local_queue_state is not None
            and self._check_resource_available is not None
        ):
            queue_state = None
            if self._get_model_queue_state is not None:
                try:
                    queue_state = await self._get_model_queue_state(provider_id, model_id, request_priority)
                except Exception:
                    queue_state = None
            if not isinstance(queue_state, dict):
                queue_state = await self._get_local_queue_state(requirements, request_priority)
            if isinstance(queue_state, dict):
                queue_depth = int(queue_state.get("queued_count_total") or 0)
                blocked_by_same_or_higher = queue_depth - int(queue_state.get("queued_count_below_priority") or 0)
                resource_ready = bool(self._check_resource_available(requirements))
                resource_ready_after_unload = bool(
                    self._check_resource_available_after_unload(requirements)
                ) if self._check_resource_available_after_unload is not None else resource_ready
                resource_reusable = bool(
                    self._check_resource_available_for_reuse(requirements, model_id, provider_id)
                ) if self._check_resource_available_for_reuse is not None else False
                if provider_api in {"ollama", "llama-cpp"} and self._probe_ollama_model_availability is not None:
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
                            **base,
                            "can_run_now": False,
                            "queue_state": queue_state,
                            "queue_depth": queue_depth,
                            "probe_ok": False,
                            "probe_source": f"{provider_api}_http",
                            "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                            "probe_error": "probe_failed",
                            "routing_eligible": False,
                            "admission": "provider_down",
                            "model_loaded": bool((admission_result or {}).get("model_loaded")),
                        }

                if isinstance(admission_result, dict):
                    admission = str(admission_result.get("admission") or "")
                    can_run_now = bool(admission_result.get("admissible_now")) and blocked_by_same_or_higher == 0
                    return {
                        **base,
                        "can_run_now": can_run_now,
                        "queue_state": queue_state,
                        "queue_depth": queue_depth,
                        "probe_ok": True,
                        "probe_source": "local",
                        "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                        "routing_eligible": True,
                        "admission": admission,
                        "model_loaded": bool(admission_result.get("model_loaded")),
                        "active_count": int(admission_result.get("active_count") or 0),
                        "max_concurrent_requests": int(admission_result.get("max_concurrent_requests") or 1),
                        "session_resources": admission_result.get("session_resources") or {},
                        "free": admission_result.get("free") or {},
                        "needed": admission_result.get("needed") or {},
                        "resource_ready": bool(admission_result.get("resource_ready", resource_ready)),
                        "resource_ready_after_unload": bool(
                            admission_result.get("resource_ready_after_unload", resource_ready_after_unload)
                        ),
                        "resource_reusable": resource_reusable,
                    }

                can_run_now = (resource_ready_after_unload or resource_reusable) and blocked_by_same_or_higher == 0
                admission = "ready" if can_run_now else "concurrency"
                return {
                    **base,
                    "can_run_now": can_run_now,
                    "queue_state": queue_state,
                    "queue_depth": queue_depth,
                    "probe_ok": True,
                    "probe_source": "local" if provider_api not in {"ollama", "llama-cpp"} else f"{provider_api}_http",
                    "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                    "routing_eligible": True,
                    "admission": admission,
                    "model_loaded": resource_reusable,
                    "resource_ready": resource_ready,
                    "resource_ready_after_unload": resource_ready_after_unload,
                    "resource_reusable": resource_reusable,
                }

        if provider_api != "openaix":
            return {
                **base,
                "can_run_now": False,
                "queue_state": None,
                "probe_ok": False,
                "probe_source": "unavailable",
                "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                "probe_error": "candidate_not_locally_visible_and_not_openaix",
                "routing_eligible": False,
                "admission": "provider_down",
                "model_loaded": False,
            }

        queue_state = await self._probe_remote_cached(
            provider_id,
            model_id,
            priority=request_priority,
            timeout_ms=timeout_ms,
            incoming_bearer_token=incoming_bearer_token,
        )
        if not isinstance(queue_state, dict):
            return {
                **base,
                "can_run_now": False,
                "queue_state": None,
                "probe_ok": False,
                "probe_source": "remote_openaix",
                "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
                "probe_error": "probe_failed",
                "routing_eligible": False,
                "admission": "provider_down",
                "model_loaded": False,
            }

        queue_depth = int(queue_state.get("queued_count_total") or 0)
        can_run_now = bool(queue_state.get("can_run_now"))
        admission = str(queue_state.get("admission") or "") or ("ready" if can_run_now else "concurrency")
        return {
            **base,
            "can_run_now": can_run_now,
            "queue_state": queue_state,
            "queue_depth": queue_depth,
            "probe_ok": True,
            "probe_source": "remote_openaix",
            "probe_latency_ms": int((time.perf_counter() - started_at) * 1000),
            "routing_eligible": True,
            "admission": admission,
            "model_loaded": queue_state.get("model_loaded"),
            "active_count": queue_state.get("active_count"),
            "max_concurrent_requests": queue_state.get("max_concurrent_requests"),
            "session_resources": queue_state.get("session_resources") or {},
            "free": queue_state.get("free") or {},
            "needed": queue_state.get("needed") or {},
        }

    async def _evaluate_candidate_shared(
        self,
        item: dict,
        *,
        request_priority: int,
        index: int,
        incoming_bearer_token: str = "",
    ) -> dict | None:
        """Share one in-flight candidate probe among tasks waiting together."""
        provider_id = str((item or {}).get("provider") or "").strip()
        model_id = str((item or {}).get("model") or "").strip()
        key = (provider_id, model_id, int(request_priority))
        task = self._candidate_inflight.get(key)
        if task is None:
            task = asyncio.create_task(self.evaluate_candidate(
                item,
                request_priority=request_priority,
                index=index,
                incoming_bearer_token=incoming_bearer_token,
            ))
            self._candidate_inflight[key] = task
            owner = True
        else:
            owner = False
        try:
            candidate = await asyncio.shield(task)
        finally:
            if owner and task.done() and self._candidate_inflight.get(key) is task:
                self._candidate_inflight.pop(key, None)
        result = copy.deepcopy(candidate)
        if isinstance(result, dict):
            result["index"] = index
        return result

    async def _probe_remote_cached(
        self,
        provider_id: str,
        model_id: str,
        *,
        priority: int,
        timeout_ms: int,
        incoming_bearer_token: str = "",
    ) -> dict | None:
        """Return a short-lived remote probe result, sharing an in-flight probe."""
        key = (provider_id, model_id, int(priority))
        now = time.monotonic()
        cached = self._remote_probe_cache.get(key)
        if cached is not None and cached[0] > now:
            return copy.deepcopy(cached[1])

        task = self._remote_probe_inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._probe_remote_model_queue_state(
                provider_id,
                model_id,
                priority=priority,
                timeout_ms=timeout_ms,
                incoming_bearer_token=incoming_bearer_token,
            ))
            self._remote_probe_inflight[key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if task.done() and self._remote_probe_inflight.get(key) is task:
                self._remote_probe_inflight.pop(key, None)
        result = copy.deepcopy(result) if isinstance(result, dict) else result
        self._remote_probe_cache[key] = (time.monotonic() + _REMOTE_PROBE_CACHE_TTL_SECONDS, result)
        return result

    def _build_resolution(
        self,
        route: dict,
        candidate: dict,
        *,
        strategy: str,
        reason: str,
        candidate_probes: list[dict],
        affinity_meta: dict | None = None,
    ) -> SmartRouteResolution:
        """Build the final route metadata and resolved worker for the selected candidate."""
        route_result = self.build_route_result(
            route,
            candidate,
            strategy=strategy,
            reason=reason,
            candidate_probes=candidate_probes,
        )
        if isinstance(affinity_meta, dict):
            route_result["kv_affinity"] = affinity_meta
        if self._resolve_model_protocol is not None:
            route_result["resolved_protocol"] = str(
                self._resolve_model_protocol(
                    str(route_result.get("resolved_provider") or ""),
                    str(route_result.get("resolved_model") or ""),
                )
                or ""
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
            for key in ("admission", "model_loaded", "queue_depth", "active_count", "max_concurrent_requests"):
                if candidate.get(key) is not None:
                    out[key] = candidate.get(key)
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