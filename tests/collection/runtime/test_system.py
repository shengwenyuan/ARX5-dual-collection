from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from arx5_collection.collection.runtime.checks import CheckPhase, CheckResult
from arx5_collection.collection.runtime.config import ArmConfig, load_station_config
from arx5_collection.collection.runtime.processes import ProcessExit
from arx5_collection.collection.runtime.system import (
    SystemBringup,
    Usb2CanResolver,
    UsbfsManager,
)


ROOT = Path(__file__).parents[3]


class UsbfsManagerTest(unittest.TestCase):
    def test_applies_checks_and_restores_original_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usbfs_memory_mb"
            path.write_text("16\n")
            manager = UsbfsManager(256, path)
            self.assertTrue(manager.apply().passed)
            self.assertEqual(path.read_text(), "256\n")
            self.assertTrue(manager.restore().passed)
            self.assertEqual(path.read_text(), "16\n")


class Usb2CanResolverTest(unittest.TestCase):
    def test_resolves_exact_serial_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ttyACM0").touch()
            (root / "ttyACM1").touch()

            def runner(argv, **kwargs):
                serial = "LEFT" if argv[-1].endswith("ttyACM0") else "RIGHT"
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    f"ID_SERIAL_SHORT={serial}\n"
                    "ID_VENDOR=ARX\n"
                    "ID_MODEL=USB2CAN\n"
                    "ID_USB_DRIVER=cdc_acm\n",
                    "",
                )

            resolver = Usb2CanResolver(root, runner)
            self.assertEqual(
                [device.serial_number for device in resolver.discover()],
                ["LEFT", "RIGHT"],
            )
            self.assertEqual(resolver.resolve("RIGHT"), root / "ttyACM1")
            with self.assertRaisesRegex(RuntimeError, "resolved to 0"):
                resolver.resolve("MISSING")

    def test_ignores_arx_key_tty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ttyACM0").touch()

            def runner(argv, **kwargs):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "ID_SERIAL_SHORT=KEY\n"
                    "ID_VENDOR=ARX\n"
                    "ID_MODEL=ARX_KEY\n"
                    "ID_USB_DRIVER=cdc_acm\n",
                    "",
                )

            self.assertEqual(Usb2CanResolver(root, runner).discover(), ())


class FakeResolver:
    def resolve(self, serial_number: str) -> Path:
        return Path(f"/dev/{serial_number}")


class FakeInterface:
    def __init__(self, arm: ArmConfig, events: list[str]) -> None:
        self.arm = arm
        self.events = events

    def start(self) -> CheckResult:
        self.events.append(f"start:{self.arm.role}")
        return CheckResult(f"can_{self.arm.role}", CheckPhase.SYSTEM, True, "up")

    def check(self) -> CheckResult:
        return CheckResult(f"can_{self.arm.role}", CheckPhase.SYSTEM, True, "up")

    def stop(self) -> ProcessExit:
        self.events.append(f"stop:{self.arm.role}")
        return ProcessExit(f"slcand-{self.arm.role}", 0, None)


class SystemBringupTest(unittest.TestCase):
    def test_system_lifecycle_keeps_hardware_owned_until_one_shutdown(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            usbfs_path = root / "usbfs_memory_mb"
            usbfs_path.write_text("16\n")
            station = load_station_config(
                ROOT / "config" / "environment" / "station.example.json"
            )
            system = SystemBringup(
                station,
                root / "logs",
                usbfs=UsbfsManager(256, usbfs_path),
                resolver=FakeResolver(),  # type: ignore[arg-type]
                interface_factory=lambda arm, tty, logs: FakeInterface(arm, events),  # type: ignore[arg-type]
            )
            results = system.start()
            self.assertTrue(all(result.passed for result in results))
            self.assertEqual(events, ["start:left", "start:right"])
            self.assertTrue(all(result.passed for result in system.check()))
            system.stop()
            self.assertEqual(
                events,
                ["start:left", "start:right", "stop:right", "stop:left"],
            )
            self.assertEqual(usbfs_path.read_text(), "16\n")


if __name__ == "__main__":
    unittest.main()
