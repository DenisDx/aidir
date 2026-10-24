"""Prefix-affinity routing state and model readiness history.

The store keys a bounded context prefix to the concrete executor that served it.
The readiness tracker keeps a small transition history per provider/model so the
router can distinguish a temporary resource conflict from a failed executor.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import asyncio
import hashlib
import hmac
import json
import os
import time
from typing import Any

DEFAULT_ANCHOR_LADDER = (
    256, 512, 1024, 1536, 2048, 2560, 3072, 3584, 4096,
    6144, 8192, 10240, 12288, 16384, 20480, 24576, 28672, 32768,
)

# Payload keys that never shape the rendered prompt.
_NON_PROMPT_FIELDS = {
    "model",
    "stream",
    "worker",
    "priority",
    "queue_timeout",
    "timeout",
    "request_timeout",
    "run_timeout",
    "keep_alive",
    "log",
    "envid",
    "save_llm_request",
    "opencode_session",
    "request_kind",
    "prompt_cache_key",
    "options",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "typical_p",
    "seed",
    "max_tokens",
    "max_completion_tokens",
    "num_predict",
    "presence_penalty",
    "frequency_penalty",
    "repeat_penalty",
    "repeat_last_n",
    "stop",
    "n",
    "user",
}

_RESOURCE_BLOCK_REASONS = {"session_memory", "base_memory", "waiting_unload"}


@dataclass(frozen=True, slots=True)
class KvFingerprint:
    """Canonical context identity used for one routing decision."""

    request_kind: str
    total_bytes: int
    anchor_bytes: int
    hash: str
    text: bytes


@dataclass(frozen=True, slots=True)
class AnchorPolicy:
    """Adaptive context-prefix length policy."""

    min_anchor_bytes: int = 512
    max_anchor_bytes: int = 32768
    short_context_bytes: int = 10240
    medium_context_bytes: int = 20480
    long_context_bytes: int = 102400
    short_fraction: float = 0.90
    medium_fraction: float = 0.50
    long_fraction: float = 0.10
    ladder: tuple[int, ...] = DEFAULT_ANCHOR_LADDER

    @classmethod
    def from_config(cls, cfg: dict | None) -> "AnchorPolicy":
        """Build the policy from routing.kv_affinity, keeping safe defaults."""
        cfg = cfg if isinstance(cfg, dict) else {}

        def _int(name: str, default: int) -> int:
            try:
                return max(1, int(cfg.get(name, default)))
            except (TypeError, ValueError):
                return default

        def _float(name: str, default: float) -> float:
            try:
                return min(1.0, max(0.0, float(cfg.get(name, default))))
            except (TypeError, ValueError):
                return default

        ladder_raw = cfg.get("anchor_ladder")
        ladder: tuple[int, ...] = DEFAULT_ANCHOR_LADDER
        if isinstance(ladder_raw, list):
            values = []
            for item in ladder_raw:
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    values.append(value)
            if values:
                ladder = tuple(sorted(set(values)))

        minimum = _int("min_anchor_bytes", 512)
        maximum = _int("max_anchor_bytes", 32768)
        if maximum < minimum:
            maximum = minimum
        short = _int("short_context_bytes", 10240)
        medium = max(short, _int("medium_context_bytes", 20480))
        long = max(medium, _int("long_context_bytes", 102400))
        return cls(
            min_anchor_bytes=minimum,
            max_anchor_bytes=maximum,
            short_context_bytes=short,
            medium_context_bytes=medium,
            long_context_bytes=long,
            short_fraction=_float("short_fraction", 0.90),
            medium_fraction=_float("medium_fraction", 0.50),
            long_fraction=_float("long_fraction", 0.10),
            ladder=ladder,
        )

    def anchor_bytes(self, total_bytes: int) -> int:
        """Return the quantized anchor length for a canonical context size."""
        total = max(0, int(total_bytes))
        if total <= 0:
            return 0
        if total <= self.short_context_bytes:
            ratio = self.short_fraction
        elif total <= self.medium_context_bytes:
            ratio = self.medium_fraction
        elif total <= self.long_context_bytes:
            span = max(1, self.long_context_bytes - self.medium_context_bytes)
            progress = (total - self.medium_context_bytes) / span
            ratio = self.medium_fraction + (self.long_fraction - self.medium_fraction) * progress
        else:
            ratio = self.long_fraction
        raw = int(total * ratio)
        raw = max(self.min_anchor_bytes, min(self.max_anchor_bytes, raw))
        raw = min(total, raw)
        return self.quantize(raw)

    def quantize(self, anchor: int) -> int:
        """Round an anchor length down to the configured ladder."""
        value = max(0, int(anchor))
        if value <= 0:
            return 0
        chosen = 0
        for step in self.ladder:
            if step <= value:
                chosen = step
            else:
                break
        return chosen or value


def _json_bytes(value: Any) -> bytes:
    """Serialize one canonical component deterministically."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def canonical_context(payload: dict | None, request_kind: str = "chat") -> bytes:
    """Serialize the prompt-shaping part of a request as an append-stable stream.

    Stable fields are written before messages, and each message is a separate
    line. Adding a turn therefore appends bytes instead of rewriting the closing
    brackets of a single JSON document, which keeps an older anchor valid.
    """
    body = payload if isinstance(payload, dict) else {}
    selected: dict[str, Any] = {}
    for key, value in body.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        if key in _NON_PROMPT_FIELDS:
            continue
        selected[key] = value

    parts: list[bytes] = [b"kv1", str(request_kind or "chat").encode("utf-8")]
    # Stable prefix-shaping fields first.
    for key in (
        "system",
        "tools",
        "tool_choice",
        "response_format",
        "format",
        "think",
        "raw",
        "context_builder",
        "template",
    ):
        if key in selected:
            parts.append(key.encode("utf-8") + b":" + _json_bytes(selected.pop(key)))

    messages = selected.pop("messages", None)
    if isinstance(messages, list):
        for message in messages:
            parts.append(b"message:" + _json_bytes(message))
    elif messages is not None:
        parts.append(b"messages:" + _json_bytes(messages))

    for key in ("prompt", "input"):
        if key in selected:
            parts.append(key.encode("utf-8") + b":" + _json_bytes(selected.pop(key)))

    for key in sorted(selected):
        parts.append(key.encode("utf-8") + b":" + _json_bytes(selected[key]))
    return b"\n".join(parts)


