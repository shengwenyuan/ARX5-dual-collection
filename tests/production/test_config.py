from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arx5_collection.production.config import (
    EXPECTED_STREAMS,
    load_configured_station,
    load_station_config,
    validate_task_streams,
)


ROOT = Path(__file__).parents[2]


def trigger_payload() -> dict[str, dict[str, object]]:
    return {
        "activate": {
            "vendor_id": "8088",
            "product_id": "0015",
            "serial_number": "pedal-one",
        },
        "abort": {
            "vendor_id": "8088",
            "product_id": "0015",
            "serial_number": "pedal-two",
        },
    }


class ProductionConfigTest(unittest.TestCase):
    def test_example_station_has_fixed_logical_identity(self) -> None:
        station = load_station_config(ROOT / "config" / "station.example.json")
        self.assertEqual(station.station_id, "station-example")
        self.assertEqual(station.ros_domain_id, 31)
        self.assertEqual([arm.role for arm in station.arms], ["left", "right"])
        self.assertEqual(
            [camera.serial_number for camera in station.cameras],
            ["camera-left-serial", "camera-right-serial", "camera-overview-serial"],
        )
        self.assertEqual(station.metadata()["devices"][2]["serial_number"], "camera-left-serial")

    def test_station_rejects_duplicate_identity(self) -> None:
        payload = json.loads((ROOT / "config" / "station.example.json").read_text())
        payload["cameras"]["right"] = payload["cameras"]["left"]
        with self.assertRaisesRegex(ValueError, "serial numbers must be unique"):
            load_station_config(self.write_json(payload))

    def test_station_v2_loads_two_distinct_pedal_bindings(self) -> None:
        payload = json.loads((ROOT / "config" / "station.example.json").read_text())
        payload["schema_version"] = 2
        payload.pop("ros_domain_id")
        payload["triggers"] = trigger_payload()
        station = load_station_config(self.write_json(payload))
        assert station.triggers is not None
        self.assertEqual(station.triggers.activate.serial_number, "pedal-one")
        self.assertEqual(station.triggers.abort.serial_number, "pedal-two")

    def test_station_v2_rejects_same_pedal_for_both_roles(self) -> None:
        payload = json.loads((ROOT / "config" / "station.example.json").read_text())
        payload["schema_version"] = 2
        payload.pop("ros_domain_id")
        payload["triggers"] = trigger_payload()
        payload["triggers"]["abort"]["serial_number"] = "pedal-one"
        with self.assertRaisesRegex(ValueError, "different serial numbers"):
            load_station_config(self.write_json(payload))

    def test_station_v2_rejects_obsolete_event_code(self) -> None:
        payload = json.loads((ROOT / "config" / "station.example.json").read_text())
        payload["schema_version"] = 2
        payload.pop("ros_domain_id")
        payload["triggers"] = trigger_payload()
        payload["triggers"]["activate"]["event_code"] = 57
        with self.assertRaisesRegex(ValueError, "keys must be exactly"):
            load_station_config(self.write_json(payload))

    def test_station_v3_rejects_invalid_ros_domain_id(self) -> None:
        payload = json.loads((ROOT / "config" / "station.example.json").read_text())
        for value in (-1, 233, "31", True):
            with self.subTest(value=value):
                payload["ros_domain_id"] = value
                with self.assertRaisesRegex(ValueError, "ros_domain_id"):
                    load_station_config(self.write_json(payload))

    def test_production_rejects_legacy_station_without_ros_domain_id(self) -> None:
        payload = json.loads((ROOT / "config" / "station.example.json").read_text())
        payload["schema_version"] = 2
        payload.pop("ros_domain_id")
        with self.assertRaisesRegex(ValueError, "station set-ros-domain-id"):
            load_configured_station(self.write_json(payload))

    def test_production_task_is_exactly_eight_required_streams(self) -> None:
        path = ROOT / "config" / "task.eight-stream.json"
        validate_task_streams(path)
        payload = json.loads(path.read_text())
        self.assertEqual(
            {stream["id"]: stream["topic"] for stream in payload["streams"]},
            EXPECTED_STREAMS,
        )

    def test_task_rejects_missing_or_optional_stream(self) -> None:
        payload = json.loads(
            (ROOT / "config" / "task.eight-stream.json").read_text()
        )
        payload["streams"].pop()
        with self.assertRaisesRegex(ValueError, "fixed eight-stream"):
            validate_task_streams(self.write_json(payload))

        payload = json.loads(
            (ROOT / "config" / "task.eight-stream.json").read_text()
        )
        payload["streams"][0]["required"] = False
        with self.assertRaisesRegex(ValueError, "must be required"):
            validate_task_streams(self.write_json(payload))

    def write_json(self, payload: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        path.write_text(json.dumps(payload))
        return path


if __name__ == "__main__":
    unittest.main()
