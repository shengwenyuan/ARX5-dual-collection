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
        schema_version=2,
        station_id="station-a",
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

    def test_writes_schema_v2_that_production_loader_reads(self) -> None:
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
        self.assertEqual(set(payload), {
            "schema_version", "station_id", "sdk_type", "arms", "cameras", "triggers"
        })
        self.assertNotIn("hidraw", self.path.read_text())


if __name__ == "__main__":
    unittest.main()
