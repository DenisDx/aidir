"""
Pricing, limit, and failure-classification rules for model usage metering.

The module is pure: it computes prices, costs, bucket names, window bounds,
and failure classes from plain data. It never touches the database, the
configuration, or the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

# One dollar is one million micro-dollars. Money is stored as integers in that
# unit, so a durable total never depends on binary floating point.
MICROS_PER_UNIT = 1_000_000

# Prompt-size intervals used for speed comparison.
CONTEXT_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (0, 10_000, "0-10k"),
    (10_001, 50_000, "10-50k"),
    (50_001, 100_000, "50-100k"),
    (100_001, 200_000, "100-200k"),
    (200_001, 350_000, "200-350k"),
    (350_001, 500_000, "350-500k"),
    (500_001, 750_000, "500-750k"),
    (750_001, 0, "750k+"),
)

# Stable failure classes. The aggregate tables keep one counter per class.
FAILURE_CLASSES: tuple[str, ...] = (
    "length_finish",
    "http_4xx",
    "http_5xx",
    "timeout",
    "unreachable",
    "invalid_response",
    "rate_limited",
    "allowance_exhausted",
    "canceled",
    "empty_response",
    "refused",
    "budget_exceeded",
    "model_unavailable",
    "tool_loop_limit",
    "other_error",
)

# Outcome groups of an aggregate row.
STATUS_GROUPS: tuple[str, ...] = ("success", "failure", "canceled")

# Error codes of the service that map to a class without further inspection.
_ERROR_CODE_CLASSES: dict[str, str] = {
    "UPSTREAM_TIMEOUT": "timeout",
    "TIMEOUT": "timeout",
    "QUEUE_TIMEOUT": "timeout",
    "UPSTREAM_UNREACHABLE": "unreachable",
    "CONNECT_ERROR": "unreachable",
    "CONNECTION_ERROR": "unreachable",
    "UNAVAILABLE": "unreachable",
    "UPSTREAM_INVALID_JSON": "invalid_response",
    "CANCELLED": "canceled",
    "CANCELED": "canceled",
    "HOOK_CANCELED": "canceled",
    "MODEL_STATE_UNAVAILABLE": "model_unavailable",
    "RESOURCES_UNAVAILABLE": "model_unavailable",
    "REMOTE_CANNOT_RUN": "model_unavailable",
    "LLAMA_CPP_START_FAILED": "model_unavailable",
    "TOOL_LOOP_LIMIT": "tool_loop_limit",
    "BUDGET_EXCEEDED": "budget_exceeded",
}

# Markers of a provider billing refusal inside an upstream error body.
_BILLING_MARKERS: tuple[str, ...] = (
    "insufficient_quota",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "enforced_spend_limit_reached",
    "payment_required",
    "billing_error",
    "credit balance is too low",
    "insufficient credits",
    "usage limits",
)

# Allowed limit actions, from the least to the most restrictive.
LIMIT_ACTIONS: tuple[str, ...] = ("warn", "route", "block")

# Allowed limit window modes.
LIMIT_MODES: tuple[str, ...] = ("rolling", "calendar")

_PERIOD_UNITS: dict[str, str] = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


class LimitRuleError(ValueError):
    """Raised when a limit rule of the configuration is not valid."""


@dataclass(frozen=True)
class PriceSet:
    """Prices of one model, in currency units per million tokens."""

    prefill: Decimal = Decimal(0)
    cached_prefill: Decimal = Decimal(0)
    generation: Decimal = Decimal(0)
    cache_write: Decimal = Decimal(0)

    @property
    def is_empty(self) -> bool:
        """Return whether no price of the set is configured."""
        return not (self.prefill or self.cached_prefill or self.generation or self.cache_write)


@dataclass(frozen=True)
class LimitRule:
    """One configured limit rule, bound to a scope of the model catalogue."""

    id: str
    scope_type: str            # "instance", "provider", or "model"
    scope_id: str              # provider id, model key, or empty for the instance
    period: str                # normalized duration, for example "5h"
    mode: str                  # "rolling" or "calendar"
    max_prefill_tokens: int | None = None
    max_cached_prefill_tokens: int | None = None
    max_generation_tokens: int | None = None
    max_money: Decimal | None = None
    action: str = "warn"

    @property
    def blocks(self) -> bool:
        """Return whether the rule refuses requests when it is exceeded."""
        return self.action == "block"

    @property
    def excludes_candidate(self) -> bool:
        """Return whether the rule makes a smart candidate ineligible."""
        return self.action in {"block", "route"}


@dataclass(frozen=True)
class LimitUsage:
    """Accumulated usage of one limit rule inside its current window."""

    rule_id: str
    scope_id: str
    prefill_tokens: int = 0
    cached_prefill_tokens: int = 0
    generation_tokens: int = 0
    money_micros: int = 0
    blocked: bool = False

    def exceeded_by(self, rule: LimitRule) -> str:
        """Return the name of the first exceeded dimension of the rule, or an empty string."""
        if rule.max_prefill_tokens is not None and self.prefill_tokens > rule.max_prefill_tokens:
            return "max_prefill_tokens"
        if (
            rule.max_cached_prefill_tokens is not None
            and self.cached_prefill_tokens > rule.max_cached_prefill_tokens
        ):
            return "max_cached_prefill_tokens"
        if rule.max_generation_tokens is not None and self.generation_tokens > rule.max_generation_tokens:
            return "max_generation_tokens"
        if rule.max_money is not None and Decimal(self.money_micros) > rule.max_money * MICROS_PER_UNIT:
            return "max_money"
        return ""


def context_bucket(prompt_tokens: int | None) -> str:
    """Return the context bucket name of a prompt size."""
    tokens = max(0, int(prompt_tokens or 0))
    for low, high, name in CONTEXT_BUCKETS:
        if high and tokens > high:
            continue
        if tokens < low:
            continue
        return name
    return CONTEXT_BUCKETS[-1][2]


def to_decimal(value: Any) -> Decimal:
    """Convert a configuration value to a Decimal, returning zero for invalid input."""
    if value is None or isinstance(value, bool):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def micros(value: Any) -> int:
    """Convert a currency amount to integer micro-units, rounding half up."""
    return int((to_decimal(value) * MICROS_PER_UNIT).quantize(Decimal(1)))


def resolve_prices(
    prices_cfg: dict | None,
    tiers: list | None = None,
    prompt_tokens: int = 0,
) -> PriceSet:
    """Return the price set of a model for a prompt size.

    The optional tiers are checked in order; the first tier whose prompt range
    contains the prompt size wins. Without a matching tier the flat prices are
    used.
    """
    prices_cfg = prices_cfg if isinstance(prices_cfg, dict) else {}
    tokens = max(0, int(prompt_tokens or 0))

    for tier in tiers or []:
        if not isinstance(tier, dict):
            continue
        minimum = tier.get("min_prompt_tokens")
        maximum = tier.get("max_prompt_tokens")
        if minimum is not None and tokens < int(minimum):
            continue
        if maximum is not None and tokens > int(maximum):
            continue
        tier_prices = tier.get("prices")
        if isinstance(tier_prices, dict):
            return _price_set(tier_prices)
    return _price_set(prices_cfg)


def _price_set(prices_cfg: dict) -> PriceSet:
    """Build a price set from a prices mapping."""
    return PriceSet(
        prefill=to_decimal(prices_cfg.get("prefill_per_million")),
        cached_prefill=to_decimal(prices_cfg.get("cached_prefill_per_million")),
        generation=to_decimal(prices_cfg.get("generation_per_million")),
        cache_write=to_decimal(prices_cfg.get("cache_write_per_million")),
    )


def compute_cost_micros(
    prices: PriceSet,
    *,
    prompt_tokens: int = 0,
    cached_prompt_tokens: int = 0,
    cache_write_tokens: int = 0,
    completion_tokens: int = 0,
) -> int:
    """Return the computed cost of one call in integer micro-units.

    The cached prompt tokens are billed at the cached price and are removed
    from the billable prefill tokens.
    """
    prompt = max(0, int(prompt_tokens or 0))
    cached = min(prompt, max(0, int(cached_prompt_tokens or 0)))
    billable_prefill = prompt - cached
    billable_generation = max(0, int(completion_tokens or 0))
    cache_write = max(0, int(cache_write_tokens or 0))

    total = (
        Decimal(billable_prefill) * prices.prefill
        + Decimal(cached) * prices.cached_prefill
        + Decimal(billable_generation) * prices.generation
        + Decimal(cache_write) * prices.cache_write
    )
    return int(total.quantize(Decimal(1)))


def cache_savings_micros(
    prices: PriceSet,
    *,
    prompt_tokens: int = 0,
    cached_prompt_tokens: int = 0,
) -> int:
    """Return what the cached prompt tokens saved, in integer micro-units."""
    prompt = max(0, int(prompt_tokens or 0))
    cached = min(prompt, max(0, int(cached_prompt_tokens or 0)))
    if not cached:
        return 0
    difference = prices.prefill - prices.cached_prefill
    if difference <= 0:
        return 0
    total = Decimal(cached) * difference
    return int(total.quantize(Decimal(1)))


def classify_failure(
    *,
    error_code: str = "",
    http_status: int | None = None,
    finish_reason: str = "",
    error_body: str = "",
    empty_response: bool = False,
) -> str:
    """Return the failure class of one LLM call.

    The finish reason decides a length stop and a refusal, the error code of
    the service decides the transport and local classes, and the HTTP status
    of the provider decides the rest. An upstream body is inspected only when
    the code and the status are both generic.
    """
    reason = str(finish_reason or "").strip().lower()
    if reason == "length":
        return "length_finish"
    if reason in {"content_filter", "refusal", "safety"}:
        return "refused"

    code = str(error_code or "").strip().upper()
    mapped = _ERROR_CODE_CLASSES.get(code)
    if mapped:
        return mapped

    body = str(error_body or "").lower()
    billing = any(marker in body for marker in _BILLING_MARKERS)

    status = int(http_status or 0)
    if status == 402:
        return "allowance_exhausted"
    if status == 429:
        return "allowance_exhausted" if billing else "rate_limited"
    if status == 400 and billing:
        return "allowance_exhausted"
    if 400 <= status < 500:
        return "http_4xx"
    if status >= 500:
        return "http_5xx"

    if empty_response:
        return "empty_response"
    if code or status:
        return "other_error"
    return ""


def parse_period(value: Any) -> str:
    """Normalize a limit period into a duration string, rejecting ambiguous values.

    Accepted forms are a number with a unit suffix, such as "5h", and the
    explicit ISO-8601-like forms "PT5H" and "P1D". The result keeps the
    compact "5h" spelling.
    """
    text = str(value or "").strip()
    if not text:
        raise LimitRuleError("limit period is empty")
    if text.startswith("P") or text.startswith("PT"):
        return _parse_iso_period(text)
    if len(text) < 2:
        raise LimitRuleError(f"invalid limit period: {value}")
    unit = text[-1]
    number = text[:-1]
    if unit == "M":
        try:
            amount = float(number)
        except ValueError as exc:
            raise LimitRuleError(f"invalid limit period: {value}") from exc
        if amount <= 0:
            raise LimitRuleError(f"invalid limit period: {value}")
        return f"{int(amount) if amount.is_integer() else amount}M"
    unit = unit.lower()
    if unit not in _PERIOD_UNITS:
        raise LimitRuleError(f"invalid limit period: {value}")
    try:
        amount = float(number)
    except ValueError as exc:
        raise LimitRuleError(f"invalid limit period: {value}") from exc
    if amount <= 0:
        raise LimitRuleError(f"invalid limit period: {value}")
    if unit == "m" and amount == 1:
        raise LimitRuleError("ambiguous limit period '1m': use '1min' or a unit without the minute suffix")
    if unit == "s" and amount == 1:
        raise LimitRuleError("ambiguous limit period '1s': use '1sec'")
    return f"{int(amount) if amount.is_integer() else amount}{unit}"


def _parse_iso_period(text: str) -> str:
    """Convert an ISO-8601-like duration into the compact period spelling."""
    body = text[1:]
    days = 0
    hours = 0
    minutes = 0
    if "D" in body:
        day_part, _, body = body.partition("D")
        try:
            days = int(day_part)
        except ValueError as exc:
            raise LimitRuleError(f"invalid limit period: {text}") from exc
    if "T" in body:
        body = body.split("T", 1)[1]
        number = ""
        for char in body:
            if char.isdigit() or char == ".":
                number += char
                continue
            if char == "H":
                hours += int(float(number or 0))
                number = ""
                continue
            if char == "M":
                minutes += int(float(number or 0))
                number = ""
                continue
            raise LimitRuleError(f"invalid limit period: {text}")
        if number:
            raise LimitRuleError(f"invalid limit period: {text}")
    if minutes:
        raise LimitRuleError(f"invalid limit period: {text}: minutes are not supported")
    if days:
        return f"{days}d"
    if hours:
        return f"{hours}h"
    raise LimitRuleError(f"invalid limit period: {text}")


def period_delta(period: str) -> timedelta:
    """Return the duration of a normalized limit period."""
    text = str(period or "").strip()
    if len(text) < 2:
        raise LimitRuleError(f"invalid limit period: {period}")
    unit = text[-1]
    number = text[:-1]
    if unit == "M":
        try:
            amount = float(number)
        except ValueError as exc:
            raise LimitRuleError(f"invalid limit period: {period}") from exc
        return timedelta(days=amount * 30)
    unit = unit.lower()
    if unit not in _PERIOD_UNITS:
        raise LimitRuleError(f"invalid limit period: {period}")
    try:
        amount = float(number)
    except ValueError as exc:
        raise LimitRuleError(f"invalid limit period: {period}") from exc
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    return timedelta(seconds=amount)


def window_bounds(
    period: str,
    mode: str,
    now: datetime,
    *,
    tz: timezone | Any = timezone.utc,
) -> tuple[datetime, datetime]:
    """Return the start and the end of the window of a limit rule.

    A rolling window looks back exactly one period. A calendar window is
    aligned to the configured timezone: a day starts at midnight, a week at
    Monday, a month at the first day, and a sub-day period at the matching
    block of the day.
    """
    delta = period_delta(period)
    if mode == "calendar":
        start = _calendar_start(period, now, tz)
        return start, start + delta
    return now - delta, now


def _calendar_start(period: str, now: datetime, tz: Any) -> datetime:
    """Return the aligned start of a calendar window."""
    local = now.astimezone(tz)
    delta = period_delta(period)
    if delta.days >= 28:
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc)
    if delta.days >= 7:
        start = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start.astimezone(timezone.utc)
    if delta.days >= 1:
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc)
    hours = max(1, int(delta.total_seconds() // 3600))
    block = (local.hour // hours) * hours
    start = local.replace(hour=block, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def parse_limit_rules(
    rules: Any,
    *,
    scope_type: str,
    scope_id: str = "",
    seen_ids: set[str] | None = None,
) -> list[LimitRule]:
    """Parse the limit rules of one configuration scope.

    Every rule must have an id, a period, and a mode; a duplicated id inside
    the same scope is a configuration error. The returned rules keep the
    configuration order.
    """
    if rules is None:
        return []
    if not isinstance(rules, list):
        raise LimitRuleError("limits must be a list of rules")

    parsed: list[LimitRule] = []
    for item in rules:
        if not isinstance(item, dict):
            raise LimitRuleError("every limit rule must be an object")
        rule_id = str(item.get("id") or "").strip()
        if not rule_id:
            raise LimitRuleError("every limit rule needs an id")
        if seen_ids is not None:
            if rule_id in seen_ids:
                raise LimitRuleError(f"duplicate limit rule id: {rule_id}")
            seen_ids.add(rule_id)
        mode = str(item.get("mode") or "rolling").strip().lower()
        if mode not in LIMIT_MODES:
            raise LimitRuleError(f"invalid limit mode: {item.get('mode')}")
        action = str(item.get("action") or "warn").strip().lower()
        if action not in LIMIT_ACTIONS:
            raise LimitRuleError(f"invalid limit action: {item.get('action')}")
        money_metric = str(item.get("money_metric") or "auto").strip().lower()
        if money_metric != "auto":
            raise LimitRuleError(f"invalid money metric: {item.get('money_metric')}")
        parsed.append(
            LimitRule(
                id=rule_id,
                scope_type=scope_type,
                scope_id=scope_id,
                period=parse_period(item.get("period")),
                mode=mode,
                max_prefill_tokens=_optional_int(item.get("max_prefill_tokens")),
                max_cached_prefill_tokens=_optional_int(item.get("max_cached_prefill_tokens")),
                max_generation_tokens=_optional_int(item.get("max_generation_tokens")),
                max_money=_optional_decimal(item.get("max_money")),
                action=action,
            )
        )
    return parsed


def _optional_int(value: Any) -> int | None:
    """Return an optional non-negative integer, or None when the field is absent."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise LimitRuleError(f"invalid token limit: {value}") from exc
    if number < 0:
        raise LimitRuleError(f"invalid token limit: {value}")
    return number


