from __future__ import annotations

import unittest

from arx5_collection.collection.station.camera_identifier import (
    CameraIdentificationError,
    CameraIdentifier,
)
from arx5_collection.collection.station.inventory import D405Device


class FakeValidator:
    def __init__(self) -> None:
        self.validated: list[str] = []

    def validate(self, serial_number: str) -> None:
        self.validated.append(serial_number)


class CameraIdentifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = FakeValidator()
        self.identifier = CameraIdentifier(
            (
                D405Device("one", "Intel RealSense D405", "1", "3.2"),
                D405Device("two", "Intel RealSense D405", "1", "3.2"),
                D405Device("three", "Intel RealSense D405", "1", "3.2"),
                D405Device("usb2", "Intel RealSense D405", "1", "2.1"),
            ),
            self.validator,  # type: ignore[arg-type]
        )

    def test_manual_serial_must_be_current_unique_and_really_stream(self) -> None:
        camera = self.identifier.bind("left", "one")
        self.assertEqual(camera.serial_number, "one")
        self.assertEqual(self.validator.validated, ["one"])
        with self.assertRaisesRegex(CameraIdentificationError, "already bound"):
            self.identifier.bind("overview", "one")
        with self.assertRaisesRegex(CameraIdentificationError, "not on"):
            self.identifier.bind("overview", "sticker-only")

    def test_usb2_is_rejected_before_open(self) -> None:
        with self.assertRaisesRegex(CameraIdentificationError, "requires USB3"):
            self.identifier.bind("right", "usb2")
        self.assertEqual(self.validator.validated, [])


if __name__ == "__main__":
    unittest.main()
