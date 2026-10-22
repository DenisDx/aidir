"""Contract tests for prefix-affinity fingerprints, store, and readiness history."""
from __future__ import annotations

import unittest

from core.kv_affinity import (
    AnchorPolicy,
    KvAffinityStore,
    ModelReadinessTracker,
    canonical_context,
)


def _chat(*messages: dict, **extra) -> dict:
    payload = {"model": "smart_test", "messages": list(messages), "stream": False}
    payload.update(extra)
    return payload


class CanonicalContextTests(unittest.TestCase):
    def test_object_key_order_does_not_change_the_context(self):
        left = _chat({"role": "user", "content": "hello", "name": "a"})
        right = _chat({"name": "a", "content": "hello", "role": "user"})
        self.assertEqual(canonical_context(left), canonical_context(right))

    def test_sampling_and_control_fields_do_not_change_the_context(self):
        base = _chat({"role": "user", "content": "hello"})
        changed = _chat(
            {"role": "user", "content": "hello"},
            temperature=0.1,
            top_p=0.9,
            stream=True,
            priority=1,
            log={"options": {"save_llm_request": True}},
            options={"num_ctx": 4096},
        )
        self.assertEqual(canonical_context(base), canonical_context(changed))

    def test_message_content_changes_the_context(self):
        left = _chat({"role": "user", "content": "hello"})
        right = _chat({"role": "user", "content": "hello!"})
        self.assertNotEqual(canonical_context(left), canonical_context(right))

    def test_appending_a_message_keeps_the_old_prefix(self):
        first = _chat({"role": "system", "content": "rules"}, {"role": "user", "content": "one"})
        second = _chat(
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        )
        first_bytes = canonical_context(first)
        second_bytes = canonical_context(second)
        self.assertTrue(second_bytes.startswith(first_bytes))

    def test_tool_order_changes_the_context(self):
        tools = [{"function": {"name": "a"}}, {"function": {"name": "b"}}]
        left = _chat({"role": "user", "content": "hello"}, tools=tools)
        right = _chat({"role": "user", "content": "hello"}, tools=list(reversed(tools)))
        self.assertNotEqual(canonical_context(left), canonical_context(right))


class AnchorPolicyTests(unittest.TestCase):
    def test_short_context_uses_most_of_the_text(self):
        policy = AnchorPolicy()
        total = 4096
        anchor = policy.anchor_bytes(total)
        self.assertGreaterEqual(anchor, int(total * 0.8))
        self.assertLessEqual(anchor, total)

    def test_long_context_uses_a_small_capped_head(self):
        policy = AnchorPolicy(max_anchor_bytes=32768)
        total = 1_000_000
        anchor = policy.anchor_bytes(total)
        self.assertLessEqual(anchor, 32768)
        self.assertGreaterEqual(anchor, 512)

    def test_anchor_never_exceeds_the_context(self):
        policy = AnchorPolicy()
        self.assertEqual(policy.anchor_bytes(100), 100)


class KvAffinityStoreTests(unittest.TestCase):
    def _store(self, **kwargs) -> KvAffinityStore:
        policy = AnchorPolicy(short_context_bytes=1000000, medium_context_bytes=1000001,
                              long_context_bytes=1000002, short_fraction=0.9)
        return KvAffinityStore(policy=policy, **kwargs)

    def test_lookup_matches_a_growing_conversation_at_the_old_anchor(self):
        store = self._store()
        first = store.fingerprint(
            "smart_test",
            _chat({"role": "system", "content": "rules"}, {"role": "user", "content": "one"}),
        )
        store.remember(first, provider="a", model="m1", cache_group="m1")
        grown = store.fingerprint(
            "smart_test",
            _chat(
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ),
        )
        record = store.lookup(grown)
        self.assertIsNotNone(record)
        self.assertEqual((record["provider"], record["model"]), ("a", "m1"))

    def test_shorter_context_does_not_reuse_a_longer_stored_anchor(self):
        store = self._store()
        long_fp = store.fingerprint(
            "smart_test",
            _chat(
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ),
        )
        store.remember(long_fp, provider="a", model="m1")
        short_fp = store.fingerprint(
            "smart_test",
            _chat({"role": "system", "content": "rules"}, {"role": "user", "content": "one"}),
        )
        self.assertIsNone(store.lookup(short_fp))

    def test_forget_candidate_removes_every_prefix(self):
        store = self._store()
        first = store.fingerprint("smart_test", _chat({"role": "user", "content": "one"}))
        second = store.fingerprint("smart_test", _chat({"role": "user", "content": "two"}))
        store.remember(first, provider="a", model="m1")
        store.remember(second, provider="a", model="m1")
        store.forget_candidate("a", "m1")
        self.assertIsNone(store.lookup(first))
        self.assertIsNone(store.lookup(second))

    def test_ttl_expires_records(self):
        now = [0.0]
        store = KvAffinityStore(policy=AnchorPolicy(short_fraction=1.0), ttl_seconds=10, clock=lambda: now[0])
        fp = store.fingerprint("smart_test", _chat({"role": "user", "content": "one"}))
        store.remember(fp, provider="a", model="m1")
        now[0] = 11.0
        self.assertIsNone(store.lookup(fp))

    def test_per_alias_limit_evicts_the_oldest_record(self):
        store = self._store(max_entries_per_alias=1)
        first = store.fingerprint("smart_test", _chat({"role": "user", "content": "one"}))
        second = store.fingerprint("smart_test", _chat({"role": "user", "content": "two"}))
        store.remember(first, provider="a", model="m1")
        store.remember(second, provider="a", model="m2")
        self.assertIsNone(store.lookup(first))
        self.assertIsNotNone(store.lookup(second))


