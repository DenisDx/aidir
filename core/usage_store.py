"""
Durable usage store: per-model counters, cost, speed profiles, and limit state.

The store keeps its own SQLite database in WAL mode, beside the audit
journals. It stores aggregates only: every completed LLM call updates the
current hour row of the model, of the smart alias it was routed through, of
the envid, and of the speed profile, and the row is written immediately.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from core.billing import (
    CONTEXT_BUCKETS,
    FAILURE_CLASSES,
    LimitRule,
    LimitUsage,
    context_bucket,
    micros,
    rules_for_call,
    to_decimal,
    window_bounds,
)

# Aggregate tables, one per granularity.
_AGG_TABLES: tuple[str, ...] = ("usage_hourly", "usage_daily", "usage_monthly")

# Counter columns of an aggregate row.
_AGG_COUNTERS: tuple[str, ...] = (
    "calls",
    "external_calls",
    "internal_calls",
    "client_requests",
    "successes",
    "failures",
) + FAILURE_CLASSES + (
    "duration_ms_sum",
    "queue_ms_sum",
    "ttft_ms_sum",
    "prefill_ms_sum",
    "generation_ms_sum",
    "prompt_tokens",
    "cached_prompt_tokens",
    "cache_write_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "answer_tokens",
    "tool_calls",
    "tool_argument_tokens_estimated",
    "input_files",
    "input_file_bytes",
    "reported_cost",
    "computed_cost",
    "allowance",
    "list_cost",
    "cache_savings",
)

# Columns of the speed profile row.
_SPEED_COUNTERS: tuple[str, ...] = (
    "calls",
    "prompt_tokens",
    "generation_tokens",
    "reasoning_tokens",
    "prefill_ms",
    "generation_ms",
    "ttft_ms",
    "total_ms",
)

# Columns of the envid row.
_ENVID_COUNTERS: tuple[str, ...] = (
    "calls",
    "external_calls",
    "internal_calls",
    "prompt_tokens",
    "cached_prompt_tokens",
    "completion_tokens",
    "reported_cost",
    "computed_cost",
    "allowance",
)


def _utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def _hour_bucket(moment: datetime) -> str:
    """Return the ISO start of the hour bucket of a moment."""
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


def _day_bucket(moment: datetime) -> str:
    """Return the ISO start of the day bucket of a moment."""
    return moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _month_bucket(moment: datetime) -> str:
    """Return the ISO start of the month bucket of a moment."""
    local = moment.astimezone(timezone.utc)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


class UsageStore:
    """Aggregate model usage in a local SQLite database.

    The store is safe to use from the worker processes: every write is one
    short transaction guarded by a lock, and a write failure never reaches the
    client request.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timezone_name: str = "UTC",
        currency: str = "USD",
        hourly_days: int = 2,
        daily_months: int = 24,
    ) -> None:
        """Prepare the store without opening the database."""
        self.path = Path(path)
        self.timezone_name = str(timezone_name or "UTC")
        self.currency = str(currency or "USD")
        self.hourly_days = max(1, int(hourly_days or 2))
        self.daily_months = max(1, int(daily_months or 24))
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._rules: list[LimitRule] = []
        self._failed_writes = 0
        self._last_write_error = ""
        self._tz = self._resolve_timezone(self.timezone_name)

    @staticmethod
    def _resolve_timezone(name: str) -> Any:
        """Return the timezone object of a name, falling back to UTC."""
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(str(name or "UTC"))
        except Exception:
            return timezone.utc

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def open(self) -> None:
        """Open the database and create or migrate the schema."""
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.path.parent.chmod(0o700)
            except OSError:
                pass
            connection = sqlite3.connect(
                str(self.path),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            self._connection = connection
            self._migrate()

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    @property
    def enabled(self) -> bool:
        """Return whether the store is open."""
        return self._connection is not None

    # ── Schema ──────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        """Create the tables and the indexes the store needs."""
        connection = self._connection
        if connection is None:
            return
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage_call_ids (
                usage_event_id TEXT PRIMARY KEY,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS usage_envid (
                granularity TEXT NOT NULL,
                bucket_start TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                envid TEXT NOT NULL,
                {envid_columns},
                PRIMARY KEY (granularity, bucket_start, provider_id, model_id, envid)
            );
            CREATE TABLE IF NOT EXISTS usage_speed (
                granularity TEXT NOT NULL,
                bucket_start TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                context_bucket TEXT NOT NULL,
                {speed_columns},
                PRIMARY KEY (granularity, bucket_start, provider_id, model_id, context_bucket)
            );
            CREATE TABLE IF NOT EXISTS usage_limit_counters (
                rule_id TEXT NOT NULL,
                window_start TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                prefill_tokens INTEGER NOT NULL DEFAULT 0,
                cached_prefill_tokens INTEGER NOT NULL DEFAULT 0,
                generation_tokens INTEGER NOT NULL DEFAULT 0,
                money_usd INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (rule_id, window_start, scope_id)
            );
            CREATE TABLE IF NOT EXISTS usage_limit_state (
                rule_id TEXT PRIMARY KEY,
                blocked INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            );
            """.format(
                envid_columns=", ".join(f"{name} INTEGER NOT NULL DEFAULT 0" for name in _ENVID_COUNTERS),
                speed_columns=", ".join(f"{name} INTEGER NOT NULL DEFAULT 0" for name in _SPEED_COUNTERS),
            )
        )
        for table in _AGG_TABLES:
            columns = ", ".join(f"{name} INTEGER NOT NULL DEFAULT 0" for name in _AGG_COUNTERS)
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    granularity TEXT NOT NULL,
                    bucket_start TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_alias TEXT NOT NULL,
                    model_kind TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    request_kind TEXT NOT NULL,
                    status_group TEXT NOT NULL,
                    {columns},
                    first_seen_at TEXT,
                    last_seen_at TEXT,
                    PRIMARY KEY (
                        granularity, bucket_start, provider_id, model_id, model_alias,
                        model_kind, protocol, request_kind, status_group
                    )
                )
                """
            )
            connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {table}_bucket_idx
                ON {table} (bucket_start, provider_id, model_id)
                """
            )

    # ── Configuration ───────────────────────────────────────────────────────

    def set_rules(self, rules: Iterable[LimitRule]) -> None:
        """Replace the limit rules the store evaluates."""
        with self._lock:
            self._rules = list(rules or [])

    @property
    def rules(self) -> list[LimitRule]:
        """Return the configured limit rules."""
        return list(self._rules)

    # ── Writing ─────────────────────────────────────────────────────────────

    def record_call(self, event: dict[str, Any]) -> bool:
        """Record one finished LLM call into the aggregates.

        The event carries the counters, the timing, the cost, the envid, and
        the scope of the call. The write is one transaction; a failure is
        counted and never reaches the caller.
        """
        connection = self._connection
        if connection is None:
            return False

        event_id = str(event.get("usage_event_id") or "").strip()
        if not event_id:
            return False

        try:
            with self._lock:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO usage_call_ids (usage_event_id, created_at) VALUES (?, ?)",
                        (event_id, _utc_now().isoformat()),
                    )
                    if cursor.rowcount == 0:
                        connection.execute("ROLLBACK")
                        return False
                    self._write_call_rows(connection, event)
                    self._write_limit_counters(connection, event)
                    connection.execute("COMMIT")
                    self.refresh_limit_state()
                    return True
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
        except Exception as exc:
            self._failed_writes += 1
            self._last_write_error = f"{exc.__class__.__name__}: {exc}"
            return False

    def record_task_outcome(self, event: dict[str, Any]) -> bool:
        """Record one client request that produced no LLM call.

        The outcome is stored in its own row group, so it never mixes with the
        model rows and never carries tokens.
        """
        connection = self._connection
        if connection is None:
            return False
        moment = _utc_now()
        bucket = _hour_bucket(moment)
        status_group = "failure"
        status = str(event.get("status") or "failed").strip().lower()
        if status == "canceled":
            status_group = "canceled"
        counters = {name: 0 for name in _AGG_COUNTERS}
        counters["calls"] = 1
        counters["client_requests"] = 1
        if status_group == "failure":
            counters["failures"] = 1
            counters[str(event.get("failure_class") or "other_error")] = 1
        elif status_group == "canceled":
            counters["canceled"] = 1
        try:
            with self._lock:
                self._upsert_aggregate(
                    connection,
                    "usage_hourly",
                    bucket,
                    provider_id="",
                    model_id="",
                    model_alias=str(event.get("model_alias") or ""),
                    model_kind="client",
                    protocol=str(event.get("protocol") or ""),
                    request_kind=str(event.get("request_kind") or "chat"),
                    status_group=status_group,
                    counters=counters,
                    first_seen_at=moment.isoformat(),
                    last_seen_at=moment.isoformat(),
                )
            return True
        except Exception as exc:
            self._failed_writes += 1
            self._last_write_error = f"{exc.__class__.__name__}: {exc}"
            return False

    def _write_call_rows(self, connection: sqlite3.Connection, event: dict[str, Any]) -> None:
        """Write the aggregate, envid, and speed rows of one call."""
        moment = _utc_now()
        started = str(event.get("started_at") or "") or moment.isoformat()
        finished = str(event.get("finished_at") or "") or moment.isoformat()
        bucket = _hour_bucket(moment)

        status_group = str(event.get("status_group") or "success")
        counters = {name: 0 for name in _AGG_COUNTERS}
        counters["calls"] = 1
        scope = str(event.get("scope") or "external")
        counters["external_calls" if scope == "external" else "internal_calls"] = 1
        if event.get("client_request"):
            counters["client_requests"] = 1
        if status_group == "success":
            counters["successes"] = 1
        elif status_group == "failure":
            counters["failures"] = 1
            counters[str(event.get("failure_class") or "other_error")] = 1
        else:
            counters["canceled"] = 1

        counters["duration_ms_sum"] = int(event.get("total_ms") or 0)
        counters["queue_ms_sum"] = int(event.get("queue_ms") or 0)
        counters["ttft_ms_sum"] = int(event.get("ttft_ms") or 0)
        counters["prefill_ms_sum"] = int(event.get("prefill_ms") or 0)
        counters["generation_ms_sum"] = int(event.get("generation_ms") or 0)
        counters["prompt_tokens"] = int(event.get("prompt_tokens") or 0)
        counters["cached_prompt_tokens"] = int(event.get("cached_prompt_tokens") or 0)
        counters["cache_write_tokens"] = int(event.get("cache_write_tokens") or 0)
        counters["completion_tokens"] = int(event.get("completion_tokens") or 0)
        counters["reasoning_tokens"] = int(event.get("reasoning_tokens") or 0)
        counters["answer_tokens"] = int(event.get("answer_tokens") or 0)
        counters["tool_calls"] = int(event.get("tool_calls") or 0)
        counters["tool_argument_tokens_estimated"] = int(event.get("tool_argument_tokens_estimated") or 0)
        counters["input_files"] = int(event.get("input_files") or 0)
        counters["input_file_bytes"] = int(event.get("input_file_bytes") or 0)
        counters["reported_cost"] = int(event.get("reported_cost_micros") or 0)
        counters["computed_cost"] = int(event.get("computed_cost_micros") or 0)
        counters["allowance"] = int(event.get("allowance_micros") or 0)
        counters["list_cost"] = int(event.get("list_cost_micros") or 0)
        counters["cache_savings"] = int(event.get("cache_savings_micros") or 0)

        provider_id = str(event.get("provider_id") or "")
        model_id = str(event.get("model_id") or "")
        model_alias = str(event.get("model_alias") or "")
        protocol = str(event.get("protocol") or "")
        request_kind = str(event.get("request_kind") or "chat")

        self._upsert_aggregate(
            connection,
            "usage_hourly",
            bucket,
            provider_id=provider_id,
            model_id=model_id,
            model_alias=model_alias,
            model_kind="concrete",
            protocol=protocol,
            request_kind=request_kind,
            status_group=status_group,
            counters=counters,
            first_seen_at=started,
            last_seen_at=finished,
        )

        alias_provider = str(event.get("alias_provider_id") or "")
        alias_model = str(event.get("alias_model_id") or "")
        if alias_provider and alias_model:
            self._upsert_aggregate(
                connection,
                "usage_hourly",
                bucket,
                provider_id=alias_provider,
                model_id=alias_model,
                model_alias=alias_model,
                model_kind="smart",
                protocol=protocol,
                request_kind=request_kind,
                status_group=status_group,
                counters=counters,
                first_seen_at=started,
                last_seen_at=finished,
            )

        envid = str(event.get("envid") or "")
        envid_counters = {name: 0 for name in _ENVID_COUNTERS}
        envid_counters["calls"] = 1
        envid_counters["external_calls" if scope == "external" else "internal_calls"] = 1
        envid_counters["prompt_tokens"] = counters["prompt_tokens"]
        envid_counters["cached_prompt_tokens"] = counters["cached_prompt_tokens"]
        envid_counters["completion_tokens"] = counters["completion_tokens"]
        envid_counters["reported_cost"] = counters["reported_cost"]
        envid_counters["computed_cost"] = counters["computed_cost"]
        envid_counters["allowance"] = counters["allowance"]
        self._upsert_envid(connection, bucket, provider_id, model_id, envid, envid_counters)

        bucket_name = context_bucket(counters["prompt_tokens"])
        speed_counters = {name: 0 for name in _SPEED_COUNTERS}
        speed_counters["calls"] = 1
        speed_counters["prompt_tokens"] = counters["prompt_tokens"]
        speed_counters["generation_tokens"] = counters["completion_tokens"]
        speed_counters["reasoning_tokens"] = counters["reasoning_tokens"]
        speed_counters["prefill_ms"] = counters["prefill_ms_sum"]
        speed_counters["generation_ms"] = counters["generation_ms_sum"]
        speed_counters["ttft_ms"] = counters["ttft_ms_sum"]
        speed_counters["total_ms"] = counters["duration_ms_sum"]
        self._upsert_speed(connection, bucket, provider_id, model_id, bucket_name, speed_counters)
        if alias_provider and alias_model:
            self._upsert_speed(
                connection,
                bucket,
                alias_provider,
                alias_model,
                bucket_name,
                speed_counters,
            )

    def _write_limit_counters(self, connection: sqlite3.Connection, event: dict[str, Any]) -> None:
        """Update the counters of every limit rule that covers the call."""
        rules = rules_for_call(
            self._rules,
            provider_id=str(event.get("provider_id") or ""),
            model_key=f"{event.get('provider_id') or ''}:{event.get('model_id') or ''}",
            alias_key=(
                f"{event.get('alias_provider_id') or ''}:{event.get('alias_model_id') or ''}"
                if event.get("alias_provider_id")
                else ""
            ),
        )
        if not rules:
            return
        now = _utc_now()
        bucket = _hour_bucket(now)
        prefill = int(event.get("prompt_tokens") or 0)
        cached = int(event.get("cached_prompt_tokens") or 0)
        generation = int(event.get("completion_tokens") or 0)
        money = int(event.get("effective_money_micros") or 0)
        for rule in rules:
            connection.execute(
                """
                INSERT INTO usage_limit_counters
                    (rule_id, window_start, scope_id, prefill_tokens, cached_prefill_tokens,
                     generation_tokens, money_usd, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id, window_start, scope_id) DO UPDATE SET
                    prefill_tokens = prefill_tokens + excluded.prefill_tokens,
                    cached_prefill_tokens = cached_prefill_tokens + excluded.cached_prefill_tokens,
                    generation_tokens = generation_tokens + excluded.generation_tokens,
                    money_usd = money_usd + excluded.money_usd,
                    updated_at = excluded.updated_at
                """,
                (rule.id, bucket, rule.scope_id, prefill, cached, generation, money, now.isoformat()),
            )

    def _upsert_aggregate(
        self,
        connection: sqlite3.Connection,
        table: str,
        bucket_start: str,
        *,
        provider_id: str,
        model_id: str,
        model_alias: str,
        model_kind: str,
        protocol: str,
        request_kind: str,
        status_group: str,
        counters: dict[str, int],
        first_seen_at: str,
        last_seen_at: str,
    ) -> None:
        """Add the counters of one call to an aggregate row, creating it when needed."""
        names = list(_AGG_COUNTERS)
        values = [counters.get(name, 0) for name in names]
        updates = ", ".join(f"{name} = {name} + excluded.{name}" for name in names)
        columns = ", ".join(["granularity", "bucket_start", "provider_id", "model_id",
                             "model_alias", "model_kind", "protocol", "request_kind",
                             "status_group", *names, "first_seen_at", "last_seen_at"])
        placeholders = ", ".join("?" for _ in range(9 + len(names) + 2))
        connection.execute(
            f"""
            INSERT INTO {table} ({columns})
            VALUES ({placeholders})
            ON CONFLICT(granularity, bucket_start, provider_id, model_id, model_alias,
                        model_kind, protocol, request_kind, status_group) DO UPDATE SET
                {updates},
                first_seen_at = MIN(first_seen_at, excluded.first_seen_at),
                last_seen_at = MAX(last_seen_at, excluded.last_seen_at)
            """,
            [
                "hourly",
                bucket_start,
                provider_id,
                model_id,
                model_alias,
                model_kind,
                protocol,
                request_kind,
                status_group,
                *values,
                first_seen_at,
                last_seen_at,
            ],
        )

    def _upsert_envid(
        self,
        connection: sqlite3.Connection,
        bucket_start: str,
        provider_id: str,
        model_id: str,
        envid: str,
        counters: dict[str, int],
    ) -> None:
        """Add the counters of one call to its envid row."""
        names = list(_ENVID_COUNTERS)
        values = [counters.get(name, 0) for name in names]
        updates = ", ".join(f"{name} = {name} + excluded.{name}" for name in names)
        columns = ", ".join(
            ["granularity", "bucket_start", "provider_id", "model_id", "envid", *names]
        )
        placeholders = ", ".join("?" for _ in range(5 + len(names)))
        connection.execute(
            f"""
            INSERT INTO usage_envid ({columns})
            VALUES ({placeholders})
            ON CONFLICT(granularity, bucket_start, provider_id, model_id, envid) DO UPDATE SET
                {updates}
            """,
            ["hourly", bucket_start, provider_id, model_id, envid, *values],
        )

    def _upsert_speed(
        self,
        connection: sqlite3.Connection,
        bucket_start: str,
        provider_id: str,
        model_id: str,
        context_bucket_name: str,
        counters: dict[str, int],
    ) -> None:
        """Add the timing counters of one call to its speed row."""
        names = list(_SPEED_COUNTERS)
        values = [counters.get(name, 0) for name in names]
        updates = ", ".join(f"{name} = {name} + excluded.{name}" for name in names)
        columns = ", ".join(
            ["granularity", "bucket_start", "provider_id", "model_id", "context_bucket", *names]
        )
        placeholders = ", ".join("?" for _ in range(5 + len(names)))
        connection.execute(
            f"""
            INSERT INTO usage_speed ({columns})
            VALUES ({placeholders})
            ON CONFLICT(granularity, bucket_start, provider_id, model_id, context_bucket) DO UPDATE SET
                {updates}
            """,
            ["hourly", bucket_start, provider_id, model_id, context_bucket_name, *values],
        )

    # ── Limit state ─────────────────────────────────────────────────────────

    def limit_usage(self, rule: LimitRule, now: datetime | None = None) -> LimitUsage:
        """Return the accumulated usage of one limit rule inside its window."""
        connection = self._connection
        moment = now or _utc_now()
        start, end = window_bounds(rule.period, rule.mode, moment, tz=self._tz)
        usage = LimitUsage(rule_id=rule.id, scope_id=rule.scope_id)
        if connection is None:
            return usage
        try:
            with self._lock:
                row = connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(prefill_tokens), 0) AS prefill_tokens,
                        COALESCE(SUM(cached_prefill_tokens), 0) AS cached_prefill_tokens,
                        COALESCE(SUM(generation_tokens), 0) AS generation_tokens,
                        COALESCE(SUM(money_usd), 0) AS money_usd
                    FROM usage_limit_counters
                    WHERE rule_id = ? AND window_start >= ? AND window_start < ?
                    """,
                    (rule.id, start.isoformat(), end.isoformat()),
                ).fetchone()
                state = connection.execute(
                    "SELECT blocked FROM usage_limit_state WHERE rule_id = ?",
                    (rule.id,),
                ).fetchone()
        except sqlite3.Error:
            return usage
        if row is not None:
            usage = LimitUsage(
                rule_id=rule.id,
                scope_id=rule.scope_id,
                prefill_tokens=int(row["prefill_tokens"] or 0),
                cached_prefill_tokens=int(row["cached_prefill_tokens"] or 0),
                generation_tokens=int(row["generation_tokens"] or 0),
                money_micros=int(row["money_usd"] or 0),
                blocked=bool(state["blocked"]) if state is not None else False,
            )
        return usage

    def refresh_limit_state(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Mark every exceeded limit rule as blocked and return the limit states."""
        connection = self._connection
        states: list[dict[str, Any]] = []
        if connection is None:
            return states
        moment = now or _utc_now()
        for rule in self._rules:
            usage = self.limit_usage(rule, moment)
            dimension = usage.exceeded_by(rule)
            blocked = usage.blocked or bool(dimension)
            try:
                with self._lock:
                    connection.execute(
                        """
                        INSERT INTO usage_limit_state (rule_id, blocked, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(rule_id) DO UPDATE SET
                            blocked = MAX(blocked, excluded.blocked),
                            updated_at = excluded.updated_at
                        """,
                        (rule.id, 1 if blocked else 0, moment.isoformat()),
                    )
            except sqlite3.Error:
                pass
            states.append(
                {
                    "id": rule.id,
                    "scope_type": rule.scope_type,
                    "scope_id": rule.scope_id,
                    "period": rule.period,
                    "mode": rule.mode,
                    "action": rule.action,
                    "blocked": blocked,
                    "exceeded_by": dimension,
                    "usage": {
                        "prefill_tokens": usage.prefill_tokens,
                        "cached_prefill_tokens": usage.cached_prefill_tokens,
                        "generation_tokens": usage.generation_tokens,
                        "money": usage.money_micros,
                    },
                    "limits": {
                        "max_prefill_tokens": rule.max_prefill_tokens,
                        "max_cached_prefill_tokens": rule.max_cached_prefill_tokens,
                        "max_generation_tokens": rule.max_generation_tokens,
                        "max_money": (
                            int(rule.max_money * 1_000_000) if rule.max_money is not None else None
                        ),
                    },
                }
            )
        return states

    def blocked_rules(self) -> list[LimitRule]:
        """Return the limit rules that are currently blocked."""
        connection = self._connection
        if connection is None:
            return []
        try:
            with self._lock:
                rows = connection.execute(
                    "SELECT rule_id FROM usage_limit_state WHERE blocked = 1"
                ).fetchall()
        except sqlite3.Error:
            return []
        blocked_ids = {str(row["rule_id"]) for row in rows}
        return [rule for rule in self._rules if rule.id in blocked_ids]

    def is_blocked(self, *, provider_id: str, model_id: str, alias_provider: str = "", alias_model: str = "") -> LimitRule | None:
        """Return the limit rule that blocks one model, if any."""
        blocked_ids = {rule.id for rule in self.blocked_rules()}
        if not blocked_ids:
            return None
        applicable = rules_for_call(
            self._rules,
            provider_id=provider_id,
            model_key=f"{provider_id}:{model_id}",
            alias_key=f"{alias_provider}:{alias_model}" if alias_provider else "",
        )
        for rule in applicable:
            if rule.id in blocked_ids and rule.blocks:
                return rule
        return None

    def is_candidate_blocked(self, provider_id: str, model_id: str) -> LimitRule | None:
        """Return the limit rule that makes one smart candidate ineligible, if any."""
        blocked_ids = {rule.id for rule in self.blocked_rules()}
        if not blocked_ids:
            return None
        applicable = rules_for_call(
            self._rules,
            provider_id=provider_id,
            model_key=f"{provider_id}:{model_id}",
        )
        for rule in applicable:
            if rule.id in blocked_ids and rule.excludes_candidate:
                return rule
        return None

    def reset_limit(self, rule_id: str, counter: str = "all") -> bool:
        """Clear the counters of one limit rule and its blocked state.

        The counter is "tokens", "money", or "all". Only the current window is
        cleared, and the stored aggregates are not changed.
        """
        connection = self._connection
        if connection is None:
            return False
        rule = next((item for item in self._rules if item.id == rule_id), None)
        if rule is None:
            return False
        selected = str(counter or "all").strip().lower()
        if selected not in {"tokens", "money", "all"}:
            return False
        now = _utc_now()
        start, end = window_bounds(rule.period, rule.mode, now, tz=self._tz)
        assignments = []
        if selected in {"tokens", "all"}:
            assignments.extend(["prefill_tokens = 0", "cached_prefill_tokens = 0", "generation_tokens = 0"])
        if selected in {"money", "all"}:
            assignments.append("money_usd = 0")
        try:
            with self._lock:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if assignments:
                        connection.execute(
                            f"""
                            UPDATE usage_limit_counters
                            SET {", ".join(assignments)}, updated_at = ?
                            WHERE rule_id = ? AND window_start >= ? AND window_start < ?
                            """,
                            (now.isoformat(), rule_id, start.isoformat(), end.isoformat()),
                        )
                    connection.execute(
                        "UPDATE usage_limit_state SET blocked = 0, updated_at = ? WHERE rule_id = ?",
                        (now.isoformat(), rule_id),
                    )
                    connection.execute("COMMIT")
                    return True
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.Error:
            return False

    # ── Reading ─────────────────────────────────────────────────────────────

    def _table_for_range(self, start: datetime, end: datetime) -> str:
        """Return the aggregate table that answers a time range."""
        span = (end - start).total_seconds()
        if span <= 3 * 24 * 3600:
            return "usage_hourly"
        if span <= 120 * 24 * 3600:
            return "usage_daily"
        return "usage_monthly"

    def _query_rows(
        self,
        table: str,
        start: datetime,
        end: datetime,
        *,
        provider_id: str = "",
        model_id: str = "",
        model_kind: str = "",
    ) -> list[sqlite3.Row]:
        """Return the aggregate rows of a time range."""
        connection = self._connection
        if connection is None:
            return []
        clauses = ["bucket_start >= ?", "bucket_start < ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if provider_id:
            clauses.append("provider_id = ?")
            params.append(provider_id)
        if model_id:
            clauses.append("model_id = ?")
            params.append(model_id)
        if model_kind:
            clauses.append("model_kind = ?")
            params.append(model_kind)
        try:
            with self._lock:
                return list(
                    connection.execute(
                        f"SELECT * FROM {table} WHERE {' AND '.join(clauses)}",
                        params,
                    ).fetchall()
                )
        except sqlite3.Error:
            return []

    def summary(self, start: datetime, end: datetime, *, scope: str = "all") -> dict[str, Any]:
        """Return the totals of a time range."""
        table = self._table_for_range(start, end)
        rows = self._query_rows(table, start, end, model_kind="concrete")
        totals = {name: 0 for name in _AGG_COUNTERS}
        for row in rows:
            for name in _AGG_COUNTERS:
                totals[name] += int(row[name] or 0)
        calls = totals["calls"] if scope == "all" else (
            totals["external_calls"] if scope == "external" else totals["internal_calls"]
        )
        totals["calls"] = calls
        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "granularity": table.removeprefix("usage_"),
            "scope": scope,
            "calls": calls,
            "totals": totals,
            "currency": self.currency,
        }

    def model_rows(self, start: datetime, end: datetime, *, scope: str = "all") -> list[dict[str, Any]]:
        """Return one aggregated row per model of a time range."""
        table = self._table_for_range(start, end)
        rows = self._query_rows(table, start, end)
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            kind = str(row["model_kind"] or "concrete")
            if kind == "client":
                continue
            key = (str(row["provider_id"] or ""), str(row["model_id"] or ""), kind)
            entry = merged.setdefault(
                key,
                {
                    "provider": key[0],
                    "model": key[1],
                    "model_kind": kind,
                    "alias": str(row["model_alias"] or ""),
                    "calls": 0,
                    "external_calls": 0,
                    "internal_calls": 0,
                    "successes": 0,
                    "failures": 0,
                    "canceled": 0,
                    "duration_ms_sum": 0,
                    "ttft_ms_sum": 0,
                    "prefill_ms_sum": 0,
                    "generation_ms_sum": 0,
                    "prompt_tokens": 0,
                    "cached_prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reasoning_tokens": 0,
                    "answer_tokens": 0,
                    "tool_calls": 0,
                    "reported_cost": 0,
                    "computed_cost": 0,
                    "allowance": 0,
                    "list_cost": 0,
                    "cache_savings": 0,
                    "failures_by_class": {name: 0 for name in FAILURE_CLASSES},
                },
            )
            for name in (
                "calls", "external_calls", "internal_calls", "successes", "failures", "canceled",
                "duration_ms_sum", "ttft_ms_sum", "prefill_ms_sum", "generation_ms_sum",
                "prompt_tokens", "cached_prompt_tokens", "completion_tokens", "reasoning_tokens",
                "answer_tokens", "tool_calls", "reported_cost", "computed_cost", "allowance",
                "list_cost", "cache_savings",
            ):
                entry[name] += int(row[name] or 0)
            for name in FAILURE_CLASSES:
                entry["failures_by_class"][name] += int(row[name] or 0)

        result = []
        for entry in merged.values():
            calls = entry["calls"] if scope == "all" else (
                entry["external_calls"] if scope == "external" else entry["internal_calls"]
            )
            entry["calls_selected"] = calls
            entry["prompt_billable_tokens"] = max(
                0,
                int(entry["prompt_tokens"] or 0) - int(entry["cached_prompt_tokens"] or 0),
            )
            entry["cache_hit_rate"] = (
                round(entry["cached_prompt_tokens"] / entry["prompt_tokens"], 6)
                if entry["prompt_tokens"]
                else 0.0
            )
            entry["ttft_ms_avg"] = int(entry["ttft_ms_sum"] / entry["calls"]) if entry["calls"] else 0
            entry["prefill_tokens_per_second"] = (
                round(entry["prompt_tokens"] / (entry["prefill_ms_sum"] / 1000), 3)
                if entry["prefill_ms_sum"]
                else None
            )
            entry["generation_tokens_per_second"] = (
                round(entry["completion_tokens"] / (entry["generation_ms_sum"] / 1000), 3)
                if entry["generation_ms_sum"]
                else None
            )
            result.append(entry)
        result.sort(key=lambda item: (-item["calls"], item["provider"], item["model"]))
        return result

    def series(self, start: datetime, end: datetime, *, interval: str = "hour") -> list[dict[str, Any]]:
        """Return the usage of every non-empty bucket of a time range."""
        table = "usage_hourly" if interval == "hour" else (
            "usage_daily" if interval == "day" else "usage_monthly"
        )
        rows = self._query_rows(table, start, end)
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket = str(row["bucket_start"] or "")
            entry = buckets.setdefault(
                bucket,
                {
                    "bucket_start": bucket,
                    "calls": 0,
                    "prompt_tokens": 0,
                    "cached_prompt_tokens": 0,
                    "completion_tokens": 0,
                    "reported_cost": 0,
                    "computed_cost": 0,
                    "allowance": 0,
                    "failures": 0,
                },
            )
            for name in (
                "calls", "prompt_tokens", "cached_prompt_tokens", "completion_tokens",
                "reported_cost", "computed_cost", "allowance", "failures",
            ):
                entry[name] += int(row[name] or 0)
        return [buckets[key] for key in sorted(buckets)]

    def speeds(self, start: datetime, end: datetime, *, provider_id: str = "", model_id: str = "") -> list[dict[str, Any]]:
        """Return the speed profiles of a time range, per model and context bucket."""
        connection = self._connection
        if connection is None:
            return []
        clauses = ["bucket_start >= ?", "bucket_start < ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if provider_id:
            clauses.append("provider_id = ?")
            params.append(provider_id)
        if model_id:
            clauses.append("model_id = ?")
            params.append(model_id)
        try:
            with self._lock:
                rows = connection.execute(
                    f"SELECT * FROM usage_speed WHERE {' AND '.join(clauses)}",
                    params,
                ).fetchall()
        except sqlite3.Error:
            return []
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (
                str(row["provider_id"] or ""),
                str(row["model_id"] or ""),
                str(row["context_bucket"] or ""),
            )
            entry = merged.setdefault(
                key,
                {
                    "provider": key[0],
                    "model": key[1],
                    "context_bucket": key[2],
                    "calls": 0,
                    "prompt_tokens": 0,
                    "generation_tokens": 0,
                    "reasoning_tokens": 0,
                    "prefill_ms": 0,
                    "generation_ms": 0,
                    "ttft_ms": 0,
                    "total_ms": 0,
                },
            )
            for name in _SPEED_COUNTERS:
                entry[name] += int(row[name] or 0)
        result = []
        for entry in merged.values():
            entry["prefill_tokens_per_second"] = (
                round(entry["prompt_tokens"] / (entry["prefill_ms"] / 1000), 3)
                if entry["prefill_ms"]
                else None
            )
            entry["generation_tokens_per_second"] = (
                round(entry["generation_tokens"] / (entry["generation_ms"] / 1000), 3)
                if entry["generation_ms"]
                else None
            )
            entry["ttft_ms_avg"] = int(entry["ttft_ms"] / entry["calls"]) if entry["calls"] else 0
            entry["reasoning_share"] = (
                round(entry["reasoning_tokens"] / entry["generation_tokens"], 6)
                if entry["generation_tokens"]
                else 0.0
            )
            result.append(entry)
        result.sort(key=lambda item: (item["provider"], item["model"], item["context_bucket"]))
        return result

    def allowance_rows(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Return the provider-reported allowance of a time range."""
        table = self._table_for_range(start, end)
        rows = self._query_rows(table, start, end, model_kind="concrete")
        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            provider = str(row["provider_id"] or "")
            entry = merged.setdefault(
                provider,
                {
                    "provider": provider,
                    "allowance": 0,
                    "reported_cost": 0,
                    "computed_cost": 0,
                    "list_cost": 0,
                    "calls": 0,
                },
            )
            for name in ("allowance", "reported_cost", "computed_cost", "list_cost", "calls"):
                entry[name] += int(row[name] or 0)
        return [merged[key] for key in sorted(merged)]

    def limits_state(self) -> list[dict[str, Any]]:
        """Return the state of every configured limit rule."""
        return self.refresh_limit_state()

    def health(self) -> dict[str, Any]:
        """Return the metering health of the store."""
        connection = self._connection
        healthy = connection is not None
        return {
            "healthy": healthy,
            "database": str(self.path),
            "failed_writes": self._failed_writes,
            "last_error": self._last_write_error,
            "rules": len(self._rules),
            "time": _utc_now().isoformat(),
        }

    # ── Rollup and retention ────────────────────────────────────────────────

    def rollup(self, now: datetime | None = None) -> dict[str, int]:
        """Close the finished days and months and apply the retention rules.

        The rollup is idempotent: a day is recomputed from its hourly rows and
        a month from its daily rows, so a repeated run cannot double count.
        """
        connection = self._connection
        if connection is None:
            return {"days": 0, "months": 0, "deleted_hourly": 0, "deleted_daily": 0}
        moment = now or _utc_now()
        local = moment.astimezone(self._tz)
        today = local.replace(hour=0, minute=0, second=0, microsecond=0)
        closed_days = 0
        closed_months = 0

        try:
            with self._lock:
                rows = connection.execute(
                    """
                    SELECT DISTINCT substr(bucket_start, 1, 10) AS day
                    FROM usage_hourly
                    WHERE bucket_start < ?
                    ORDER BY day
                    """,
                    (today.isoformat(),),
                ).fetchall()
                for row in rows:
                    day = str(row["day"] or "")
                    if not day:
                        continue
                    self._rollup_day(connection, day)
                    closed_days += 1

                month_rows = connection.execute(
                    """
                    SELECT DISTINCT substr(bucket_start, 1, 7) AS month
                    FROM usage_daily
                    WHERE bucket_start < ?
                    ORDER BY month
                    """,
                    (today.strftime("%Y-%m-01T00:00:00+00:00"),),
                ).fetchall()
                for row in month_rows:
                    month = str(row["month"] or "")
                    if not month:
                        continue
                    self._rollup_month(connection, month)
                    closed_months += 1

                deleted_hourly = self._apply_retention(connection, moment)
        except sqlite3.Error:
            return {"days": closed_days, "months": closed_months, "deleted_hourly": 0, "deleted_daily": 0}
        return {
            "days": closed_days,
            "months": closed_months,
            "deleted_hourly": deleted_hourly.get("hourly", 0),
            "deleted_daily": deleted_hourly.get("daily", 0),
        }

    def _rollup_day(self, connection: sqlite3.Connection, day: str) -> None:
        """Sum the hourly rows of one day into its daily row."""
        start = f"{day}T00:00:00+00:00"
        end = f"{day}T23:59:59.999999+00:00"
        rows = connection.execute(
            "SELECT * FROM usage_hourly WHERE bucket_start >= ? AND bucket_start <= ?",
            (start, end),
        ).fetchall()
        if not rows:
            return
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in rows:
            key = (
                str(row["provider_id"] or ""),
                str(row["model_id"] or ""),
                str(row["model_alias"] or ""),
                str(row["model_kind"] or ""),
                str(row["protocol"] or ""),
                str(row["request_kind"] or ""),
                str(row["status_group"] or ""),
            )
            entry = grouped.setdefault(
                key,
                {"counters": {name: 0 for name in _AGG_COUNTERS}, "first": row["first_seen_at"], "last": row["last_seen_at"]},
            )
            for name in _AGG_COUNTERS:
                entry["counters"][name] += int(row[name] or 0)
            if row["first_seen_at"] and (not entry["first"] or row["first_seen_at"] < entry["first"]):
                entry["first"] = row["first_seen_at"]
            if row["last_seen_at"] and (not entry["last"] or row["last_seen_at"] > entry["last"]):
                entry["last"] = row["last_seen_at"]
        connection.execute("DELETE FROM usage_daily WHERE bucket_start = ?", (start,))
        for key, entry in grouped.items():
            provider_id, model_id, model_alias, model_kind, protocol, request_kind, status_group = key
            self._insert_aggregate(
                connection,
                "usage_daily",
                start,
                provider_id=provider_id,
                model_id=model_id,
                model_alias=model_alias,
                model_kind=model_kind,
                protocol=protocol,
                request_kind=request_kind,
                status_group=status_group,
                counters=entry["counters"],
                first_seen_at=entry["first"] or start,
                last_seen_at=entry["last"] or start,
            )

    def _rollup_month(self, connection: sqlite3.Connection, month: str) -> None:
        """Sum the daily rows of one month into its monthly row."""
        start = f"{month}-01T00:00:00+00:00"
        end = f"{month}-31T23:59:59.999999+00:00"
        rows = connection.execute(
            "SELECT * FROM usage_daily WHERE bucket_start >= ? AND bucket_start <= ?",
            (start, end),
        ).fetchall()
        if not rows:
            return
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in rows:
            key = (
                str(row["provider_id"] or ""),
                str(row["model_id"] or ""),
                str(row["model_alias"] or ""),
                str(row["model_kind"] or ""),
                str(row["protocol"] or ""),
                str(row["request_kind"] or ""),
                str(row["status_group"] or ""),
            )
            entry = grouped.setdefault(
                key,
                {"counters": {name: 0 for name in _AGG_COUNTERS}, "first": row["first_seen_at"], "last": row["last_seen_at"]},
            )
            for name in _AGG_COUNTERS:
                entry["counters"][name] += int(row[name] or 0)
            if row["first_seen_at"] and (not entry["first"] or row["first_seen_at"] < entry["first"]):
                entry["first"] = row["first_seen_at"]
            if row["last_seen_at"] and (not entry["last"] or row["last_seen_at"] > entry["last"]):
                entry["last"] = row["last_seen_at"]
        connection.execute("DELETE FROM usage_monthly WHERE bucket_start = ?", (start,))
        for key, entry in grouped.items():
            provider_id, model_id, model_alias, model_kind, protocol, request_kind, status_group = key
            self._insert_aggregate(
                connection,
                "usage_monthly",
                start,
                provider_id=provider_id,
                model_id=model_id,
                model_alias=model_alias,
                model_kind=model_kind,
                protocol=protocol,
                request_kind=request_kind,
                status_group=status_group,
                counters=entry["counters"],
                first_seen_at=entry["first"] or start,
                last_seen_at=entry["last"] or start,
            )

    def _insert_aggregate(
        self,
        connection: sqlite3.Connection,
        table: str,
        bucket_start: str,
        *,
        provider_id: str,
        model_id: str,
        model_alias: str,
        model_kind: str,
        protocol: str,
        request_kind: str,
        status_group: str,
        counters: dict[str, int],
        first_seen_at: str,
        last_seen_at: str,
    ) -> None:
        """Insert one aggregate row, replacing an existing row with the same key."""
        names = list(_AGG_COUNTERS)
        columns = ", ".join(
            ["granularity", "bucket_start", "provider_id", "model_id", "model_alias",
             "model_kind", "protocol", "request_kind", "status_group", *names,
             "first_seen_at", "last_seen_at"]
        )
        placeholders = ", ".join("?" for _ in range(9 + len(names) + 2))
        connection.execute(
            f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
            [
                table.removeprefix("usage_"),
                bucket_start,
                provider_id,
                model_id,
                model_alias,
                model_kind,
                protocol,
                request_kind,
                status_group,
                *[counters.get(name, 0) for name in names],
                first_seen_at,
                last_seen_at,
            ],
        )

    def _apply_retention(self, connection: sqlite3.Connection, moment: datetime) -> dict[str, int]:
        """Delete the rows that fell out of the retention windows."""
        local = moment.astimezone(self._tz)
        today = local.replace(hour=0, minute=0, second=0, microsecond=0)
        hourly_cutoff = today - timedelta(days=self.hourly_days)
        daily_cutoff = (today - timedelta(days=int(self.daily_months * 30.4))).replace(day=1)
        deleted_hourly = connection.execute(
            "DELETE FROM usage_hourly WHERE bucket_start < ?",
            (hourly_cutoff.isoformat(),),
        ).rowcount
        deleted_daily = connection.execute(
            "DELETE FROM usage_daily WHERE bucket_start < ?",
            (daily_cutoff.isoformat(),),
        ).rowcount
        connection.execute(
            "DELETE FROM usage_call_ids WHERE created_at < ?",
            (hourly_cutoff.isoformat(),),
        )
        connection.execute(
            "DELETE FROM usage_envid WHERE granularity = 'hourly' AND bucket_start < ?",
            (hourly_cutoff.isoformat(),),
        )
        connection.execute(
            "DELETE FROM usage_speed WHERE granularity = 'hourly' AND bucket_start < ?",
            (hourly_cutoff.isoformat(),),
        )
        return {"hourly": deleted_hourly, "daily": deleted_daily}

    # ── Diagnostics ─────────────────────────────────────────────────────────

    def debug_dump(self) -> dict[str, Any]:
        """Return a small snapshot of the stored rows, used by the tests."""
        connection = self._connection
        if connection is None:
            return {}
        try:
            with self._lock:
                hourly = connection.execute("SELECT COUNT(*) AS n FROM usage_hourly").fetchone()
                daily = connection.execute("SELECT COUNT(*) AS n FROM usage_daily").fetchone()
                monthly = connection.execute("SELECT COUNT(*) AS n FROM usage_monthly").fetchone()
                counters = connection.execute(
                    "SELECT COUNT(*) AS n FROM usage_limit_counters"
                ).fetchone()
        except sqlite3.Error:
            return {}
        return {
            "hourly": int(hourly["n"] if hourly else 0),
            "daily": int(daily["n"] if daily else 0),
            "monthly": int(monthly["n"] if monthly else 0),
            "limit_counters": int(counters["n"] if counters else 0),
        }


def load_usage_rules(config_raw: dict | None) -> list[LimitRule]:
    """Load the limit rules of the whole configuration.

    The rules of the root, of every provider, and of every model are collected
    into one list. A duplicated rule id is a configuration error.
    """
    from core.billing import LimitRuleError, parse_limit_rules

    raw = config_raw if isinstance(config_raw, dict) else {}
    billing = raw.get("billing") if isinstance(raw.get("billing"), dict) else {}
    rules: list[LimitRule] = []
    seen: set[str] = set()
    rules.extend(parse_limit_rules(billing.get("limits"), scope_type="instance", seen_ids=seen))

    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
    for provider_id, provider_cfg in providers.items():
        if not isinstance(provider_cfg, dict):
            continue
        provider_billing = provider_cfg.get("billing") if isinstance(provider_cfg.get("billing"), dict) else {}
        rules.extend(
            parse_limit_rules(
                provider_billing.get("limits"),
                scope_type="provider",
                scope_id=str(provider_id),
                seen_ids=seen,
            )
        )
        for model_cfg in provider_cfg.get("models") or []:
            if not isinstance(model_cfg, dict):
                continue
            model_id = str(model_cfg.get("id") or "").strip()
            if not model_id:
                continue
            model_billing = model_cfg.get("billing") if isinstance(model_cfg.get("billing"), dict) else {}
            rules.extend(
                parse_limit_rules(
                    model_billing.get("limits"),
                    scope_type="model",
                    scope_id=f"{provider_id}:{model_id}",
                    seen_ids=seen,
                )
            )
    return rules


def resolve_model_prices(
    config_raw: dict | None,
    *,
    provider_id: str,
    model_id: str,
    prompt_tokens: int = 0,
) -> Any:
    """Return the price set of a concrete model, with the tiers applied."""
    from core.billing import PriceSet, resolve_prices

    raw = config_raw if isinstance(config_raw, dict) else {}
    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
    provider = providers.get(provider_id) if isinstance(providers.get(provider_id), dict) else {}
    for model_cfg in provider.get("models") or []:
        if not isinstance(model_cfg, dict):
            continue
        if str(model_cfg.get("id") or "").strip() != model_id:
            continue
        billing = model_cfg.get("billing") if isinstance(model_cfg.get("billing"), dict) else {}
        prices = billing.get("prices") if isinstance(billing.get("prices"), dict) else {}
        tiers = billing.get("price_tiers") if isinstance(billing.get("price_tiers"), list) else []
        return resolve_prices(prices, tiers, prompt_tokens)
    return PriceSet()
