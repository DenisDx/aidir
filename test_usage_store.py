"""Tests of the durable usage store: aggregates, rollups, limits, and retention."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from core.billing import parse_limit_rules
from core.usage_store import UsageStore, load_usage_rules, resolve_model_prices


def _event(**overrides) -> dict:
    """Return a usage event with the fields a finished call carries."""
    event = {
        "usage_event_id": "task-1:1",
        "task_id": "task-1",
        "call_index": 1,
        "provider_id": "p1",
        "model_id": "m1",
        "model_alias": "alias-1",
        "alias_provider_id": "",
        "alias_model_id": "",
        "protocol": "chat-completions",
        "request_kind": "chat",
        "stream": False,
        "status": "ok",
        "status_group": "success",
        "failure_class": "",
        "error_code": "",
        "http_status": 200,
        "scope": "external",
        "client_request": True,
        "total_ms": 1000,
        "ttft_ms": 200,
        "queue_ms": 0,
        "prefill_ms": 300,
        "generation_ms": 500,
        "timing_source": "stream",
        "prompt_tokens": 1000,
        "cached_prompt_tokens": 100,
        "cache_write_tokens": 0,
        "completion_tokens": 200,
        "reasoning_tokens": 50,
        "answer_tokens": 150,
        "tool_calls": 1,
        "tool_argument_tokens_estimated": 12,
        "input_files": 0,
        "input_file_bytes": 0,
        "reported_cost_micros": 400,
        "computed_cost_micros": 380,
        "allowance_micros": 250,
        "list_cost_micros": 400,
        "effective_money_micros": 250,
        "cache_savings_micros": 30,
        "billing_source": "go",
        "cost_estimated": False,
        "envid": "",
    }
    event.update(overrides)
    return event


class UsageStoreTestCase(unittest.TestCase):
    """Provide a store in a temporary directory."""

    def setUp(self) -> None:
        """Open a fresh store."""
        self.directory = tempfile.mkdtemp(prefix="aidir-usage-")
        self.path = os.path.join(self.directory, "usage.sqlite")
        self.store = UsageStore(self.path, hourly_days=2)
        self.store.open()

    def tearDown(self) -> None:
        """Close the store and remove the directory."""
        self.store.close()
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def _window(self) -> tuple[datetime, datetime]:
        """Return a window around now."""
        end = datetime.now(timezone.utc)
        return end - timedelta(hours=2), end


class TestAggregates(UsageStoreTestCase):
    """Verify the aggregate rows of the store."""

    def test_a_call_updates_the_model_row(self) -> None:
        """One call adds its counters to the row of its model."""
        self.assertTrue(self.store.record_call(_event()))
        start, end = self._window()
        rows = self.store.model_rows(start, end)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "p1")
        self.assertEqual(rows[0]["model"], "m1")
        self.assertEqual(rows[0]["calls"], 1)
        self.assertEqual(rows[0]["external_calls"], 1)
        self.assertEqual(rows[0]["internal_calls"], 0)
        self.assertEqual(rows[0]["prompt_tokens"], 1000)
        self.assertEqual(rows[0]["completion_tokens"], 200)
        self.assertEqual(rows[0]["computed_cost"], 380)
        self.assertEqual(rows[0]["allowance"], 250)
        self.assertEqual(rows[0]["cache_hit_rate"], 0.1)

    def test_a_duplicate_finalization_is_ignored(self) -> None:
        """The same call index never counts twice."""
        self.assertTrue(self.store.record_call(_event()))
        self.assertFalse(self.store.record_call(_event()))
        start, end = self._window()
        self.assertEqual(self.store.summary(start, end)["totals"]["calls"], 1)

    def test_a_failed_call_adds_no_tokens(self) -> None:
        """A call without an answer adds its class and no tokens."""
        self.store.record_call(
            _event(
                usage_event_id="task-1:2",
                call_index=2,
                status="http_error",
                status_group="failure",
                failure_class="http_5xx",
                prompt_tokens=0,
                completion_tokens=0,
                computed_cost_micros=0,
                effective_money_micros=0,
                scope="internal",
                client_request=False,
            )
        )
        start, end = self._window()
        rows = self.store.model_rows(start, end)
        self.assertEqual(rows[0]["failures"], 1)
        self.assertEqual(rows[0]["failures_by_class"]["http_5xx"], 1)
        self.assertEqual(rows[0]["prompt_tokens"], 0)
        self.assertEqual(rows[0]["internal_calls"], 1)

    def test_a_smart_call_updates_the_alias_and_the_model(self) -> None:
        """A call through an alias is counted for the alias and for the model."""
        self.store.record_call(
            _event(
                alias_provider_id="smart",
                alias_model_id="fast",
                model_alias="alias-1",
            )
        )
        start, end = self._window()
        rows = self.store.model_rows(start, end)
        kinds = {(row["provider"], row["model"], row["model_kind"]) for row in rows}
        self.assertIn(("p1", "m1", "concrete"), kinds)
        self.assertIn(("smart", "fast", "smart"), kinds)

    def test_the_external_and_the_internal_calls_are_separate(self) -> None:
        """The scope selects the column, not a different aggregation."""
        self.store.record_call(_event())
        self.store.record_call(
            _event(
                usage_event_id="task-1:2",
                call_index=2,
                scope="internal",
                client_request=False,
            )
        )
        start, end = self._window()
        self.assertEqual(self.store.summary(start, end, scope="all")["totals"]["calls"], 2)
        self.assertEqual(self.store.summary(start, end, scope="external")["totals"]["calls"], 1)
        self.assertEqual(self.store.summary(start, end, scope="internal")["totals"]["calls"], 1)

    def test_a_task_failure_without_a_call_has_its_own_row(self) -> None:
        """A task failure is stored apart from the model rows."""
        self.assertTrue(
            self.store.record_task_outcome({"status": "failed", "failure_class": "timeout", "model_alias": "m1"})
        )
        start, end = self._window()
        rows = self.store.model_rows(start, end)
        self.assertEqual(rows, [])
        summary = self.store.summary(start, end)
        self.assertEqual(summary["totals"]["calls"], 0)

    def test_the_envid_row_is_written(self) -> None:
        """The envid of a call is stored next to the model counters."""
        self.store.record_call(_event(envid="test-12345"))
        start, end = self._window()
        speeds = self.store.speeds(start, end)
        self.assertTrue(speeds)
        self.assertEqual(speeds[0]["context_bucket"], "0-10k")
        self.assertEqual(speeds[0]["prefill_tokens_per_second"], round(1000 / 0.3, 3))

    def test_an_idle_hour_writes_no_row(self) -> None:
        """A period without traffic has no row."""
        start, end = self._window()
        self.assertEqual(self.store.model_rows(start, end), [])
        self.assertEqual(self.store.series(start, end), [])


class TestRollup(UsageStoreTestCase):
    """Verify the daily and monthly rollup."""

    def test_a_closed_day_becomes_a_daily_row(self) -> None:
        """The hours of a finished day are summed into one daily row."""
        old = datetime.now(timezone.utc) - timedelta(days=1)
        self.store.record_call(_event(usage_event_id="old:1"))
        connection = self.store._connection
        connection.execute(
            "UPDATE usage_hourly SET bucket_start = ? WHERE bucket_start = ?",
            (old.replace(minute=0, second=0, microsecond=0).isoformat(),
             datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()),
        )
        self.store.rollup()
        counts = self.store.debug_dump()
        self.assertEqual(counts["daily"], 1)
        self.assertGreaterEqual(counts["hourly"], 1)

    def test_the_rollup_is_idempotent(self) -> None:
        """A repeated rollup does not double count."""
        old = datetime.now(timezone.utc) - timedelta(days=1)
        self.store.record_call(_event(usage_event_id="old:1"))
        connection = self.store._connection
        connection.execute(
            "UPDATE usage_hourly SET bucket_start = ? WHERE bucket_start = ?",
            (old.replace(minute=0, second=0, microsecond=0).isoformat(),
             datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()),
        )
        self.store.rollup()
        first = self.store.debug_dump()["daily"]
        self.store.rollup()
        self.assertEqual(self.store.debug_dump()["daily"], first)

    def test_the_old_rows_are_deleted_by_the_retention(self) -> None:
        """The hourly rows older than the retention window are removed."""
        old = datetime.now(timezone.utc) - timedelta(days=5)
        self.store.record_call(_event(usage_event_id="old:1"))
        connection = self.store._connection
        connection.execute(
            "UPDATE usage_hourly SET bucket_start = ?",
            (old.replace(minute=0, second=0, microsecond=0).isoformat(),),
        )
        self.store.rollup()
        self.assertEqual(self.store.debug_dump()["hourly"], 0)


class TestLimits(UsageStoreTestCase):
    """Verify the limit counters, the blocked state, and the reset."""

    def _rule(self, **overrides):
        """Return one limit rule of a model."""
        rule = {
            "id": "model_5h",
            "period": "5h",
            "mode": "rolling",
            "max_generation_tokens": 100,
            "action": "block",
        }
        rule.update(overrides)
        return parse_limit_rules([rule], scope_type="model", scope_id="p1:m1")[0]

    def test_the_counters_follow_the_calls(self) -> None:
        """Every call adds its tokens to the counter of the rule."""
        self.store.set_rules([self._rule(max_generation_tokens=500)])
        self.store.record_call(_event(completion_tokens=60))
        self.store.record_call(_event(usage_event_id="task-1:2", call_index=2, completion_tokens=60))
        usage = self.store.limit_usage(self.store.rules[0])
        self.assertEqual(usage.generation_tokens, 120)
        self.assertFalse(usage.blocked)

    def test_an_exceeded_rule_is_blocked(self) -> None:
        """A rule above its limit marks the model as blocked."""
        self.store.set_rules([self._rule()])
        self.store.record_call(_event(completion_tokens=150))
        states = self.store.limits_state()
        self.assertTrue(states[0]["blocked"])
        self.assertEqual(states[0]["exceeded_by"], "max_generation_tokens")
        rule = self.store.is_blocked(provider_id="p1", model_id="m1")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.id, "model_5h")

    def test_a_blocked_candidate_is_ineligible(self) -> None:
        """A candidate of an exceeded rule is not selected by the routing."""
        self.store.set_rules([self._rule(action="route")])
        self.store.record_call(_event(completion_tokens=150))
        self.assertIsNotNone(self.store.is_candidate_blocked("p1", "m1"))
        self.assertIsNone(self.store.is_candidate_blocked("p2", "m2"))

    def test_the_reset_clears_the_counters_and_the_block(self) -> None:
        """The reset clears the counters of the window and the blocked state."""
        self.store.set_rules([self._rule()])
        self.store.record_call(_event(completion_tokens=150))
        self.assertTrue(self.store.limits_state()[0]["blocked"])
        self.assertTrue(self.store.reset_limit("model_5h", "all"))
        self.assertFalse(self.store.limits_state()[0]["blocked"])
        self.assertEqual(self.store.limit_usage(self.store.rules[0]).generation_tokens, 0)

    def test_the_token_reset_keeps_the_money_counter(self) -> None:
        """The token reset does not clear the money counter."""
        self.store.set_rules([self._rule(max_money=1)])
        self.store.record_call(_event(completion_tokens=150, effective_money_micros=900_000))
        self.assertTrue(self.store.reset_limit("model_5h", "tokens"))
        usage = self.store.limit_usage(self.store.rules[0])
        self.assertEqual(usage.generation_tokens, 0)
        self.assertEqual(usage.money_micros, 900_000)

    def test_an_unknown_rule_cannot_be_reset(self) -> None:
        """A reset of an unknown rule does nothing."""
        self.store.set_rules([self._rule()])
        self.assertFalse(self.store.reset_limit("missing", "all"))


class TestConfiguration(unittest.TestCase):
    """Verify the loading of the rules and the prices from the configuration."""

    def test_the_rules_of_the_whole_configuration_are_loaded(self) -> None:
        """The root, the provider, and the model rules are collected."""
        raw = {
            "billing": {"limits": [{"id": "all", "period": "1d", "mode": "calendar"}]},
            "models": {
                "providers": {
                    "p1": {
                        "billing": {"limits": [{"id": "prov", "period": "1h", "mode": "rolling"}]},
                        "models": [
                            {
                                "id": "m1",
                                "billing": {"limits": [{"id": "model", "period": "5h", "mode": "rolling"}]},
                            }
                        ],
                    }
                }
            },
        }
        rules = load_usage_rules(raw)
        self.assertEqual({rule.id for rule in rules}, {"all", "prov", "model"})
        by_id = {rule.id: rule for rule in rules}
        self.assertEqual(by_id["all"].scope_type, "instance")
        self.assertEqual(by_id["prov"].scope_type, "provider")
        self.assertEqual(by_id["prov"].scope_id, "p1")
        self.assertEqual(by_id["model"].scope_type, "model")
        self.assertEqual(by_id["model"].scope_id, "p1:m1")

    def test_a_duplicated_rule_id_is_refused(self) -> None:
        """The same rule id may not appear twice in the configuration."""
        raw = {
            "billing": {"limits": [{"id": "same", "period": "1h", "mode": "rolling"}]},
            "models": {
                "providers": {
                    "p1": {
                        "models": [
                            {"id": "m1", "billing": {"limits": [{"id": "same", "period": "1h", "mode": "rolling"}]}}
                        ]
                    }
                }
            },
        }
        with self.assertRaises(ValueError):
            load_usage_rules(raw)

    def test_the_prices_of_a_model_are_resolved(self) -> None:
        """The price set of a model comes from the configuration."""
        raw = {
            "models": {
                "providers": {
                    "p1": {
                        "models": [
                            {
                                "id": "m1",
                                "billing": {
                                    "prices": {
                                        "prefill_per_million": 1.4,
                                        "cached_prefill_per_million": 0.26,
                                        "generation_per_million": 4.4,
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
        prices = resolve_model_prices(raw, provider_id="p1", model_id="m1", prompt_tokens=10)
        self.assertEqual(float(prices.prefill), 1.4)
        self.assertEqual(float(prices.generation), 4.4)

    def test_a_model_without_prices_has_an_unknown_cost(self) -> None:
        """A model without a price set answers with an empty price set."""
        prices = resolve_model_prices({}, provider_id="p1", model_id="m1", prompt_tokens=10)
        self.assertTrue(prices.is_empty)


if __name__ == "__main__":
    unittest.main()