class ReadinessTrackerTests(unittest.TestCase):
    def _admission(self, reason: str, *, loaded: bool = True) -> dict:
        return {"admission": reason, "model_loaded": loaded}

    def test_warm_resource_block_waits_until_the_timeout(self):
        now = [0.0]
        tracker = ModelReadinessTracker(clock=lambda: now[0])
        tracker.record("p", "m", self._admission("session_memory", loaded=True), now=0.0)
        now[0] = 30.0
        self.assertFalse(tracker.should_divert(
            "p", "m", warm_timeout_seconds=60, cold_timeout_seconds=0, ratio_threshold=0.0,
        ))
        now[0] = 61.0
        self.assertTrue(tracker.should_divert(
            "p", "m", warm_timeout_seconds=60, cold_timeout_seconds=0, ratio_threshold=0.0,
        ))

    def test_cold_model_with_zero_timeout_diverts_immediately(self):
        tracker = ModelReadinessTracker(clock=lambda: 0.0)
        tracker.record("p", "m", self._admission("base_memory", loaded=False), now=0.0)
        self.assertTrue(tracker.should_divert(
            "p", "m", warm_timeout_seconds=60, cold_timeout_seconds=0, ratio_threshold=0.0,
        ))

    def test_concurrency_is_not_a_resource_block(self):
        tracker = ModelReadinessTracker(clock=lambda: 0.0)
        tracker.record("p", "m", self._admission("concurrency"), now=0.0)
        self.assertEqual(tracker.blocked_for("p", "m", now=100.0), 0.0)
        self.assertFalse(tracker.should_divert(
            "p", "m", warm_timeout_seconds=60, cold_timeout_seconds=0, ratio_threshold=0.5,
        ))

    def test_anti_flap_ratio_can_divert_before_the_current_run_timeout(self):
        now = [0.0]
        tracker = ModelReadinessTracker(history_window_seconds=600, clock=lambda: now[0])
        tracker.record("p", "m", self._admission("session_memory"), now=0.0)
        tracker.record("p", "m", self._admission("ready"), now=45.0)
        tracker.record("p", "m", self._admission("session_memory"), now=50.0)
        now[0] = 65.0
        self.assertLess(tracker.blocked_for("p", "m", now=65.0), 60)
        self.assertGreaterEqual(tracker.blocked_ratio("p", "m", now=65.0), 0.5)
        self.assertTrue(tracker.should_divert(
            "p", "m", warm_timeout_seconds=60, cold_timeout_seconds=0, ratio_threshold=0.5,
        ))

    def test_a_long_idle_gap_starts_a_fresh_history(self):
        now = [0.0]
        tracker = ModelReadinessTracker(history_window_seconds=600, clock=lambda: now[0])
        tracker.record("p", "m", self._admission("session_memory"), now=0.0)
        now[0] = 1000.0
        tracker.record("p", "m", self._admission("session_memory"), now=1000.0)
        self.assertEqual(tracker.blocked_for("p", "m", now=1000.0), 0.0)


if __name__ == "__main__":
    unittest.main()