def _optional_decimal(value: Any) -> Decimal | None:
    """Return an optional non-negative money limit, or None when the field is absent."""
    if value is None:
        return None
    amount = to_decimal(value)
    if amount < 0:
        raise LimitRuleError(f"invalid money limit: {value}")
    return amount


def scope_key(scope_type: str, scope_id: str) -> str:
    """Return the stable key of a rule scope."""
    return f"{scope_type}:{scope_id}" if scope_id else scope_type


def rules_for_call(
    rules: Iterable[LimitRule],
    *,
    provider_id: str,
    model_key: str,
    alias_key: str = "",
) -> list[LimitRule]:
    """Return the limit rules that apply to one LLM call.

    A provider rule applies to every call of that provider, a model rule
    applies to the concrete model and to the smart alias it was configured on,
    and an instance rule applies to every call.
    """
    applicable: list[LimitRule] = []
    for rule in rules:
        if rule.scope_type == "instance":
            applicable.append(rule)
        elif rule.scope_type == "provider" and rule.scope_id == provider_id:
            applicable.append(rule)
        elif rule.scope_type == "model" and rule.scope_id in {model_key, alias_key} and rule.scope_id:
            applicable.append(rule)
    return applicable


def limit_message(rule: LimitRule, dimension: str, used: int, limit: int) -> str:
    """Return the operator-facing message of an exceeded limit."""
    if dimension == "max_money":
        return f"Budget limit '{rule.id}' exceeded: money {used}/{limit} micro-units"
    return f"Budget limit '{rule.id}' exceeded: {dimension} {used}/{limit}"


