"""Regression tests for configured resource sensor monitoring."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from core.peer_resources import PeerResourceResult
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

    async def test_run_polls_then_stops_cleanly(self) -> None:
        """Run the background loop once and stop it without an attribute error."""
        resources = self._resources({"id": "temperature", "command": "nvidia-smi"})
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(return_value="42")  # type: ignore[method-assign]

        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0)
        monitor.stop()
        await task

        self.assertEqual(monitor._run_command.await_count, 1)

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

    async def test_availability_command_controls_capacity_and_recovers_after_error(self) -> None:
        """Use command-observed capacity, fail closed on error, and recover on a later poll."""
        resources = Resources([{
            "id": "gpu",
            "type": "cuda",
            "limits": {"VRAM": 20},
            "units": {"VRAM": "MiB"},
            "availability": {
                "command": "nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits",
                "metric": "VRAM",
                "unit": "MiB",
            },
            "monitoring": {"poll_interval": 10, "sensors": []},
        }])
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(side_effect=["12", "invalid", "15"])  # type: ignore[method-assign]

        await monitor.poll_resource("gpu")
        resource = resources.get("gpu")
        self.assertTrue(resource.is_available({"VRAM": 12}))
        self.assertFalse(resource.is_available({"VRAM": 13}))
        self.assertEqual(resources.snapshot()[0]["availability"]["available"], {"VRAM": 12.0})
        self.assertNotIn("command", resources.snapshot()[0]["availability"])

        await monitor.poll_resource("gpu")
        self.assertFalse(resource.is_available({"VRAM": 1}))
        self.assertFalse(resource.is_available_after_unload({"VRAM": 1}))
        self.assertEqual(resources.snapshot()[0]["availability"]["status"], "error")

        await monitor.poll_resource("gpu")
        self.assertTrue(resource.is_available({"VRAM": 15}))
        self.assertTrue(resource.is_available_after_unload({"VRAM": 15}))
        self.assertEqual(resources.snapshot()[0]["availability"]["status"], "ok")

    async def test_peer_availability_updates_resource_capacity(self) -> None:
        """Use a confirmed peer reading when no local command overrides the resource."""
        class _PeerClient:
            """Provide a fixed compatible peer response."""

            def is_candidate(self, provider_id: str | None) -> bool:
                """Accept the configured remote provider."""
                return provider_id == "remote"

            async def probe_resource(self, provider_id, resource_id, timeout_ms):
                """Return remote available VRAM for the requested resource."""
                return PeerResourceResult(
                    "ok",
                    resource={
                        "id": resource_id,
                        "limits": {"VRAM": 20},
                        "units": {"VRAM": "MiB"},
                        "availability": {"status": "ok", "available": {"VRAM": 9}},
                        "telemetry": {
                            "sensors": [{
                                "id": "gpu_temperature",
                                "label": "GPU temperature",
                                "unit": "C",
                                "status": "ok",
                                "value": 44,
                                "updated_at": "2026-10-05T10:00:00+00:00",
                                "error": None,
                            }],
                        },
                    },
                )

        resources = Resources([{
            "id": "remote_gpu",
            "type": "cuda",
            "limits": {"VRAM": 20},
            "units": {"VRAM": "MiB"},
            "provider": "remote",
            "availability": {"peer_resource_id": "gpu", "request_timeout_ms": 100},
            "telemetry": {"sensors": ["gpu_temperature"]},
        }])
        monitor = ResourceMonitor(resources, peer_client=_PeerClient())

        self.assertTrue(await monitor.refresh_availability("remote_gpu"))
        resource = resources.get("remote_gpu")
        self.assertTrue(resource.is_available({"VRAM": 9}))
        self.assertFalse(resource.is_available({"VRAM": 10}))
        snapshot = resources.snapshot()[0]["availability"]
        self.assertEqual(snapshot["source"], "peer")
        self.assertEqual(snapshot["available"], {"VRAM": 9.0})
        self.assertEqual(resources.snapshot()[0]["telemetry"]["sensors"][0]["value"], 44)

    async def test_dispatch_refresh_skips_peer_resources(self) -> None:
        """Leave peer admission to the remote queue-state probe before forwarding a task."""
        class _PeerClient:
            """Count resource probes issued by the monitor."""

            def __init__(self) -> None:
                self.calls = 0

            def is_candidate(self, provider_id: str | None) -> bool:
                """Accept the configured remote provider."""
                return provider_id == "remote"

            async def probe_resource(self, provider_id, resource_id, timeout_ms):
                """Fail the probe so an accidental call is visible."""
                self.calls += 1
                return PeerResourceResult("failed", error="peer resource request timed out")

        peer_client = _PeerClient()
        resources = Resources([{
            "id": "remote_gpu",
            "type": "cuda",
            "limits": {"VRAM": 20},
            "units": {"VRAM": "MiB"},
            "provider": "remote",
            "availability": {"peer_resource_id": "gpu", "request_timeout_ms": 100},
        }])
        monitor = ResourceMonitor(resources, peer_client=peer_client)

        self.assertTrue(await monitor.refresh_required_availability({"remote_gpu": {"VRAM": 20}}))
        self.assertEqual(peer_client.calls, 0)

    async def test_peer_error_is_cleared_when_provider_stops_publishing_resources(self) -> None:
        """Do not keep an old peer error after the provider is classified as a non-peer."""
        class _PeerClient:
            """Fail once, then report that the provider has no resource endpoint."""

            def __init__(self) -> None:
                self.calls = 0

            def is_candidate(self, provider_id: str | None) -> bool:
                """Accept the configured remote provider."""
                return provider_id == "remote"

            async def probe_resource(self, provider_id, resource_id, timeout_ms):
                """Return a timeout first and then a non-peer outcome."""
                self.calls += 1
                if self.calls == 1:
                    return PeerResourceResult("failed", error="peer resource request timed out")
                return PeerResourceResult("not_peer", error="peer resource endpoint is unavailable")

        resources = Resources([{
            "id": "remote_gpu",
            "type": "cuda",
            "limits": {"VRAM": 20},
            "units": {"VRAM": "MiB"},
            "provider": "remote",
            "availability": {"peer_resource_id": "gpu", "request_timeout_ms": 100},
        }])
        monitor = ResourceMonitor(resources, peer_client=_PeerClient())

        self.assertFalse(await monitor.refresh_availability("remote_gpu"))
        self.assertEqual(resources.snapshot()[0]["availability"]["status"], "error")

        self.assertTrue(await monitor.refresh_availability("remote_gpu"))
        snapshot = resources.snapshot()[0]["availability"]
        self.assertEqual(snapshot["source"], "estimated")
        self.assertEqual(snapshot["status"], "ok")
        self.assertIsNone(snapshot["error"])

    async def test_multi_gpu_output_is_filtered_to_the_configured_device(self) -> None:
        """Keep only the selected GPU reading from a command that reports every GPU."""
        resources = Resources([{
            "id": "gpu1",
            "type": "cuda",
            "devices": [1],
            "limits": {"VRAM": 2000},
            "units": {"VRAM": "MiB"},
            "availability": {
                "command": "nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits",
                "metric": "VRAM",
                "unit": "MiB",
            },
            "telemetry": {"sensors": ["gpu_temperature"]},
            "monitoring": {
                "poll_interval": 10,
                "sensors": [{
                    "id": "gpu_temperature",
                    "label": "GPU temperature",
                    "unit": "C",
                    "command": "nvidia-smi --query-gpu=index,temperature.gpu --format=csv,noheader,nounits",
                }],
            },
        }])
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(side_effect=["0, 32495\n1, 1980", "0, 31\n1, 40"])  # type: ignore[method-assign]

        await monitor.poll_resource("gpu1")

        snapshot = resources.snapshot()[0]
        self.assertEqual(snapshot["devices"], [1])
        self.assertEqual(snapshot["availability"]["available"], {"VRAM": 1980.0})
        self.assertEqual(snapshot["availability"]["devices"], [
            {"device": 1, "available": {"VRAM": 1980.0}},
        ])
        sensor = snapshot["monitoring"]["sensors"][0]
        self.assertEqual(sensor["value"], 40.0)
        self.assertEqual(sensor["devices"], [
            {"device": 1, "value": 40.0, "status": "ok", "alert": False},
        ])

    async def test_multi_device_resource_aggregates_free_memory_and_alerts_per_device(self) -> None:
        """Sum free capacity on request and keep an alert on the device that crossed the threshold."""
        resources = Resources([{
            "id": "gpu_pool",
            "type": "cuda",
            "devices": [0, 1],
            "limits": {"VRAM": 34000},
            "units": {"VRAM": "MiB"},
            "availability": {
                "command": "nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits",
                "metric": "VRAM",
                "unit": "MiB",
                "aggregate": "sum",
            },
            "monitoring": {
                "poll_interval": 10,
                "sensors": [{
                    "id": "gpu_temperature",
                    "unit": "C",
                    "command": "nvidia-smi --query-gpu=index,temperature.gpu --format=csv,noheader,nounits",
                    "threshold": {"operator": "above", "value": 35, "reactions": ["notify"]},
                }],
            },
        }])
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(side_effect=["0, 32495\n1, 1980", "0, 31\n1, 40"])  # type: ignore[method-assign]

        await monitor.poll_resource("gpu_pool")

        snapshot = resources.snapshot()[0]
        self.assertEqual(snapshot["availability"]["available"], {"VRAM": 34475.0})
        self.assertEqual(snapshot["availability"]["devices"], [
            {"device": 0, "available": {"VRAM": 32495.0}},
            {"device": 1, "available": {"VRAM": 1980.0}},
        ])
        sensor = snapshot["monitoring"]["sensors"][0]
        self.assertEqual(sensor["value"], 40.0)
        self.assertTrue(sensor["alert"])
        self.assertEqual([item["alert"] for item in sensor["devices"]], [False, True])

    async def test_values_of_unselected_devices_are_not_parsed(self) -> None:
        """Ignore a non-numeric reading from another GPU when only one device is selected."""
        resources = Resources([{
            "id": "gpu1",
            "type": "cuda",
            "devices": [1],
            "limits": {"VRAM": 2000},
            "units": {"VRAM": "MiB"},
            "availability": {
                "command": "nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits",
                "metric": "VRAM",
                "unit": "MiB",
            },
            "monitoring": {
                "poll_interval": 10,
                "sensors": [{
                    "id": "gpu_fan_speed",
                    "unit": "%",
                    "command": "nvidia-smi --query-gpu=index,fan.speed --format=csv,noheader,nounits",
                }],
            },
        }])
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(side_effect=["0, [N/A]\n1, 1980", "0, [N/A]\n1, 34"])  # type: ignore[method-assign]

        await monitor.poll_resource("gpu1")

        snapshot = resources.snapshot()[0]
        self.assertEqual(snapshot["availability"]["available"], {"VRAM": 1980.0})
        sensor = snapshot["monitoring"]["sensors"][0]
        self.assertEqual(sensor["status"], "ok")
        self.assertEqual(sensor["value"], 34.0)

    async def test_missing_configured_device_is_a_sensor_error(self) -> None:
        """Fail the probe when the command omits one of the configured devices."""
        resources = Resources([{
            "id": "gpu_pool",
            "type": "cuda",
            "devices": [0, 1],
            "limits": {"VRAM": 34000},
            "units": {"VRAM": "MiB"},
            "availability": {
                "command": "nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits",
                "metric": "VRAM",
                "unit": "MiB",
            },
            "monitoring": {"poll_interval": 10, "sensors": []},
        }])
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(return_value="0, 32495")  # type: ignore[method-assign]

        await monitor.poll_resource("gpu_pool")

        availability = resources.snapshot()[0]["availability"]
        self.assertEqual(availability["status"], "error")
        self.assertIn("no reading for device 1", availability["error"])

    async def test_telemetry_exports_only_whitelisted_sensor_state(self) -> None:
        """Expose selected sensor readings without exposing their executable commands."""
        resources = Resources([{
            "id": "gpu",
            "type": "cuda",
            "limits": {"VRAM": 20},
            "telemetry": {"sensors": ["temperature"]},
            "monitoring": {
                "poll_interval": 10,
                "sensors": [{
                    "id": "temperature",
                    "label": "GPU temperature",
                    "unit": "C",
                    "command": "nvidia-smi",
                }],
            },
        }])
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(return_value="42")  # type: ignore[method-assign]

        await monitor.poll_resource("gpu")

        telemetry = resources.snapshot()[0]["telemetry"]
        self.assertEqual(telemetry["sensors"][0]["value"], 42.0)
        self.assertNotIn("command", telemetry["sensors"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
