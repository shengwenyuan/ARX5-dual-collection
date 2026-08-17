from __future__ import annotations

import unittest
from pathlib import Path

from arx5_collection.production.config import load_station_config
from arx5_collection.production.devices import DeviceIdentityVerifier


ROOT = Path(__file__).parents[2]


def inventory(camera_usb_type: str = "3.2") -> dict:
    station = load_station_config(ROOT / "config" / "station.example.json")
    return {
        "arx_usb": [
            {"serial": arm.usb_serial, "node": f"usb-{arm.role}", "speed_mbps": "12"}
            for arm in station.arms
        ],
        "realsense": {
            "available": True,
            "devices": [
                {"serial": camera.serial_number, "usb_type": camera_usb_type}
                for camera in station.cameras
            ],
        },
        "pedals": [
            {
                "vendor_id": pedal.vendor_id,
                "product_id": pedal.product_id,
                "serial_number": pedal.serial_number,
                "path": f"/dev/{pedal.role}",
            }
            for pedal in (
                station.triggers.activate,
                station.triggers.abort,
            )
        ],
    }


class DeviceIdentityVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.station = load_station_config(ROOT / "config" / "station.example.json")

    def test_one_probe_verifies_all_seven_devices(self) -> None:
        verifier = DeviceIdentityVerifier(self.station, inventory)
        identities = verifier.inspect()
        self.assertEqual(len(identities), 7)
        self.assertTrue(all(identity.matched for identity in identities))
        self.assertEqual(identities[2].id, "camera_left")

    def test_usb2_camera_fails_the_same_check_protocol(self) -> None:
        verifier = DeviceIdentityVerifier(
            self.station, lambda: inventory(camera_usb_type="2.1")
        )
        checks = verifier.checks()
        self.assertTrue(all(check.passed for check in checks[:2]))
        self.assertTrue(all(not check.passed for check in checks[2:5]))
        self.assertTrue(all(check.passed for check in checks[5:]))
        self.assertIn("not on USB3", checks[2].detail)

    def test_missing_arx_serial_is_not_reassigned_by_order(self) -> None:
        payload = inventory()
        payload["arx_usb"].pop(0)
        identities = DeviceIdentityVerifier(self.station, lambda: payload).inspect()
        self.assertFalse(identities[0].matched)
        self.assertTrue(identities[1].matched)


if __name__ == "__main__":
    unittest.main()