# Approximate characters per token used to measure the tool-call text.
CHARS_PER_TOKEN = 4


def estimate_tool_argument_tokens(tool_calls: Any) -> int:
    """Return the approximate token count of the generated tool calls.

    The tool-call names and arguments are serialized and measured in
    characters. The result is an estimate and is stored as one.
    """
    if not isinstance(tool_calls, list) or not tool_calls:
        return 0
    characters = 0
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or call.get("name") or "")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = "" if arguments is None else str(arguments)
        characters += len(name) + len(arguments)
    if characters <= 0:
        return 0
    return max(1, characters // CHARS_PER_TOKEN)


def _first_int(source: dict, *names: str) -> int:
    """Return the first positive integer value of the named fields, or zero."""
    for name in names:
        value = source.get(name)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0


def _usage_sources(response: dict) -> list[dict]:
    """Return the response parts that may carry the usage and the timings.

    A streamed answer delivers its counters in the chunk that carries them,
    which is not always the last one, so the parts are returned in the order
    of their priority.
    """
    sources: list[dict] = []
    for key in ("usage_chunk", "final"):
        value = response.get(key)
        if isinstance(value, dict):
            sources.append(value)
    sources.append(response)
    chunks = response.get("stream_chunks")
    if isinstance(chunks, list):
        for item in reversed(chunks):
            if isinstance(item, dict):
                sources.append(item)
    return sources


def extract_usage_fields(response: dict | None) -> dict[str, Any]:
    """Return the metering fields of one internal response.

    The input is the internal response shape, which keeps the provider usage
    object when the provider sent one. The output holds the token counters,
    the provider billing fields, the timing, and the finish reason.
    """
    source = response if isinstance(response, dict) else {}
    sources = _usage_sources(source)

    usage: dict = {}
    timings: dict = {}
    for part in sources:
        part_usage = part.get("usage") if isinstance(part.get("usage"), dict) else {}
        if part_usage and not usage:
            usage = part_usage
        part_timings = part.get("timings") if isinstance(part.get("timings"), dict) else {}
        if part_timings and not timings:
            timings = part_timings

    opencode = usage.get("opencode") if isinstance(usage.get("opencode"), dict) else {}
    opencode_tokens = opencode.get("tokens") if isinstance(opencode.get("tokens"), dict) else {}

    prompt_tokens = _first_int(source, "prompt_eval_count")
    if not prompt_tokens:
        prompt_tokens = _first_int(usage, "prompt_tokens", "input_tokens")
    completion_tokens = _first_int(source, "eval_count")
    if not completion_tokens:
        completion_tokens = _first_int(usage, "completion_tokens", "output_tokens")

    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    completion_details = (
        usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    )

    cached_prompt_tokens = _first_int(prompt_details, "cached_tokens")
    if not cached_prompt_tokens:
        cached_prompt_tokens = _first_int(usage, "cache_read_input_tokens")
    if not cached_prompt_tokens:
        cached_prompt_tokens = _first_int(opencode_tokens, "cache_read")
    if not cached_prompt_tokens:
        cached_prompt_tokens = _first_int(timings, "cache_n")

    cache_write_tokens = _first_int(usage, "cache_creation_input_tokens")
    if not cache_write_tokens:
        cache_write_tokens = _first_int(opencode_tokens, "cache_write_5m", "cache_write_1h")

    reasoning_tokens = _first_int(completion_details, "reasoning_tokens")
    if not reasoning_tokens:
        reasoning_tokens = _first_int(opencode_tokens, "reasoning")

    message = source.get("message") if isinstance(source.get("message"), dict) else {}
    if not message:
        for part in sources:
            part_message = part.get("message") if isinstance(part.get("message"), dict) else {}
            if part_message:
                message = part_message
                break
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []

    finish_reason = str(source.get("done_reason") or "").strip()
    if not finish_reason:
        for part in sources:
            candidate = str(part.get("done_reason") or "").strip()
            if candidate:
                finish_reason = candidate
                break
    content = message.get("content")
    thinking = message.get("thinking")
    empty_response = bool(
        finish_reason
        and not tool_calls
        and not str(content or "").strip()
        and not str(thinking or "").strip()
    )

    prefill_ms = int(timings.get("prompt_ms") or 0)
    generation_ms = int(timings.get("predicted_ms") or 0)
    timing_source = "provider" if timings else "total_only"

    reported_cost = usage.get("cost")
    allowance = opencode.get("allowance")
    list_cost = opencode.get("list_cost")

    return {
        "prompt_tokens": prompt_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "cache_write_tokens": cache_write_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "answer_tokens": max(0, completion_tokens - reasoning_tokens),
        "tool_calls": len(tool_calls),
        "tool_argument_tokens_estimated": estimate_tool_argument_tokens(tool_calls),
        "reported_cost_micros": micros(reported_cost) if reported_cost is not None else None,
        "allowance_micros": micros(allowance) if allowance is not None else None,
        "list_cost_micros": micros(list_cost) if list_cost is not None else None,
        "billing_source": str(opencode.get("billing_source") or "") or "",
        "cost_estimated": bool(opencode.get("estimated")),
        "finish_reason": finish_reason,
        "empty_response": empty_response,
        "prefill_ms": prefill_ms,
        "generation_ms": generation_ms,
        "timing_source": timing_source,
    }
