from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path

from arx5_collection.episode.adapters.pedal import (
    PRESS_REPORT,
    HidrawPedal,
    HidrawPedalIdentity,
    PedalDeviceResolver,
    PedalTrigger,
    PedalUnavailable,
    discover_hidraw_pedals,
)
from arx5_collection.episode.ports import RecordTrigger, TriggerEvent
from arx5_collection.production.config import PedalConfig


VENDOR_DESCRIPTOR = bytes.fromhex(
    "0600ff0900a1010900150026ff0075089540810609009106c0"
)
KEYBOARD_DESCRIPTOR = bytes.fromhex("05010906a101")


def pedal(role: str, serial: str) -> PedalConfig:
    return PedalConfig(role, "8088", "0015", serial)


class PedalDeviceResolverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.sysfs = self.root / "sys"
        self.devices = self.root / "dev"
        self.opened: list[Path] = []
        self.closed: list[int] = []

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def add_hidraw(
        self,
        name: str,
        serial: str,
        descriptor: bytes = VENDOR_DESCRIPTOR,
    ) -> None:
        device = self.sysfs / name / "device"
        device.mkdir(parents=True)
        (device / "uevent").write_text(
            "HID_ID=0003:00008088:00000015\n"
            f"HID_UNIQ={serial}\n"
        )
        (device / "report_descriptor").write_bytes(descriptor)

    def open(self, path: str | Path, flags: int) -> int:
        resolved = Path(path)
        self.opened.append(resolved)
        if resolved.name == "hidraw-fail":
            raise OSError(errno.EACCES, "denied")
        return len(self.opened) + 20

    def resolver(self) -> PedalDeviceResolver:
        return PedalDeviceResolver(
            sysfs_root=self.sysfs,
            device_root=self.devices,
            open_function=self.open,
            close_function=self.closed.append,
        )

    def test_selects_one_vendor_interface_per_stable_serial(self) -> None:
        self.add_hidraw("hidraw-a", "one")
        self.add_hidraw("hidraw-a-keyboard", "one", KEYBOARD_DESCRIPTOR)
        self.add_hidraw("hidraw-b", "two")

        result = self.resolver().resolve(
            pedal("activate", "one"), pedal("abort", "two")
        )

        self.assertEqual(result[TriggerEvent.ACTIVATE].path.name, "hidraw-a")
        self.assertEqual(result[TriggerEvent.ABORT].path.name, "hidraw-b")

    def test_inventory_exposes_stable_identity_not_runtime_event_code(self) -> None:
        self.add_hidraw("hidraw-a", "one")
        self.assertEqual(
            discover_hidraw_pedals(self.sysfs, self.devices),
            (HidrawPedalIdentity(self.devices / "hidraw-a", "8088", "0015", "one"),),
        )

    def test_missing_member_is_unavailable(self) -> None:
        self.add_hidraw("hidraw-a", "one")
        with self.assertRaisesRegex(PedalUnavailable, "trigger abort"):
            self.resolver().resolve(
                pedal("activate", "one"), pedal("abort", "two")
            )
        self.assertEqual(self.opened, [])

    def test_second_open_failure_closes_first(self) -> None:
        self.add_hidraw("hidraw-a", "one")
        self.add_hidraw("hidraw-fail", "two")
        with self.assertRaisesRegex(PedalUnavailable, "cannot open"):
            self.resolver().resolve(
                pedal("activate", "one"), pedal("abort", "two")
            )
        self.assertEqual(self.closed, [21])


class PedalTriggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.readable: list[int] = []
        self.reports: dict[int, bytes | OSError] = {}
        self.closed: list[int] = []
        self.now = 1.0
        self.trigger = PedalTrigger(
            {
                TriggerEvent.ACTIVATE: HidrawPedal(Path("activate"), 21),
                TriggerEvent.ABORT: HidrawPedal(Path("abort"), 23),
            },
            select_function=self.select,
            read_function=self.read,
            close_function=self.closed.append,
            monotonic_clock=lambda: self.now,
        )

    def select(self, read, write, errors, timeout):
        return tuple(self.readable), (), ()

    def read(self, file_descriptor: int, size: int) -> bytes:
        report = self.reports[file_descriptor]
        if isinstance(report, OSError):
            raise report
        return report

    def test_is_record_trigger_and_closes_both_fds(self) -> None:
        with self.trigger as trigger:
            self.assertIsInstance(trigger, RecordTrigger)
        self.assertEqual(self.closed, [21, 23])

    def test_fixed_reports_map_to_activate_and_abort(self) -> None:
        with self.trigger:
            self.readable = [21]
            self.reports[21] = PRESS_REPORT
            self.assertIs(self.trigger.wait(0.1), TriggerEvent.ACTIVATE)

            self.readable = [23]
            self.reports[23] = PRESS_REPORT
            self.assertIs(self.trigger.wait(0.1), TriggerEvent.ABORT)

    def test_abort_wins_when_both_are_ready(self) -> None:
        with self.trigger:
            self.readable = [21, 23]
            self.reports = {21: PRESS_REPORT, 23: PRESS_REPORT}
            self.assertIs(self.trigger.wait(0.1), TriggerEvent.ABORT)

    def test_ignores_other_reports_and_debounces(self) -> None:
        with self.trigger:
            self.readable = [21]
            self.reports[21] = bytes(64)
            self.assertIsNone(self.trigger.wait(0.1))

            self.reports[21] = PRESS_REPORT
            self.assertIs(self.trigger.wait(0.1), TriggerEvent.ACTIVATE)
            self.now = 1.1
            self.assertIsNone(self.trigger.wait(0.1))

    def test_disconnect_is_a_runtime_error(self) -> None:
        with self.trigger:
            self.readable = [23]
            self.reports[23] = OSError(errno.ENODEV, "gone")
            with self.assertRaisesRegex(RuntimeError, "disconnected or unreadable"):
                self.trigger.wait(0.1)

    def test_empty_read_is_a_disconnect(self) -> None:
        with self.trigger:
            self.readable = [23]
            self.reports[23] = b""
            with self.assertRaisesRegex(RuntimeError, "pedal disconnected"):
                self.trigger.wait(0.1)


if __name__ == "__main__":
    unittest.main()
