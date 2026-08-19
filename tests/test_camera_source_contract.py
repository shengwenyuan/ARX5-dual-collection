from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = (
    Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "arx5_camera_source"
)
sys.path.insert(0, str(PACKAGE_ROOT))

from arx5_camera_source.camera_config import load_station_cameras  # noqa: E402
from arx5_camera_source.image_contract import (  # noqa: E402
    color_contract,
    timestamp_parts,
    validate_image_buffer,
)


class CameraConfigTest(unittest.TestCase):
    def write_config(self, cameras: object) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "station.json"
        path.write_text(json.dumps({"schema_version": 1, "cameras": cameras}))
        return path

    def test_loads_fixed_roles_in_logical_order(self) -> None:
        path = self.write_config(
            {
                "overview": {"serial_number": "300"},
                "right": "200",
                "left": {"serial_number": "100"},
            }
        )
        specs = load_station_cameras(path)
        self.assertEqual([spec.role for spec in specs], ["left", "right", "overview"])
        self.assertEqual([spec.serial for spec in specs], ["100", "200", "300"])
        self.assertEqual(specs[0].namespace, "/sensors/camera_left")

    def test_rejects_missing_or_unresolved_role(self) -> None:
        with self.assertRaises(ValueError):
            load_station_cameras(self.write_config({"left": "100", "right": "200"}))
        with self.assertRaises(ValueError):
            load_station_cameras(
                self.write_config({"left": "100", "right": "200", "overview": None})
            )

    def test_rejects_duplicate_serial(self) -> None:
        path = self.write_config(
            {"left": "100", "right": "100", "overview": "300"}
        )
        with self.assertRaises(ValueError):
            load_station_cameras(path)


class ImageContractTest(unittest.TestCase):
    def test_color_contracts(self) -> None:
        self.assertEqual(color_contract("YUYV").ros_encoding, "yuv422_yuy2")
        self.assertEqual(color_contract("rgb8").bytes_per_pixel, 3)
        with self.assertRaises(ValueError):
            color_contract("bgr8")

    def test_timestamp_conversion(self) -> None:
        self.assertEqual(timestamp_parts(1_234.56789), (1, 234_567_890))
        with self.assertRaises(ValueError):
            timestamp_parts(-1.0)

    def test_image_buffer_contract(self) -> None:
        validate_image_buffer(848, 480, 1696, 814_080, 2)
        with self.assertRaises(ValueError):
            validate_image_buffer(848, 480, 1000, 480_000, 2)
        with self.assertRaises(ValueError):
            validate_image_buffer(848, 480, 1696, 10, 2)


if __name__ == "__main__":
    unittest.main()
