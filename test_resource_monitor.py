"""Regression tests for configured resource sensor monitoring."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from core.resource_monitor import ResourceMonitor
from core.resources import Resources


class TestResourceMonitor(unittest.IsolatedAsyncioTestCase):
    """Validate command parsing, sensor snapshots, and threshold reactions."""

    def _resources(self, sensor: dict) -> Resources:
        """Create one monitored test resource containing the supplied sensor."""
        return Resources([{
            "id": "gpu",
            "type": "cuda",
            "limits": {"VRAM": 20},
            "monitoring": {
                "poll_interval": 10,
                "sensors": [sensor],
            },
        }])

    async def test_poll_records_regex_matched_alert_without_exposing_command(self) -> None:
        """Extract a regex capture, retain the alert state, and hide executable config from snapshots."""
        resources = self._resources({
            "id": "temperature",
            "label": "GPU temperature",
            "unit": "C",
            "command": "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits",
            "pattern": r"temperature=(\d+(?:\.\d+)?)",
            "threshold": {"operator": "above", "value": 80, "reactions": ["notify"]},
        })
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(return_value="temperature=82.5\n")  # type: ignore[method-assign]

        await monitor.poll_resource("gpu")

        sensor = resources.snapshot()[0]["monitoring"]["sensors"][0]
        self.assertEqual(sensor["status"], "alert")
        self.assertEqual(sensor["value"], 82.5)
        self.assertTrue(sensor["alert"])
        self.assertNotIn("command", sensor)

    async def test_release_reaction_runs_once_until_sensor_recovers(self) -> None:
        """Release idle models once per alert period and re-arm after a normal reading."""
        resources = self._resources({
            "id": "free_vram",
            "command": "nvidia-smi",
            "threshold": {
                "operator": "below",
                "value": 1000,
                "reactions": ["release_resource"],
                "cooldown": 300,
            },
        })
        resources.force_release = AsyncMock(return_value={
            "released": True,
            "active_consumers": [],
            "unloaded_models": ["model-a"],
        })  # type: ignore[method-assign]
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(side_effect=["900", "800", "1200", "700"])  # type: ignore[method-assign]

        await monitor.poll_resource("gpu")
        await monitor.poll_resource("gpu")
        await monitor.poll_resource("gpu")
        await monitor.poll_resource("gpu")

        self.assertEqual(resources.force_release.await_count, 2)
        sensor = resources.snapshot()[0]["monitoring"]["sensors"][0]
        self.assertEqual(sensor["status"], "alert")
        self.assertEqual(sensor["value"], 700.0)

    async def test_invalid_command_output_is_exposed_as_sensor_error(self) -> None:
        """Expose invalid numeric output instead of treating it as a successful zero reading."""
        resources = self._resources({"id": "fan", "command": "sensors"})
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(return_value="fan speed unavailable")  # type: ignore[method-assign]

        await monitor.poll_resource("gpu")

        sensor = resources.snapshot()[0]["monitoring"]["sensors"][0]
        self.assertEqual(sensor["status"], "error")
        self.assertIsNone(sensor["value"])
        self.assertIn("not one number", sensor["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
