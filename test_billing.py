"""Tests of the pricing, limit, and failure-classification rules."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.billing import (
    LimitRuleError,
    classify_failure,
    compute_cost_micros,
    context_bucket,
    estimate_tool_argument_tokens,
    extract_usage_fields,
    micros,
    parse_limit_rules,
    parse_period,
    period_delta,
    resolve_prices,
    rules_for_call,
    window_bounds,
)


class TestPrices(unittest.TestCase):
    """Verify the price resolution and the cost calculation."""

    def test_flat_prices_are_used_without_a_tier(self) -> None:
        """The flat prices answer when no tier matches the prompt size."""
        prices = resolve_prices(
            {"prefill_per_million": 1.4, "cached_prefill_per_million": 0.26, "generation_per_million": 4.4},
            None,
            1000,
        )
        self.assertEqual(prices.prefill, Decimal("1.4"))
        self.assertEqual(prices.cached_prefill, Decimal("0.26"))
        self.assertEqual(prices.generation, Decimal("4.4"))
        self.assertEqual(prices.cache_write, Decimal(0))

    def test_the_matching_tier_wins(self) -> None:
        """The first tier whose range contains the prompt size is used."""
        tiers = [
            {"max_prompt_tokens": 1000, "prices": {"prefill_per_million": 1.0}},
            {"min_prompt_tokens": 1001, "prices": {"prefill_per_million": 2.0}},
        ]
        self.assertEqual(resolve_prices({}, tiers, 500).prefill, Decimal("1.0"))
        self.assertEqual(resolve_prices({}, tiers, 5000).prefill, Decimal("2.0"))

    def test_the_cost_uses_the_cached_price_for_the_cached_tokens(self) -> None:
        """The cached tokens are billed at the cached price and leave the prefill."""
        prices = resolve_prices(
            {"prefill_per_million": 2.0, "cached_prefill_per_million": 0.5, "generation_per_million": 4.0},
            None,
            0,
        )
        cost = compute_cost_micros(
            prices,
            prompt_tokens=1_000_000,
            cached_prompt_tokens=500_000,
            completion_tokens=1_000_000,
        )
        self.assertEqual(cost, 500_000 * 2 + 500_000 * 0.5 + 1_000_000 * 4)

    def test_the_cache_write_price_is_optional(self) -> None:
        """A price set without a cache-write price bills the cache write at zero."""
        prices = resolve_prices({"prefill_per_million": 1.0}, None, 0)
        cost = compute_cost_micros(prices, prompt_tokens=0, cache_write_tokens=1_000_000)
        self.assertEqual(cost, 0)

    def test_money_is_stored_as_micro_units(self) -> None:
        """The money conversion is exact and never uses floating point."""
        self.assertEqual(micros("1.5"), 1_500_000)
        self.assertEqual(micros(None), 0)
        self.assertEqual(micros("not a number"), 0)


class TestContextBuckets(unittest.TestCase):
    """Verify the prompt-size buckets."""

    def test_the_bucket_of_a_prompt_size(self) -> None:
        """Every documented boundary lands in its bucket."""
        self.assertEqual(context_bucket(0), "0-10k")
        self.assertEqual(context_bucket(10_000), "0-10k")
        self.assertEqual(context_bucket(10_001), "10-50k")
        self.assertEqual(context_bucket(50_000), "10-50k")
        self.assertEqual(context_bucket(50_001), "50-100k")
        self.assertEqual(context_bucket(200_000), "100-200k")
        self.assertEqual(context_bucket(200_001), "200-350k")
        self.assertEqual(context_bucket(350_001), "350-500k")
        self.assertEqual(context_bucket(500_001), "500-750k")
        self.assertEqual(context_bucket(750_001), "750k+")
        self.assertEqual(context_bucket(1_000_000), "750k+")


class TestFailureClassification(unittest.TestCase):
    """Verify the failure classes of one LLM call."""

    def test_the_finish_reason_decides_the_length_stop(self) -> None:
        """A length stop wins over the status of a successful response."""
        self.assertEqual(classify_failure(finish_reason="length"), "length_finish")

    def test_a_refusal_is_its_own_class(self) -> None:
        """A provider refusal is not a provider failure."""
        self.assertEqual(classify_failure(finish_reason="content_filter"), "refused")

    def test_the_service_error_codes_map_to_their_class(self) -> None:
        """The error codes of the service map without inspecting the body."""
        self.assertEqual(classify_failure(error_code="UPSTREAM_TIMEOUT"), "timeout")
        self.assertEqual(classify_failure(error_code="UPSTREAM_UNREACHABLE"), "unreachable")
        self.assertEqual(classify_failure(error_code="UPSTREAM_INVALID_JSON"), "invalid_response")
        self.assertEqual(classify_failure(error_code="CANCELLED"), "canceled")
        self.assertEqual(classify_failure(error_code="LLAMA_CPP_START_FAILED"), "model_unavailable")
        self.assertEqual(classify_failure(error_code="TOOL_LOOP_LIMIT"), "tool_loop_limit")
        self.assertEqual(classify_failure(error_code="BUDGET_EXCEEDED"), "budget_exceeded")

    def test_the_status_decides_the_http_classes(self) -> None:
        """A status without a known code maps to the http class."""
        self.assertEqual(classify_failure(http_status=404), "http_4xx")
        self.assertEqual(classify_failure(http_status=502), "http_5xx")
        self.assertEqual(classify_failure(http_status=429), "rate_limited")

    def test_a_billing_refusal_is_the_allowance_class(self) -> None:
        """A provider billing refusal is the allowance class, whatever the status."""
        self.assertEqual(classify_failure(http_status=402), "allowance_exhausted")
        self.assertEqual(
            classify_failure(http_status=429, error_body="{\"error\":{\"code\":\"credit_balance_exhausted\"}}"),
            "allowance_exhausted",
        )
        self.assertEqual(
            classify_failure(http_status=400, error_body="Your credit balance is too low to access the Anthropic API."),
            "allowance_exhausted",
        )

    def test_an_empty_answer_is_its_own_class(self) -> None:
        """A successful call without an answer is an empty response."""
        self.assertEqual(classify_failure(empty_response=True), "empty_response")

    def test_an_unknown_failure_is_other_error(self) -> None:
        """A failure without a known signal falls into other_error."""
        self.assertEqual(classify_failure(error_code="EXCEPTION"), "other_error")


class TestUsageExtraction(unittest.TestCase):
    """Verify the metering fields of an internal response."""

    def test_the_provider_usage_object_is_read(self) -> None:
        """The cached, the reasoning, and the billing fields are taken from the usage."""
        response = {
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 300,
                "prompt_tokens_details": {"cached_tokens": 800},
                "completion_tokens_details": {"reasoning_tokens": 120},
                "cost": 0.00412,
                "opencode": {
                    "billing_source": "go",
                    "list_cost": 0.00412,
                    "allowance": 0.00274,
                    "estimated": False,
                },
            },
            "message": {"content": "hello"},
            "done_reason": "stop",
        }
        fields = extract_usage_fields(response)
        self.assertEqual(fields["prompt_tokens"], 1200)
        self.assertEqual(fields["cached_prompt_tokens"], 800)
        self.assertEqual(fields["completion_tokens"], 300)
        self.assertEqual(fields["reasoning_tokens"], 120)
        self.assertEqual(fields["answer_tokens"], 180)
        self.assertEqual(fields["reported_cost_micros"], 4_120)
        self.assertEqual(fields["allowance_micros"], 2_740)
        self.assertEqual(fields["list_cost_micros"], 4_120)
        self.assertEqual(fields["billing_source"], "go")
        self.assertFalse(fields["empty_response"])

    def test_an_unknown_reasoning_share_is_zero(self) -> None:
        """A provider that does not report reasoning yields zero reasoning tokens."""
        fields = extract_usage_fields({"usage": {"prompt_tokens": 10, "completion_tokens": 20}})
        self.assertEqual(fields["reasoning_tokens"], 0)
        self.assertEqual(fields["answer_tokens"], 20)

    def test_an_empty_answer_is_detected(self) -> None:
        """A finished call without content, reasoning, and tool calls is empty."""
        fields = extract_usage_fields({"message": {"content": "  "}, "done_reason": "stop"})
        self.assertTrue(fields["empty_response"])

    def test_the_tool_argument_tokens_are_measured_from_the_generated_text(self) -> None:
        """The tool-call text is measured and stored as an estimate."""
        calls = [{"function": {"name": "search", "arguments": "{\"q\":\"abcdefgh\"}"}}]
        self.assertEqual(estimate_tool_argument_tokens(calls), (6 + 15) // 4)
        self.assertEqual(estimate_tool_argument_tokens([]), 0)

    def test_the_provider_timings_are_used_when_present(self) -> None:
        """A provider timing block is the timing source of the call."""
        fields = extract_usage_fields({"timings": {"prompt_ms": 120, "predicted_ms": 400}})
        self.assertEqual(fields["prefill_ms"], 120)
        self.assertEqual(fields["generation_ms"], 400)
        self.assertEqual(fields["timing_source"], "provider")

    def test_a_synchronous_response_without_timings_stores_the_total_only(self) -> None:
        """A remote synchronous call without timings has no invented split."""
        fields = extract_usage_fields({"usage": {"prompt_tokens": 10, "completion_tokens": 20}})
        self.assertEqual(fields["timing_source"], "total_only")
        self.assertEqual(fields["prefill_ms"], 0)
        self.assertEqual(fields["generation_ms"], 0)

    def test_the_counters_of_a_streamed_answer_are_found(self) -> None:
        """A streamed answer carries its counters in the chunk that has them."""
        response = {
            "final": {"choices": [{"delta": {}}], "done_reason": "stop"},
            "usage_chunk": {
                "usage": {
                    "prompt_tokens": 104454,
                    "completion_tokens": 201,
                    "prompt_tokens_details": {"cached_tokens": 103138},
                },
                "timings": {
                    "cache_n": 103138,
                    "prompt_n": 1316,
                    "prompt_ms": 6050.7,
                    "predicted_n": 201,
                    "predicted_ms": 2005.3,
                },
            },
        }
        fields = extract_usage_fields(response)
        self.assertEqual(fields["prompt_tokens"], 104454)
        self.assertEqual(fields["cached_prompt_tokens"], 103138)
        self.assertEqual(fields["completion_tokens"], 201)
        self.assertEqual(fields["prefill_ms"], 6050)
        self.assertEqual(fields["generation_ms"], 2005)
        self.assertEqual(fields["timing_source"], "provider")

    def test_the_counters_are_found_in_the_chunk_list_of_a_stream(self) -> None:
        """A stored chunk list is scanned from its end."""
        response = {
            "stream_chunks": [
                {"choices": [{"delta": {"content": "hi"}}]},
                {"usage": {"prompt_tokens": 42, "completion_tokens": 7}},
            ],
        }
        fields = extract_usage_fields(response)
        self.assertEqual(fields["prompt_tokens"], 42)
        self.assertEqual(fields["completion_tokens"], 7)

    def test_the_prefill_counts_only_the_new_tokens(self) -> None:
        """The cached tokens are restored, not computed, and do not count."""
        fields = extract_usage_fields({
            "usage": {
                "prompt_tokens": 104454,
                "completion_tokens": 201,
                "prompt_tokens_details": {"cached_tokens": 103138},
            },
            "timings": {"cache_n": 103138, "prompt_n": 1316, "prompt_ms": 6050.7},
        })
        self.assertEqual(fields["prefill_tokens"], 1316)
        self.assertEqual(fields["cached_prompt_tokens"], 103138)

    def test_the_prefill_falls_back_to_the_uncached_tokens(self) -> None:
        """Without a provider counter the uncached tokens are the prefill."""
        fields = extract_usage_fields({
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
            "timings": {"prompt_ms": 500},
        })
        self.assertEqual(fields["prefill_tokens"], 200)


class TestLimitRules(unittest.TestCase):
    """Verify the parsing and the windows of the limit rules."""

    def test_a_period_is_normalized(self) -> None:
        """The period keeps its compact spelling."""
        self.assertEqual(parse_period("5h"), "5h")
        self.assertEqual(parse_period("1M"), "1M")
        self.assertEqual(parse_period("PT2H"), "2h")
        self.assertEqual(parse_period("P1D"), "1d")

    def test_an_ambiguous_period_is_refused(self) -> None:
        """A period that cannot be read is a configuration error."""
        for value in ("", "5", "m", "1x", "P", "PT"):
            with self.assertRaises(LimitRuleError):
                parse_period(value)

    def test_the_period_length_is_resolved(self) -> None:
        """The normalized period maps to its duration."""
        self.assertEqual(period_delta("5h"), timedelta(hours=5))
        self.assertEqual(period_delta("1d"), timedelta(days=1))
        self.assertEqual(period_delta("1w"), timedelta(weeks=1))

    def test_the_rules_of_a_scope_are_parsed(self) -> None:
        """A rule keeps its scope, its window, and its action."""
        rules = parse_limit_rules(
            [{"id": "r1", "period": "5h", "mode": "rolling", "max_money": 12, "action": "block"}],
            scope_type="model",
            scope_id="p:model",
        )
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].id, "r1")
        self.assertEqual(rules[0].scope_type, "model")
        self.assertEqual(rules[0].scope_id, "p:model")
        self.assertTrue(rules[0].blocks)
        self.assertTrue(rules[0].excludes_candidate)
        self.assertEqual(rules[0].max_money, Decimal(12))

    def test_a_duplicated_rule_id_is_a_configuration_error(self) -> None:
        """The same rule id must not appear twice."""
        seen: set[str] = set()
        parse_limit_rules(
            [{"id": "r1", "period": "1h", "mode": "rolling"}],
            scope_type="instance",
            seen_ids=seen,
        )
        with self.assertRaises(LimitRuleError):
            parse_limit_rules(
                [{"id": "r1", "period": "1h", "mode": "rolling"}],
                scope_type="provider",
                scope_id="p",
                seen_ids=seen,
            )

    def test_an_invalid_action_is_refused(self) -> None:
        """Only the documented actions are accepted."""
        with self.assertRaises(LimitRuleError):
            parse_limit_rules(
                [{"id": "r1", "period": "1h", "mode": "rolling", "action": "deny"}],
                scope_type="instance",
            )

    def test_the_rules_of_a_call_are_selected_by_scope(self) -> None:
        """A provider rule covers its models, a model rule covers its model."""
        rules = [
            *parse_limit_rules([{"id": "all", "period": "1h", "mode": "rolling"}], scope_type="instance"),
            *parse_limit_rules(
                [{"id": "prov", "period": "1h", "mode": "rolling"}],
                scope_type="provider",
                scope_id="p1",
            ),
            *parse_limit_rules(
                [{"id": "model", "period": "1h", "mode": "rolling"}],
                scope_type="model",
                scope_id="p1:m1",
            ),
        ]
        selected = rules_for_call(rules, provider_id="p1", model_key="p1:m1")
        self.assertEqual({rule.id for rule in selected}, {"all", "prov", "model"})
        selected = rules_for_call(rules, provider_id="p2", model_key="p2:m2")
        self.assertEqual({rule.id for rule in selected}, {"all"})

    def test_a_rolling_window_looks_back_one_period(self) -> None:
        """The rolling window ends at the moment of the evaluation."""
        now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
        start, end = window_bounds("5h", "rolling", now)
        self.assertEqual(end, now)
        self.assertEqual(start, now - timedelta(hours=5))

    def test_a_calendar_window_is_aligned(self) -> None:
        """The calendar window starts at the boundary of its unit."""
        now = datetime(2026, 11, 12, 12, 30, tzinfo=timezone.utc)
        start, end = window_bounds("1d", "calendar", now)
        self.assertEqual(start, datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc))
        start, _ = window_bounds("1M", "calendar", now)
        self.assertEqual(start, datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc))
        start, _ = window_bounds("5h", "calendar", now)
        self.assertEqual(start, datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
