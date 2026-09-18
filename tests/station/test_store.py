from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arx5_collection.production.config import (
    ArmConfig,
    CameraConfig,
    PedalConfig,
    StationConfig,
    TriggerConfig,
    load_station_config,
)
from arx5_collection.station.store import StationConfigStore


def station() -> StationConfig:
    return StationConfig(
        schema_version=5,
        station_id="station-a",
        ros_domain_id=31,
        sdk_type=2,
        arms=(
            ArmConfig("left", "arm-left", "can1"),
            ArmConfig("right", "arm-right", "can3"),
        ),
        cameras=(
            CameraConfig("left", "camera-left"),
            CameraConfig("right", "camera-right"),
            CameraConfig("overview", "camera-overview"),
        ),
        triggers=TriggerConfig(
            PedalConfig("activate", "8088", "0015", "pedal-one"),
            PedalConfig("abort", "8088", "0015", "pedal-two"),
        ),
    )


class StationConfigStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state" / "station.json"

    def test_writes_schema_v5_that_production_loader_reads(self) -> None:
        StationConfigStore(self.path).commit(station())

        loaded = load_station_config(self.path)
        self.assertEqual(loaded, station())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_failed_validation_preserves_existing_bytes(self) -> None:
        self.path.parent.mkdir()
        original = b'{"existing": true}\n'
        self.path.write_bytes(original)
        broken = station()

        with patch(
            "arx5_collection.station.store.load_station_config",
            side_effect=ValueError("invalid"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid"):
                StationConfigStore(self.path).commit(broken)

        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_serialized_payload_contains_no_runtime_paths(self) -> None:
        StationConfigStore(self.path).commit(station())
        payload = json.loads(self.path.read_text())
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "station_id",
                "ros_domain_id",
                "sdk_type",
                "arms",
                "cameras",
                "triggers",
            },
        )
        self.assertNotIn("hidraw", self.path.read_text())

    def test_set_ros_domain_id_atomically_upgrades_schema_v2(self) -> None:
        legacy = station()
        legacy = StationConfig(
            schema_version=2,
            station_id=legacy.station_id,
            ros_domain_id=None,
            sdk_type=legacy.sdk_type,
            arms=legacy.arms,
            cameras=legacy.cameras,
            triggers=legacy.triggers,
        )
        StationConfigStore(self.path).commit(legacy)

        updated = StationConfigStore(self.path).set_ros_domain_id(42)

        self.assertEqual(updated.schema_version, 5)
        self.assertEqual(updated.ros_domain_id, 42)
        self.assertEqual(load_station_config(self.path), updated)

    def test_saving_deployed_v4_drops_routes_and_preserves_devices(self) -> None:
        StationConfigStore(self.path).commit(station())
        payload = json.loads(self.path.read_text())
        payload["schema_version"] = 4
        payload["task_upload_routes"] = {"legacy task": "legacy_route"}
        self.path.write_text(json.dumps(payload))
        legacy = load_station_config(self.path)

        updated = StationConfigStore(self.path).set_ros_domain_id(42)

        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["schema_version"], 5)
        self.assertNotIn("task_upload_routes", saved)
        self.assertEqual(updated.arms, legacy.arms)
        self.assertEqual(updated.cameras, legacy.cameras)
        self.assertEqual(updated.triggers, legacy.triggers)
        self.assertEqual(updated.station_id, legacy.station_id)
        self.assertEqual(load_station_config(self.path), updated)


if __name__ == "__main__":
    unittest.main()
