"""
Usage API for the WebUI: summaries, per-model rows, series, speeds, and limits.

The routes are mounted by the WebUI backend and use the session of the
WebUI. Every route answers from the usage store; a missing or unhealthy store
never turns into an error of the page.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

# Presets of the period bar.
PERIOD_PRESETS: tuple[str, ...] = ("today", "yesterday", "24h", "7d", "30d", "month")

# Micro-units in one currency unit.
_MICROS = 1_000_000


def resolve_period(
    preset: str = "",
    start_raw: str = "",
    end_raw: str = "",
    *,
    tz: Any = timezone.utc,
    now: datetime | None = None,
) -> tuple[datetime, datetime, str]:
    """Return the start, the end, and the label of a requested period.

    The default is the last 24 hours. The explicit timestamps win over the
    preset.
    """
    moment = now or datetime.now(timezone.utc)
    local = moment.astimezone(tz)

    if start_raw and end_raw:
        start = _parse_timestamp(start_raw, local)
        end = _parse_timestamp(end_raw, local)
        if start and end and end > start:
            return start, end, "custom"

    name = str(preset or "24h").strip().lower()
    if name == "today":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        label = "today"
    elif name == "yesterday":
        start = (local - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        label = "yesterday"
    elif name == "7d":
        start = local - timedelta(days=7)
        end = moment
        label = "7d"
    elif name == "30d":
        start = local - timedelta(days=30)
        end = moment
        label = "30d"
    elif name == "month":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = moment
        label = "month"
    else:
        start = moment - timedelta(hours=24)
        end = moment
        label = "24h"
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), label


def _parse_timestamp(value: str, fallback: datetime) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning None when it is not one."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _store_of(core: Any) -> Any:
    """Return the usage store of a core, or None."""
    store = getattr(core, "usage_store", None)
    if store is None or not getattr(store, "enabled", False):
        return None
    return store


def _scope_of(params: Any) -> str:
    """Return the call scope of a request: all, external, or internal."""
    scope = str(params.get("scope") or "all").strip().lower()
    return scope if scope in {"all", "external", "internal"} else "all"


def _tz_of(store: Any) -> Any:
    """Return the timezone the store resolves the periods in."""
    return getattr(store, "_tz", timezone.utc)


def _unavailable(reason: str) -> dict[str, Any]:
    """Return the payload of an unavailable usage store."""
    return {"available": False, "reason": reason, "data": {}}


def mount_usage_api(
    app: Any,
    core: Any,
    *,
    require_session: Callable[..., Any] | None = None,
) -> None:
    """Mount the usage routes of the WebUI API.

    The routes need an authenticated session, so the session dependency of
    the host application is passed in.
    """
    dependency = require_session if require_session is not None else (lambda: None)

    def _period(request: Request, store: Any) -> tuple[datetime, datetime, str]:
        """Resolve the period of a request against the store timezone."""
        params = request.query_params
        return resolve_period(
            str(params.get("period") or "24h"),
            str(params.get("from") or ""),
            str(params.get("to") or ""),
            tz=_tz_of(store),
        )

    @app.get("/api/usage/summary")
    async def usage_summary(request: Request, session: dict = Depends(dependency)):
        """Return the totals of the selected period."""
        store = _store_of(core)
        if store is None:
            return JSONResponse(_unavailable("usage store is not available"), status_code=200)
        start, end, label = _period(request, store)
        summary = store.summary(start, end, scope=_scope_of(request.query_params))
        rows = store.model_rows(start, end, scope=_scope_of(request.query_params))
        totals = summary.get("totals") or {}
        prompt_tokens = int(totals.get("prompt_tokens") or 0)
        cached_tokens = int(totals.get("cached_prompt_tokens") or 0)
        billable_prompt = max(0, prompt_tokens - cached_tokens)
        return JSONResponse({
            "available": True,
            "period": {"label": label, "from": start.isoformat(), "to": end.isoformat()},
            "scope": summary.get("scope"),
            "currency": summary.get("currency"),
            "calls": int(totals.get("calls") or 0),
            "external_calls": int(totals.get("external_calls") or 0),
            "internal_calls": int(totals.get("internal_calls") or 0),
            "client_requests": int(totals.get("client_requests") or 0),
            "successes": int(totals.get("successes") or 0),
            "failures": int(totals.get("failures") or 0),
            "canceled": int(totals.get("canceled") or 0),
            "tokens": {
                "prompt": prompt_tokens,
                "prompt_billable": billable_prompt,
                "cached_prompt": cached_tokens,
                "cache_write": int(totals.get("cache_write_tokens") or 0),
                "completion": int(totals.get("completion_tokens") or 0),
                "reasoning": int(totals.get("reasoning_tokens") or 0),
                "answer": int(totals.get("answer_tokens") or 0),
                "tool_argument_estimated": int(totals.get("tool_argument_tokens_estimated") or 0),
            },
            "cache": {
                "hit_rate": round(cached_tokens / prompt_tokens, 6) if prompt_tokens else 0.0,
                "savings": int(totals.get("cache_savings") or 0),
            },
            "cost": {
                "reported": int(totals.get("reported_cost") or 0),
                "computed": int(totals.get("computed_cost") or 0),
                "allowance": int(totals.get("allowance") or 0),
                "list_cost": int(totals.get("list_cost") or 0),
            },
            "speed": {
                "ttft_ms_avg": int(totals.get("ttft_ms_sum") or 0) // max(1, int(totals.get("calls") or 1)),
                "prefill_tokens_per_second": _rate(totals.get("prompt_tokens"), totals.get("prefill_ms_sum")),
                "generation_tokens_per_second": _rate(totals.get("completion_tokens"), totals.get("generation_ms_sum")),
            },
            "models": rows,
        })

    @app.get("/api/usage/models")
    async def usage_models(request: Request, session: dict = Depends(dependency)):
        """Return one row per model of the selected period."""
        store = _store_of(core)
        if store is None:
            return JSONResponse(_unavailable("usage store is not available"), status_code=200)
        start, end, label = _period(request, store)
        rows = store.model_rows(start, end, scope=_scope_of(request.query_params))
        sort = str(request.query_params.get("sort") or "calls").strip().lower()
        keys = {
            "cost": lambda row: -(row.get("computed_cost") or 0),
            "tokens": lambda row: -(row.get("prompt_tokens") or 0),
            "speed": lambda row: (row.get("generation_tokens_per_second") or 0),
            "failures": lambda row: -(row.get("failures") or 0),
            "calls": lambda row: -(row.get("calls") or 0),
        }
        rows.sort(key=keys.get(sort, keys["calls"]))
        return JSONResponse({
            "available": True,
            "period": {"label": label, "from": start.isoformat(), "to": end.isoformat()},
            "scope": _scope_of(request.query_params),
            "currency": store.currency,
            "models": rows,
        })

    @app.get("/api/usage/series")
    async def usage_series(request: Request, session: dict = Depends(dependency)):
        """Return the non-empty buckets of the selected period."""
        store = _store_of(core)
        if store is None:
            return JSONResponse(_unavailable("usage store is not available"), status_code=200)
        start, end, label = _period(request, store)
        interval = str(request.query_params.get("interval") or "hour").strip().lower()
        if interval not in {"hour", "day", "month"}:
            interval = "hour"
        return JSONResponse({
            "available": True,
            "period": {"label": label, "from": start.isoformat(), "to": end.isoformat()},
            "interval": interval,
            "buckets": store.series(start, end, interval=interval),
        })

    @app.get("/api/usage/speeds")
    async def usage_speeds(request: Request, session: dict = Depends(dependency)):
        """Return the speed profiles of the selected period."""
        store = _store_of(core)
        if store is None:
            return JSONResponse(_unavailable("usage store is not available"), status_code=200)
        start, end, label = _period(request, store)
        return JSONResponse({
            "available": True,
            "period": {"label": label, "from": start.isoformat(), "to": end.isoformat()},
            "speeds": store.speeds(
                start,
                end,
                provider_id=str(request.query_params.get("provider") or ""),
                model_id=str(request.query_params.get("model") or ""),
            ),
        })

    @app.get("/api/usage/limits")
    async def usage_limits(session: dict = Depends(dependency)):
        """Return the state of every configured limit rule."""
        store = _store_of(core)
        if store is None:
            return JSONResponse(_unavailable("usage store is not available"), status_code=200)
        return JSONResponse({"available": True, "limits": store.limits_state()})

    @app.post("/api/usage/limits/reset")
    async def usage_limits_reset(payload: dict | None = None, session: dict = Depends(dependency)):
        """Clear the counters of one limit rule and its blocked state."""
        store = _store_of(core)
        if store is None:
            return JSONResponse(_unavailable("usage store is not available"), status_code=200)
        body = payload if isinstance(payload, dict) else {}
        rule_id = str(body.get("rule_id") or "").strip()
        counter = str(body.get("counter") or "all").strip().lower()
        if not rule_id:
            return JSONResponse({"available": True, "reset": False, "error": "rule_id is required"}, status_code=400)
        if counter not in {"tokens", "money", "all"}:
            return JSONResponse({"available": True, "reset": False, "error": "invalid counter"}, status_code=400)
        ok = store.reset_limit(rule_id, counter)
        return JSONResponse({"available": True, "reset": bool(ok), "rule_id": rule_id, "counter": counter})

    @app.get("/api/usage/allowance")
    async def usage_allowance(request: Request, session: dict = Depends(dependency)):
        """Return the provider-reported allowance of the selected period."""
        store = _store_of(core)
        if store is None:
            return JSONResponse(_unavailable("usage store is not available"), status_code=200)
        start, end, label = _period(request, store)
        return JSONResponse({
            "available": True,
            "period": {"label": label, "from": start.isoformat(), "to": end.isoformat()},
            "allowance": store.allowance_rows(start, end),
        })

    @app.get("/api/usage/health")
    async def usage_health(session: dict = Depends(dependency)):
        """Return the metering health of the usage store."""
        store = _store_of(core)
        if store is None:
            return JSONResponse({"available": False, "healthy": False, "reason": "usage store is not available"})
        health = store.health()
        health["available"] = True
        return JSONResponse(health)


def _rate(tokens: Any, milliseconds: Any) -> float | None:
    """Return a token rate per second, or None when the time is unknown."""
    token_count = int(tokens or 0)
    total_ms = int(milliseconds or 0)
    if not token_count or not total_ms:
        return None
    return round(token_count / (total_ms / 1000), 3)
