from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arx5_collection.episode.adapters.pedal import HidrawPedalIdentity
from arx5_collection.production.config import ArmConfig, CameraConfig, PedalConfig, TriggerConfig
from arx5_collection.production.system import Usb2CanDevice
from arx5_collection.station.inventory import D405Device, StationInventory
from arx5_collection.station.service import StationInitializationService
from arx5_collection.station.store import StationConfigStore


class FakeInventoryProvider:
    def __init__(self, inventory: StationInventory) -> None:
        self.inventory = inventory

    def collect(self) -> StationInventory:
        return self.inventory


class FakeArmIdentifier:
    def identify(self, candidates, prompt):
        prompt()
        return (
            ArmConfig("left", "arm-one", "can1"),
            ArmConfig("right", "arm-two", "can3"),
        )


class FakeCameraIdentifier:
    def __init__(self, candidates) -> None:
        self.candidates = candidates

    def bind(self, role, serial):
        return CameraConfig(role, serial)


class FakePedalIdentifier:
    def __init__(self, candidates) -> None:
        self.candidates = candidates

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def identify(self, prompt):
        prompt("activate")
        prompt("abort")
        return TriggerConfig(
            PedalConfig("activate", "8088", "0015", "pedal-one"),
            PedalConfig("abort", "8088", "0015", "pedal-two"),
        )


class FakeInteraction:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.camera_serials = iter(("camera-left", "camera-overview", "camera-right"))

    def choose_station_id(self, default):
        return "new-station"

    def prompt_left_arm_movement(self):
        self.events.append("move-left")

    def choose_camera(self, role, candidates, used_serials):
        self.events.append(f"camera:{role}")
        return next(self.camera_serials)

    def prompt_pedal(self, role):
        self.events.append(f"pedal:{role}")

    def report(self, message):
        pass


def inventory() -> StationInventory:
    return StationInventory(
        usb2can=(
            Usb2CanDevice(Path("/dev/a"), "arm-one", None, None),
            Usb2CanDevice(Path("/dev/b"), "arm-two", None, None),
        ),
        cameras=tuple(
            D405Device(serial, "Intel RealSense D405", "1", "3.2")
            for serial in ("camera-left", "camera-overview", "camera-right")
        ),
        pedals=(
            HidrawPedalIdentity(Path("/dev/h1"), "8088", "0015", "pedal-one"),
            HidrawPedalIdentity(Path("/dev/h2"), "8088", "0015", "pedal-two"),
        ),
    )


class StationInitializationServiceTest(unittest.TestCase):
    def test_full_transaction_commits_one_production_config(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "station.json"
        interaction = FakeInteraction()
        service = StationInitializationService(
            StationConfigStore(path),
            Path(temporary.name) / "logs",
            inventory_provider=FakeInventoryProvider(inventory()),  # type: ignore[arg-type]
            arm_identifier_factory=lambda logs: FakeArmIdentifier(),  # type: ignore[arg-type]
            camera_identifier_factory=FakeCameraIdentifier,  # type: ignore[arg-type]
            pedal_identifier_factory=FakePedalIdentifier,
        )

        result = service.configure(interaction)  # type: ignore[arg-type]

        self.assertEqual(result.station_id, "new-station")
        self.assertTrue(path.exists())
        self.assertEqual(
            interaction.events,
            [
                "move-left",
                "camera:left",
                "camera:overview",
                "camera:right",
                "pedal:activate",
                "pedal:abort",
            ],
        )


if __name__ == "__main__":
    unittest.main()
