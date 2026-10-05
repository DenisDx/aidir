"""Configurable command-based resource sensor monitoring."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import re
import time
from typing import TYPE_CHECKING

from core import log

if TYPE_CHECKING:
    from core.peer_resources import PeerResourceClient
    from core.resources import Resources


class SensorCommandError(RuntimeError):
    """Report a sensor command or its output as invalid."""


class ResourceMonitor:
    """Poll configured resource sensors and trigger their threshold reactions."""

    def __init__(self, resources: "Resources", peer_client: "PeerResourceClient | None" = None) -> None:
        self._resources = resources
        self._peer_client = peer_client
        self._stopped = asyncio.Event()
        self._next_poll_at: dict[str, float] = {}
        self._last_reaction_at: dict[tuple[str, str], float] = {}
        self._configuration_errors: set[tuple[str, str, str]] = set()

    async def run(self) -> None:
        """Poll configured sensors until stop() is called."""
        while not self._stopped.is_set():
            await self.poll_due_resources()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._seconds_until_next_poll())
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Stop the monitoring loop after its current polling cycle."""
        self._stopped.set()

    def _seconds_until_next_poll(self) -> float:
        """Return the delay until the earliest scheduled resource poll."""
        if not self._next_poll_at:
            return 1.0
        return max(0.0, min(self._next_poll_at.values()) - time.monotonic())

    async def poll_due_resources(self) -> None:
        """Poll each configured resource when its poll interval has elapsed."""
        now = time.monotonic()
        for resource in self._resources.all():
            interval = self._poll_interval(resource)
            if interval is None:
                continue
            if now < self._next_poll_at.get(resource.id, 0):
                continue
            self._next_poll_at[resource.id] = now + interval
            await self.poll_resource(resource.id)

    async def poll_resource(self, resource_id: str) -> None:
        """Refresh command availability and poll every configured sensor of one resource."""
        resource = self._resources.get(resource_id)
        if resource is None:
            return
        await self.refresh_availability(resource_id)
        for sensor in resource.sensor_configurations():
            await self._poll_sensor(resource, sensor)

    async def refresh_availability(self, resource_id: str) -> bool:
        """Refresh one command-backed resource availability and return probe success."""
        resource = self._resources.get(resource_id)
        if resource is None:
            return False
        if not resource.has_command_availability():
            return await self._refresh_peer_availability(resource)

        config = resource.availability_configuration()
        metric = str(config.get("metric") or "").strip()
        if metric not in resource.limits:
            error = f"availability metric must name a configured limit: {metric or '<missing>'}"
            resource.record_availability_error("command", error)
            self._log_configuration_error(resource.id, "availability", error)
            return False

        configured_unit = str(config.get("unit") or "")
        limit_unit = str(resource.units.get(metric) or "")
        if configured_unit and limit_unit and configured_unit != limit_unit:
            error = (
                f"availability unit {configured_unit!r} does not match "
                f"configured limit unit {limit_unit!r}"
            )
            resource.record_availability_error("command", error)
            self._log_configuration_error(resource.id, "availability", error)
            return False

        try:
            timeout = self._availability_command_timeout(resource.id, resource.monitoring, config)
            output = await self._run_command(str(config["command"]), timeout)
            value = self._parse_value(output, {})
            resource.record_observed_availability("command", {metric: value})
            return True
        except (SensorCommandError, asyncio.TimeoutError, OSError) as exc:
            self._set_availability_error(resource, str(exc))
        except Exception as exc:
            error = f"unexpected availability monitoring error: {exc}"
            self._set_availability_error(resource, error)
            log("system", "error", f"Resource availability {resource.id} failed unexpectedly: {exc}")
        return False

    async def _refresh_peer_availability(self, resource) -> bool:
        """Refresh one peer-backed resource, or retain estimated behavior for non-peers."""
        if self._peer_client is None or not self._peer_client.is_candidate(resource.provider):
            return True
        config = resource.availability_configuration()
        peer_resource_id = str(config.get("peer_resource_id") or resource.id).strip()
        try:
            timeout_ms = int(config.get("request_timeout_ms", 1500))
        except (TypeError, ValueError):
            timeout_ms = 1500
        result = await self._peer_client.probe_resource(
            resource.provider,
            peer_resource_id,
            max(1, timeout_ms),
        )
        if result is None or result.status == "not_peer":
            return True
        if result.status != "ok" or not isinstance(result.resource, dict):
            self._set_peer_availability_error(resource, result.error or "peer resource probe failed")
            return False

        availability = result.resource.get("availability") or {}
        remote_available = availability.get("available")
        if str(availability.get("status") or "ok") != "ok" or not isinstance(remote_available, dict):
            self._set_peer_availability_error(resource, "peer resource availability is unavailable")
            return False
        remote_units = result.resource.get("units") if isinstance(result.resource.get("units"), dict) else {}
        available: dict[str, float] = {}
        for metric, value in remote_available.items():
            if metric not in resource.limits:
                continue
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                continue
            local_unit = str(resource.units.get(metric) or "")
            remote_unit = str(remote_units.get(metric) or "")
            if local_unit and remote_unit and local_unit != remote_unit:
                self._set_peer_availability_error(
                    resource,
                    f"peer unit {remote_unit!r} does not match configured limit unit {local_unit!r}",
                )
                return False
            available[str(metric)] = numeric_value
        if not available:
            self._set_peer_availability_error(resource, "peer resource has no matching available metrics")
            return False
        resource.record_observed_availability("peer", available)
        resource.record_peer_telemetry(result.resource.get("telemetry") or {})
        return True

    async def refresh_required_availability(self, requirements: dict[str, dict[str, int]]) -> bool:
        """Refresh every command-backed required resource and return whether all probes succeed."""
        for resource_id in requirements:
            if not await self.refresh_availability(resource_id):
                return False
        return True

    async def _poll_sensor(self, resource, sensor: dict) -> None:
        """Execute one sensor command, record its reading, and process its threshold."""
        sensor_id = str(sensor.get("id") or "").strip()
        if not sensor_id:
            self._log_configuration_error(resource.id, "<missing>", "sensor id is required")
            return
        command = sensor.get("command")
        if not isinstance(command, str) or not command.strip():
            self._set_error(resource, sensor_id, "sensor command is required")
            return

        try:
            timeout = self._command_timeout(resource.id, sensor_id, resource.monitoring, sensor)
            output = await self._run_command(command, timeout)
            value = self._parse_value(output, sensor)
            await self._record_value(resource, sensor, value)
        except (SensorCommandError, asyncio.TimeoutError, OSError) as exc:
            self._set_error(resource, sensor_id, str(exc))
        except Exception as exc:
            self._set_error(resource, sensor_id, f"unexpected monitoring error: {exc}")
            log("system", "error", f"Resource sensor {resource.id}/{sensor_id} failed unexpectedly: {exc}")

    async def _record_value(self, resource, sensor: dict, value: float) -> None:
        """Store a sensor reading and invoke configured reactions when it crosses a threshold."""
        sensor_id = str(sensor["id"])
        threshold = sensor.get("threshold")
        alert = self._threshold_exceeded(value, threshold)
        state = {
            "status": "alert" if alert else "ok",
            "value": value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
            "alert": alert,
        }
        resource.set_sensor_state(sensor_id, state)

        reaction_key = (resource.id, sensor_id)
        if not alert:
            self._last_reaction_at.pop(reaction_key, None)
            return

        cooldown = self._reaction_cooldown(threshold)
        now = time.monotonic()
        if now - self._last_reaction_at.get(reaction_key, float("-inf")) < cooldown:
            return
        self._last_reaction_at[reaction_key] = now
        await self._trigger_reactions(resource.id, sensor_id, threshold)

    async def _trigger_reactions(self, resource_id: str, sensor_id: str, threshold: object) -> None:
        """Log threshold notifications and release idle models when configured."""
        reactions = threshold.get("reactions", []) if isinstance(threshold, dict) else []
        if isinstance(reactions, str):
            reactions = [reactions]
        if not isinstance(reactions, list):
            self._log_configuration_error(resource_id, sensor_id, "threshold reactions must be a list")
            return

        for reaction in reactions:
            if reaction == "notify":
                log("system", "warn", f"Resource sensor threshold exceeded: {resource_id}/{sensor_id}")
            elif reaction == "release_resource":
                result = await self._resources.force_release(resource_id)
                if result is None:
                    log("system", "error", f"Cannot release unknown resource {resource_id} after {sensor_id} alert")
                elif result["released"]:
                    models = ", ".join(result["unloaded_models"]) or "no idle models"
                    log("system", "warn", f"Released resource {resource_id} after {sensor_id} alert: {models}")
                else:
                    consumers = ", ".join(result["active_consumers"])
                    log("system", "warn", f"Cannot release {resource_id} after {sensor_id} alert; active tasks: {consumers}")
            else:
                self._log_configuration_error(resource_id, sensor_id, f"unsupported reaction {reaction!r}")

    async def _run_command(self, command: str, timeout: float) -> str:
        """Run one trusted configured shell command and return its standard output."""
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise SensorCommandError(f"command timed out after {timeout:g}s")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise SensorCommandError(f"command exited with {process.returncode}: {detail[:240] or 'no error output'}")
        return stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _parse_value(output: str, sensor: dict) -> float:
        """Extract one finite numeric sensor value from command output."""
        pattern = sensor.get("pattern")
        if pattern is None:
            value_text = output.strip()
        elif isinstance(pattern, str) and pattern:
            match = re.search(pattern, output)
            if match is None:
                raise SensorCommandError("command output does not match sensor pattern")
            group = sensor.get("match_group", 1)
            try:
                value_text = match.group(group)
            except (IndexError, KeyError) as exc:
                raise SensorCommandError(f"sensor pattern group {group!r} is unavailable") from exc
        else:
            raise SensorCommandError("sensor pattern must be a non-empty string")

        try:
            value = float(value_text.strip())
        except (AttributeError, TypeError, ValueError) as exc:
            raise SensorCommandError(f"command output is not one number: {value_text!r}") from exc
        if not math.isfinite(value):
            raise SensorCommandError("command output must be a finite number")
        return value

    def _set_error(self, resource, sensor_id: str, error: str) -> None:
        """Store and log a sensor polling error without hiding the failed reading."""
        resource.set_sensor_state(sensor_id, {
            "status": "error",
            "value": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "error": error,
            "alert": False,
        })
        log("system", "warn", f"Resource sensor {resource.id}/{sensor_id}: {error}")

    @staticmethod
    def _set_availability_error(resource, error: str) -> None:
        """Store and log a failed command availability observation."""
        resource.record_availability_error("command", error)
        log("system", "warn", f"Resource availability {resource.id}: {error}")

    @staticmethod
    def _set_peer_availability_error(resource, error: str) -> None:
        """Store and log a failed peer availability observation."""
        resource.record_availability_error("peer", error)
        log("system", "warn", f"Peer resource availability {resource.id}: {error}")

    def _log_configuration_error(self, resource_id: str, sensor_id: str, message: str) -> None:
        """Log each invalid sensor configuration once to avoid periodic log noise."""
        key = (resource_id, sensor_id, message)
        if key not in self._configuration_errors:
            self._configuration_errors.add(key)
            log("system", "error", f"Resource sensor configuration {resource_id}/{sensor_id}: {message}")

    def _poll_interval(self, resource) -> float | None:
        """Return a valid configured resource polling interval, or disable monitoring."""
        resource_id = resource.id
        monitoring = resource.monitoring
        if not monitoring:
            if resource.has_command_availability() or (
                self._peer_client is not None and self._peer_client.is_candidate(resource.provider)
            ):
                return 10.0
            return None
        try:
            interval = float(monitoring.get("poll_interval", 10))
        except (TypeError, ValueError):
            log("system", "error", f"Resource {resource_id} monitoring poll_interval must be a positive number")
            return None
        if interval <= 0:
            log("system", "error", f"Resource {resource_id} monitoring poll_interval must be positive")
            return None
        return interval

    @staticmethod
    def _command_timeout(resource_id: str, sensor_id: str, monitoring: dict, sensor: dict) -> float:
        """Return the positive command timeout from sensor or resource settings."""
        raw_timeout = sensor.get("command_timeout", monitoring.get("command_timeout", 5))
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise SensorCommandError(
                f"command timeout for {resource_id}/{sensor_id} must be a positive number"
            ) from exc
        if timeout <= 0:
            raise SensorCommandError(f"command timeout for {resource_id}/{sensor_id} must be positive")
        return timeout

    @staticmethod
    def _availability_command_timeout(resource_id: str, monitoring: dict, availability: dict) -> float:
        """Return the positive availability command timeout for one resource."""
        raw_timeout = availability.get("command_timeout", monitoring.get("command_timeout", 5))
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise SensorCommandError(
                f"availability command timeout for {resource_id} must be a positive number"
            ) from exc
        if timeout <= 0:
            raise SensorCommandError(f"availability command timeout for {resource_id} must be positive")
        return timeout

    @staticmethod
    def _threshold_exceeded(value: float, threshold: object) -> bool:
        """Return whether a value crosses its optional above or below threshold."""
        if threshold is None:
            return False
        if not isinstance(threshold, dict):
            raise SensorCommandError("threshold must be an object")
        try:
            limit = float(threshold["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SensorCommandError("threshold value must be a number") from exc
        operator = str(threshold.get("operator") or "above")
        if operator == "above":
            return value > limit
        if operator == "below":
            return value < limit
        raise SensorCommandError("threshold operator must be 'above' or 'below'")

    @staticmethod
    def _reaction_cooldown(threshold: object) -> float:
        """Return the positive reaction cooldown, defaulting to five minutes."""
        if not isinstance(threshold, dict):
            return 300.0
        try:
            cooldown = float(threshold.get("cooldown", 300))
        except (TypeError, ValueError) as exc:
            raise SensorCommandError("threshold cooldown must be a non-negative number") from exc
        if cooldown < 0:
            raise SensorCommandError("threshold cooldown must be a non-negative number")
        return cooldown
