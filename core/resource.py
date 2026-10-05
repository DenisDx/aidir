"""
Runtime resource object.
Tracks used capacity (for example VRAM) and supports blind reservation.
Implements alive_time (soft-used window after release) and keep_alive tracking.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time


class Resource:
    """
    Runtime resource with capacity accounting and alive-time tracking.

    alive_time: seconds a model stays in VRAM after task release (e.g. Ollama 5-min default).
                Memory is considered occupied during this window; force-unload is needed to free earlier.
    keep_alive: seconds to actively keep a model loaded after last use (via cron pings). 0=disabled.
    keep_alive_period: cron ping interval for keep_alive.
    provider: id in models.providers used to resolve API URL for force-unload calls.
    """

    def __init__(
        self,
        rid: str,
        rtype: str,
        limits: dict[str, int] | None = None,
        alive_time: int = 0,
        keep_alive: int = 0,
        keep_alive_period: int = 0,
        provider: str | None = None,
        monitoring: dict | None = None,
        availability: dict | None = None,
        units: dict | None = None,
        telemetry: dict | None = None,
    ) -> None:
        self.id = rid
        self.type = rtype
        self.limits: dict[str, int] = {k: int(v) for k, v in (limits or {}).items()}
        self.used: dict[str, int] = {k: 0 for k in self.limits}
        self.consumers: dict[str, dict[str, int]] = {}
        self.alive_time: int = int(alive_time)
        self.keep_alive: int = int(keep_alive)
        self.keep_alive_period: int = int(keep_alive_period)
        self.provider: str | None = provider
        self.use: bool = True
        self.monitoring: dict = dict(monitoring) if isinstance(monitoring, dict) else {}
        self.availability: dict = dict(availability) if isinstance(availability, dict) else {}
        self.units: dict[str, str] = {
            str(metric): str(unit)
            for metric, unit in (units or {}).items()
        } if isinstance(units, dict) else {}
        self.telemetry: dict = dict(telemetry) if isinstance(telemetry, dict) else {}
        self._sensor_states: dict[str, dict] = {}
        self._availability_state: dict = {
            "source": "estimated",
            "status": "ok",
            "available": {},
            "observed_at": None,
            "error": None,
        }
        self._availability_generation = 0
        self._consumer_availability_generations: dict[str, int] = {}
        self._peer_telemetry: dict = {}
        # Soft consumers: models still in memory after task release (within alive_time window).
        # Each entry: {consumer_id, resources, released_at, model_id, provider_id}
        self._soft_used: list[dict] = []
        self._lock = asyncio.Lock()

    def has_command_availability(self) -> bool:
        """Return whether this resource has a configured command availability probe."""
        return isinstance(self.availability.get("command"), str) and bool(self.availability["command"].strip())

    def availability_configuration(self) -> dict:
        """Return the configured availability probe for internal execution."""
        return dict(self.availability)

    def record_observed_availability(self, source: str, available: dict[str, int | float]) -> None:
        """Store a successful availability observation and start its reservation generation."""
        self._availability_generation += 1
        self._availability_state = {
            "source": source,
            "status": "ok",
            "available": {
                str(metric): float(value)
                for metric, value in available.items()
            },
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
        }

    def record_availability_error(self, source: str, error: str) -> None:
        """Store a failed availability observation while retaining any previous reading."""
        previous = self._availability_state
        self._availability_state = {
            "source": source,
            "status": "error",
            "available": dict(previous.get("available") or {}),
            "observed_at": previous.get("observed_at"),
            "error": error,
        }

    def availability_snapshot(self) -> dict:
        """Return public availability state without executable configuration."""
        state = dict(self._availability_state)
        if state["source"] == "estimated":
            soft = self._compute_soft_used()
            state["available"] = {
                metric: max(0, int(limit) - int(self.used.get(metric, 0)) - int(soft.get(metric, 0)))
                for metric, limit in self.limits.items()
            }
        return state

    def telemetry_snapshot(self) -> dict:
        """Return configured safe sensor readings without executable sensor configuration."""
        if self._peer_telemetry:
            return {
                "sensors": [
                    dict(reading)
                    for reading in self._peer_telemetry.get("sensors", [])
                    if isinstance(reading, dict)
                ]
            }
        sensor_ids = self.telemetry.get("sensors")
        if not isinstance(sensor_ids, list):
            return {}
        readings: list[dict] = []
        for sensor_id in sensor_ids:
            normalized_id = str(sensor_id or "").strip()
            if not normalized_id:
                continue
            sensor_config = next(
                (item for item in self.sensor_configurations() if str(item.get("id") or "").strip() == normalized_id),
                None,
            )
            if sensor_config is None:
                continue
            state = self._sensor_states.get(normalized_id, {})
            readings.append({
                "id": normalized_id,
                "label": str(sensor_config.get("label") or normalized_id),
                "unit": str(sensor_config.get("unit") or ""),
                "status": str(state.get("status") or "pending"),
                "value": state.get("value"),
                "updated_at": state.get("updated_at"),
                "error": state.get("error"),
            })
        return {"sensors": readings}

    def record_peer_telemetry(self, telemetry: dict) -> None:
        """Store already-sanitized telemetry received from a compatible resource peer."""
        configured_sensor_ids = {
            str(sensor_id).strip()
            for sensor_id in self.telemetry.get("sensors", [])
            if str(sensor_id).strip()
        }
        remote_readings = telemetry.get("sensors") if isinstance(telemetry, dict) else None
        if not configured_sensor_ids or not isinstance(remote_readings, list):
            self._peer_telemetry = {}
            return
        self._peer_telemetry = {
            "sensors": [
                {
                    key: reading.get(key)
                    for key in ("id", "label", "unit", "status", "value", "updated_at", "error")
                }
                for reading in remote_readings
                if isinstance(reading, dict) and str(reading.get("id") or "").strip() in configured_sensor_ids
            ]
        }

    def _observed_available_after_reservations(self, metric: str) -> float | None:
        """Return observed capacity less reservations created after that observation."""
        state = self._availability_state
        if state.get("source") not in {"command", "peer"} or state.get("status") != "ok":
            return None
        available = state.get("available") or {}
        if metric not in available:
            return None
        reserved_after_observation = sum(
            int(usage.get(metric, 0))
            for consumer_id, usage in self.consumers.items()
            if self._consumer_availability_generations.get(consumer_id, 0) >= self._availability_generation
        )
        return float(available[metric]) - reserved_after_observation

    def _compute_soft_used(self) -> dict[str, int]:
        """Sum resources held by soft consumers still within alive_time window."""
        if not self.alive_time:
            return {}
        now = time.time()
        total: dict[str, int] = {}
        for entry in self._soft_used:
            if entry.get("persistent") or now - entry["released_at"] < self.alive_time:
                for k, v in entry["resources"].items():
                    total[k] = total.get(k, 0) + v
        return total

    def _matching_soft_consumer(
        self,
        model_id: str,
        required: dict[str, int] | None = None,
        provider_id: str | None = None,
    ) -> dict | None:
        """Return active soft consumer for the same model when it satisfies required usage."""
        mid = str(model_id or "").strip()
        if not self.alive_time or not mid:
            return None

        req = required or {}
        pid = str(provider_id or "").strip()
        for entry in self.get_active_soft_consumers():
            if str(entry.get("model_id") or "").strip() != mid:
                continue
            if pid and str(entry.get("provider_id") or "").strip() != pid:
                continue
            resources = entry.get("resources") or {}
            if any(int(resources.get(key, 0)) < int(amount) for key, amount in req.items() if int(amount) > 0):
                continue
            return entry
        return None

    def is_available(self, required: dict[str, int] | None = None) -> bool:
        """Return True if requested amounts fit (accounting for alive-time soft usage)."""
        if not self.use:
            return False
        req = required or {}
        for key, amount in req.items():
            need = int(amount)
            if need <= 0:
                continue
            observed = self._observed_available_after_reservations(key)
            if observed is not None:
                if need > observed:
                    return False
                continue
            if self.has_command_availability() or self._availability_state.get("source") == "peer":
                return False
            soft = self._compute_soft_used()
            limit = int(self.limits.get(key, 0))
            used = int(self.used.get(key, 0))
            soft_amount = int(soft.get(key, 0))
            if used + soft_amount + need > limit:
                return False
        return True

    def is_available_for_reuse(
        self,
        required: dict[str, int] | None = None,
        model_id: str = "",
        provider_id: str | None = None,
    ) -> bool:
        """Return True when the same warm model can be reused without unloading it."""
        if not self.use:
            return False
        if self.has_command_availability():
            return self.is_available(required)
        req = required or {}
        match = self._matching_soft_consumer(model_id, req, provider_id)
        if match is None:
            return False

        soft = self._compute_soft_used()
        matched_resources = match.get("resources") or {}
        for key, amount in req.items():
            need = int(amount)
            if need <= 0:
                continue
            limit = int(self.limits.get(key, 0))
            used = int(self.used.get(key, 0))
            soft_amount = int(soft.get(key, 0)) - int(matched_resources.get(key, 0))
            if used + max(0, soft_amount) + need > limit:
                return False
        return True

    def is_available_after_unload(self, required: dict[str, int] | None = None) -> bool:
        """Return True if amounts fit assuming all soft consumers are force-unloaded."""
        if not self.use:
            return False
        if self.has_command_availability():
            return self.is_available(required)
        req = required or {}
        for key, amount in req.items():
            need = int(amount)
            if need <= 0:
                continue
            limit = int(self.limits.get(key, 0))
            used = int(self.used.get(key, 0))
            if used + need > limit:
                return False
        return True

    def get_active_soft_consumers(self) -> list[dict]:
        """Return soft consumers still within alive_time window."""
        if not self.alive_time:
            return []
        now = time.time()
        return [
            entry for entry in self._soft_used
            if entry.get("persistent") or now - entry["released_at"] < self.alive_time
        ]

    def has_reusable_soft_consumer(
        self,
        model_id: str,
        required: dict[str, int] | None = None,
        provider_id: str | None = None,
    ) -> bool:
        """Return True when the same model is still present as a compatible soft consumer."""
        return self._matching_soft_consumer(model_id, required, provider_id) is not None

    def clear_soft_consumer(self, model_id: str, provider_id: str | None = None) -> None:
        """Remove soft consumer entry for a model (after force-unload)."""
        pid = str(provider_id or "").strip()
        self._soft_used = [
            entry for entry in self._soft_used
            if entry.get("model_id") != model_id
            or (pid and str(entry.get("provider_id") or "").strip() != pid)
        ]

    def add_soft_consumer(
        self,
        resources: dict[str, int],
        model_id: str,
        provider_id: str,
        *,
        persistent: bool = False,
    ) -> None:
        """Record a model that occupies resources outside an active task reservation."""
        self.clear_soft_consumer(model_id, provider_id)
        self._soft_used.append({
            "consumer_id": f"{provider_id}:{model_id}",
            "resources": {key: int(value) for key, value in resources.items() if int(value) > 0},
            "released_at": time.time(),
            "model_id": model_id,
            "provider_id": provider_id,
            "persistent": persistent,
        })

    def sensor_configurations(self) -> list[dict]:
        """Return configured sensor definitions for this resource."""
        sensors = self.monitoring.get("sensors")
        return [dict(sensor) for sensor in sensors if isinstance(sensor, dict)] if isinstance(sensors, list) else []

    def set_sensor_state(self, sensor_id: str, state: dict) -> None:
        """Store the latest serializable runtime state for one monitored sensor."""
        self._sensor_states[sensor_id] = dict(state)

    def sensor_snapshot(self) -> dict:
        """Return public monitoring settings and latest state without executable commands."""
        sensors: list[dict] = []
        for config in self.sensor_configurations():
            sensor_id = str(config.get("id") or "").strip()
            if not sensor_id:
                continue
            threshold = config.get("threshold")
            state = self._sensor_states.get(sensor_id, {})
            item = {
                "id": sensor_id,
                "label": str(config.get("label") or sensor_id),
                "unit": str(config.get("unit") or ""),
                "status": str(state.get("status") or "pending"),
                "value": state.get("value"),
                "updated_at": state.get("updated_at"),
                "error": state.get("error"),
                "alert": bool(state.get("alert")),
            }
            if isinstance(threshold, dict):
                item["threshold"] = {
                    "operator": str(threshold.get("operator") or "above"),
                    "value": threshold.get("value"),
                }
            sensors.append(item)
        return {
            "poll_interval": self.monitoring.get("poll_interval"),
            "sensors": sensors,
        }


    async def reserve_blind(
        self,
        required: dict[str, int] | None = None,
        consumer_id: str = "",
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> None:
        """Blindly reserve amounts without availability checks."""
        req = required or {}
        cid = consumer_id.strip()
        mid = str(model_id or "").strip()
        async with self._lock:
            if mid and self._matching_soft_consumer(mid, req, provider_id) is not None:
                self.clear_soft_consumer(mid, provider_id)
            for key, amount in req.items():
                inc = int(amount)
                if inc <= 0:
                    continue
                self.used[key] = int(self.used.get(key, 0)) + inc
                if cid:
                    by_consumer = self.consumers.setdefault(cid, {})
                    by_consumer[key] = int(by_consumer.get(key, 0)) + inc
                    self._consumer_availability_generations[cid] = self._availability_generation

    async def release(
        self,
        reserved: dict[str, int] | None = None,
        consumer_id: str = "",
        model_id: str | None = None,
        provider_id: str | None = None,
        persistent: bool = False,
    ) -> None:
        """Release previously reserved amounts. Adds to soft-used tracking if alive_time > 0."""
        req = reserved or {}
        cid = consumer_id.strip()
        async with self._lock:
            for key, amount in req.items():
                dec = int(amount)
                if dec <= 0:
                    continue
                self.used[key] = max(0, int(self.used.get(key, 0)) - dec)
                if cid and cid in self.consumers:
                    by_consumer = self.consumers[cid]
                    by_consumer[key] = max(0, int(by_consumer.get(key, 0)) - dec)
                    if by_consumer[key] == 0:
                        by_consumer.pop(key, None)
            if cid and cid in self.consumers and not self.consumers[cid]:
                self.consumers.pop(cid, None)
                self._consumer_availability_generations.pop(cid, None)
            # Track soft consumer - model may remain in VRAM for alive_time seconds after release
            mid = (model_id or "").strip()
            pid = str(provider_id or "").strip()
            if self.alive_time > 0 and req and mid:
                # Refresh existing entry so timestamp reflects the latest release
                self._soft_used = [
                    entry for entry in self._soft_used
                    if entry.get("model_id") != mid
                    or (pid and str(entry.get("provider_id") or "").strip() != pid)
                ]
                self._soft_used.append({
                    "consumer_id": cid,
                    "resources": {k: int(v) for k, v in req.items() if int(v) > 0},
                    "released_at": time.time(),
                    "model_id": mid,
                    "provider_id": pid,
                    "persistent": persistent,
                })

    def snapshot(self) -> dict:
        """Return serializable runtime state for UI/API."""
        consumers = [
            {"id": cid, "usage": dict(usage)}
            for cid, usage in sorted(self.consumers.items())
        ]
        soft = self._compute_soft_used()
        soft_consumers = [
            {
                "model_id": e["model_id"],
                "provider_id": e.get("provider_id", ""),
                "resources": e["resources"],
                "expires_in": None if e.get("persistent") else max(0, round(self.alive_time - (time.time() - e["released_at"]))),
                "persistent": bool(e.get("persistent")),
            }
            for e in self.get_active_soft_consumers()
        ]
        return {
            "id": self.id,
            "type": self.type,
            "use": self.use,
            "limits": dict(self.limits),
            "units": dict(self.units),
            "used": dict(self.used),
            "soft_used": soft,
            "consumers": consumers,
            "soft_consumers": soft_consumers,
            "monitoring": self.sensor_snapshot(),
            "availability": self.availability_snapshot(),
            "telemetry": self.telemetry_snapshot(),
        }