def _prefix_hash(prefix: bytes, *, request_kind: str, secret: str) -> str:
    """Return a truncated HMAC-SHA256 of one anchor prefix."""
    message = (
        b"kv2\x00"
        + str(request_kind or "chat").encode("utf-8")
        + b"\x00"
        + prefix
    )
    if secret:
        digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    else:
        digest = hashlib.sha256(message).digest()
    return digest[:16].hex()


class KvAffinityStore:
    """Bounded, TTL-aware memory of context prefixes and their executors."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        policy: AnchorPolicy | None = None,
        ttl_seconds: int = 3600,
        max_entries: int = 20000,
        clock=time.time,
    ) -> None:
        self.enabled = bool(enabled)
        self.policy = policy or AnchorPolicy()
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._records: OrderedDict[tuple[int, str], dict] = OrderedDict()
        self._redis = None
        self._namespace = "aidir"

    @classmethod
    def from_config(cls, full_config: dict | None, *, clock=time.time) -> "KvAffinityStore":
        """Build the store from routing.kv_affinity in the full configuration."""
        routing = ((full_config or {}).get("routing") or {}) if isinstance(full_config, dict) else {}
        cfg = routing.get("kv_affinity") if isinstance(routing, dict) else None
        cfg = cfg if isinstance(cfg, dict) else {}
        secret = str(cfg.get("_secret") or os.environ.get("AIDIR_KV_AFFINITY_SECRET", ""))
        store = cls(
            enabled=bool(cfg.get("enabled", True)),
            policy=AnchorPolicy.from_config(cfg),
            ttl_seconds=int(cfg.get("ttl_seconds", 3600) or 3600),
            max_entries=int(cfg.get("max_entries", 20000) or 20000),
            clock=clock,
        )
        store._secret = secret
        return store

    @property
    def _secret(self) -> str:
        return getattr(self, "_secret_value", "")

    @_secret.setter
    def _secret(self, value: str) -> None:
        self._secret_value = str(value or "")

    def attach_redis(self, redis, namespace: str = "aidir") -> None:
        """Attach an optional Redis mirror while keeping the in-process store hot."""
        self._redis = redis
        self._namespace = str(namespace or "aidir")

    def _mirror_key(self, record_hash: str) -> str:
        """Return the Redis key for one affinity record."""
        return f"{self._namespace}:kv_affinity:{record_hash}"

    def _schedule_mirror(self, coroutine) -> None:
        """Run one mirror write without blocking the routing hot path."""
        if self._redis is None:
            coroutine.close()
            return
        try:
            asyncio.get_running_loop().create_task(coroutine)
        except RuntimeError:
            coroutine.close()

    async def lookup_remote(self, fingerprint: KvFingerprint | None) -> dict | None:
        """Return the most specific mirrored record for a fingerprint."""
        if self._redis is None or not self.enabled or fingerprint is None:
            return None
        lengths = [step for step in self.policy.ladder if step <= fingerprint.anchor_bytes]
        if fingerprint.anchor_bytes not in lengths:
            lengths.append(fingerprint.anchor_bytes)
        hashes = {
            length: _prefix_hash(
                fingerprint.text[:length],
                request_kind=fingerprint.request_kind,
                secret=self._secret,
            )
            for length in lengths
        }
        try:
            raw_records = await self._redis.mget([self._mirror_key(value) for value in hashes.values()])
        except Exception:
            return None
        best: dict | None = None
        now = self._clock()
        for length, raw in zip(lengths, raw_records):
            if not raw:
                continue
            try:
                record = json.loads(raw)
                expires_at = float(record.get("expires_at") or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if expires_at <= now:
                continue
            if best is None or length > int(best.get("anchor_bytes") or 0):
                best = record
        if best is None:
            return None
        best["matched_anchor_bytes"] = int(best.get("anchor_bytes") or 0)
        key = (int(best.get("anchor_bytes") or 0), str(best.get("hash") or ""))
        self._records[key] = dict(best)
        self._records.move_to_end(key)
        self._enforce_bounds()
        return dict(best)

    async def _write_mirror(self, record: dict) -> None:
        """Write one record and add it to the mirror index."""
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.set(
                self._mirror_key(str(record.get("hash") or "")),
                json.dumps(record, ensure_ascii=False),
                ex=max(1, int(record.get("expires_at") or self._clock() + self.ttl_seconds) - int(self._clock())),
            )
            pipe.sadd(f"{self._namespace}:kv_affinity:index", str(record.get("hash") or ""))
            await pipe.execute()
        except Exception:
            return

    async def _forget_candidate_mirror(self, provider_id: str, model_id: str) -> None:
        """Delete mirrored records routed to one candidate."""
        try:
            index_key = f"{self._namespace}:kv_affinity:index"
            hashes = await self._redis.smembers(index_key)
            if not hashes:
                return
            raw_records = await self._redis.mget([self._mirror_key(value) for value in hashes])
            stale = []
            for raw in raw_records:
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if record.get("provider") == provider_id and record.get("model") == model_id:
                    stale.append(str(record.get("hash") or ""))
            if stale:
                await self._redis.delete(*[self._mirror_key(value) for value in stale])
                await self._redis.srem(index_key, *stale)
        except Exception:
            return

    def fingerprint(self, payload: dict | None, request_kind: str = "chat") -> KvFingerprint | None:
        """Build the canonical context fingerprint for one request."""
        if not self.enabled:
            return None
        text = canonical_context(payload, request_kind)
        total = len(text)
        if total <= 0:
            return None
        anchor = self.policy.anchor_bytes(total)
        if anchor <= 0:
            return None
        kind = str(request_kind or "chat")
        return KvFingerprint(
            request_kind=kind,
            total_bytes=total,
            anchor_bytes=anchor,
            hash=_prefix_hash(text[:anchor], request_kind=kind, secret=self._secret),
            text=text,
        )

    def lookup(self, fingerprint: KvFingerprint | None) -> dict | None:
        """Return the most specific live record matching the current fingerprint."""
        if not self.enabled or fingerprint is None:
            return None
        now = self._clock()
        self._prune(now)
        best: dict | None = None
        hashes: dict[int, str] = {}
        for (length, stored_hash), record in list(self._records.items()):
            if length > fingerprint.anchor_bytes or length > fingerprint.total_bytes:
                continue
            if length not in hashes:
                hashes[length] = _prefix_hash(
                    fingerprint.text[:length],
                    request_kind=fingerprint.request_kind,
                    secret=self._secret,
                )
            if hashes[length] != stored_hash:
                continue
            if best is None or length > int(best.get("anchor_bytes") or 0):
                best = record
            elif length == int(best.get("anchor_bytes") or 0) and record.get("last_used", 0) > best.get("last_used", 0):
                best = record
        if best is None:
            return None
        best["last_used"] = now
        best["hits"] = int(best.get("hits") or 0) + 1
        best_key = (int(best.get("anchor_bytes") or 0), str(best.get("hash") or ""))
        self._records.move_to_end(best_key)
        out = dict(best)
        out["matched_anchor_bytes"] = int(best.get("anchor_bytes") or 0)
        return out

    def remember(
        self,
        fingerprint: KvFingerprint | None,
        *,
        provider: str,
        model: str,
        cache_group: str = "",
        reason: str = "",
    ) -> None:
        """Store or refresh the candidate that served a fingerprint."""
        if not self.enabled or fingerprint is None:
            return
        provider_id = str(provider or "").strip()
        model_id = str(model or "").strip()
        if not provider_id or not model_id:
            return
        now = self._clock()
        self._prune(now)
        key = (fingerprint.anchor_bytes, fingerprint.hash)
        record = self._records.get(key)
        if record is None:
            record = {
                "version": 2,
                "anchor_bytes": fingerprint.anchor_bytes,
                "hash": fingerprint.hash,
                "cache_group": str(cache_group or model_id),
                "provider": provider_id,
                "model": model_id,
                "last_used": now,
                "expires_at": now + self.ttl_seconds,
                "hits": 0,
                "last_reason": str(reason or ""),
            }
            self._records[key] = record
        else:
            record.update(
                {
                    "cache_group": str(cache_group or model_id),
                    "provider": provider_id,
                    "model": model_id,
                    "last_used": now,
                    "expires_at": now + self.ttl_seconds,
                    "last_reason": str(reason or ""),
                }
            )
            self._records.move_to_end(key)
        self._enforce_bounds()
        self._schedule_mirror(self._write_mirror(dict(record)))

    def forget_prefix(self, fingerprint: KvFingerprint | None) -> None:
        """Remove the exact record for one fingerprint."""
        if fingerprint is None:
            return
        self._records.pop((fingerprint.anchor_bytes, fingerprint.hash), None)

    def forget_candidate(self, provider: str, model: str) -> None:
        """Invalidate every prefix routed to one concrete executor."""
        provider_id = str(provider or "").strip()
        model_id = str(model or "").strip()
        if not provider_id or not model_id:
            return
        for key, record in list(self._records.items()):
            if record.get("provider") == provider_id and record.get("model") == model_id:
                self._records.pop(key, None)
        self._schedule_mirror(self._forget_candidate_mirror(provider_id, model_id))

    def clear(self) -> None:
        """Drop every affinity record."""
        self._records.clear()

    def stats(self) -> dict:
        """Return a small diagnostic snapshot without prompt content."""
        return {
            "enabled": self.enabled,
            "records": len(self._records),
            "ttl_seconds": self.ttl_seconds,
        }

    def _prune(self, now: float) -> None:
        """Remove expired records."""
        for key, record in list(self._records.items()):
            if float(record.get("expires_at") or 0) <= now:
                self._records.pop(key, None)

    def _enforce_bounds(self) -> None:
        """Apply the global LRU limit."""
        while len(self._records) > self.max_entries:
            self._records.popitem(last=False)


class ModelReadinessTracker:
    """Bounded transition history of model admission states."""

    def __init__(self, *, history_window_seconds: int = 600, clock=time.time) -> None:
        self.history_window_seconds = max(1, int(history_window_seconds))
        self._clock = clock
        self._states: dict[tuple[str, str], dict] = {}

    def record(
        self,
        provider: str,
        model: str,
        admission: dict | None,
        *,
        now: float | None = None,
    ) -> None:
        """Record one admission observation for a concrete provider/model."""
        provider_id = str(provider or "").strip()
        model_id = str(model or "").strip()
        if not provider_id or not model_id or not isinstance(admission, dict):
            return
        current = float(self._clock() if now is None else now)
        key = (provider_id, model_id)
        entry = self._states.get(key)
        if entry is None or current - float(entry.get("last_sample_at") or 0) > self.history_window_seconds:
            entry = {
                "transitions": [],
                "blocked_since": None,
                "last_sample_at": current,
                "last_admission": {},
            }
            self._states[key] = entry

        reason = str(admission.get("admission") or "")
        state = "blocked" if reason in _RESOURCE_BLOCK_REASONS else ("ready" if reason == "ready" else "other")
        if state == "blocked":
            if entry["blocked_since"] is None:
                entry["blocked_since"] = current
        else:
            entry["blocked_since"] = None

        previous = entry.get("last_admission") or {}
        if (
            not previous
            or str(previous.get("admission") or "") != reason
            or previous.get("model_loaded") != admission.get("model_loaded")
        ):
            entry["transitions"].append(
                {
                    "ts": current,
                    "state": state,
                    "admission": reason,
                    "model_loaded": admission.get("model_loaded"),
                    "free": admission.get("free") or {},
                    "needed": admission.get("needed") or {},
                }
            )
        entry["last_admission"] = dict(admission)
        entry["last_sample_at"] = current
        self._prune(entry, current)

    def should_divert(
        self,
        provider: str,
        model: str,
        *,
        warm_timeout_seconds: int,
        cold_timeout_seconds: int,
        ratio_threshold: float,
        now: float | None = None,
    ) -> bool:
        """Return whether affinity should give up on a resource-blocked candidate."""
        entry = self._states.get((str(provider or "").strip(), str(model or "").strip()))
        if entry is None or entry.get("blocked_since") is None:
            return False
        current = float(self._clock() if now is None else now)
        blocked_since = float(entry["blocked_since"])
        last = entry.get("last_admission") or {}
        model_loaded = last.get("model_loaded")
        wait_limit = max(0, int(warm_timeout_seconds if model_loaded is not False else cold_timeout_seconds))
        blocked_for = max(0.0, current - blocked_since)
        if blocked_for >= wait_limit:
            return True
        threshold = max(0.0, float(ratio_threshold or 0.0))
        if threshold <= 0:
            return False
        observed = self.observed_span(provider, model, now=current)
        if observed < max(1.0, float(wait_limit)):
            return False
        return self.blocked_ratio(provider, model, now=current) >= threshold

    def blocked_for(self, provider: str, model: str, *, now: float | None = None) -> float:
        """Return how long the candidate has been continuously resource-blocked."""
        entry = self._states.get((str(provider or "").strip(), str(model or "").strip()))
        if entry is None or entry.get("blocked_since") is None:
            return 0.0
        current = float(self._clock() if now is None else now)
        return max(0.0, current - float(entry["blocked_since"]))

    def blocked_ratio(self, provider: str, model: str, *, now: float | None = None) -> float:
        """Return the blocked fraction of the observed portion of the history window."""
        entry = self._states.get((str(provider or "").strip(), str(model or "").strip()))
        if entry is None:
            return 0.0
        current = float(self._clock() if now is None else now)
        observed = self.observed_span(provider, model, now=current)
        if observed <= 0:
            return 0.0
        transitions = entry.get("transitions") or []
        window_start = current - self.history_window_seconds
        blocked = 0.0
        for index, transition in enumerate(transitions):
            if str(transition.get("state") or "") != "blocked":
                continue
            start = max(float(transition.get("ts") or 0), window_start)
            if index + 1 < len(transitions):
                end = min(float(transitions[index + 1].get("ts") or current), current)
            else:
                end = current
            if end > start:
                blocked += end - start
        return min(1.0, max(0.0, blocked / observed))

    def observed_span(self, provider: str, model: str, *, now: float | None = None) -> float:
        """Return how much history is available for one candidate, capped by the window."""
        entry = self._states.get((str(provider or "").strip(), str(model or "").strip()))
        if entry is None:
            return 0.0
        current = float(self._clock() if now is None else now)
        transitions = entry.get("transitions") or []
        first = float(transitions[0]["ts"]) if transitions else float(entry.get("last_sample_at") or current)
        return max(0.0, min(float(self.history_window_seconds), current - first))

    def last_admission(self, provider: str, model: str) -> dict:
        """Return the latest admission observation for one candidate."""
        entry = self._states.get((str(provider or "").strip(), str(model or "").strip()))
        return dict(entry.get("last_admission") or {}) if entry else {}

    def clear_candidate(self, provider: str, model: str) -> None:
        """Drop the history for one executor."""
        self._states.pop((str(provider or "").strip(), str(model or "").strip()), None)

    def clear(self) -> None:
        """Drop all readiness history."""
        self._states.clear()

    def _prune(self, entry: dict, now: float) -> None:
        """Discard transitions older than the history window, keeping one boundary."""
        cutoff = now - self.history_window_seconds
        transitions = entry.get("transitions") or []
        keep_from = 0
        for index, transition in enumerate(transitions):
            if float(transition.get("ts") or 0) >= cutoff:
                keep_from = max(0, index - 1)
                break
        else:
            keep_from = max(0, len(transitions) - 1)
        if keep_from:
            entry["transitions"] = transitions[keep_from:]
