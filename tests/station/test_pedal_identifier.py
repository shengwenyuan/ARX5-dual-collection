from __future__ import annotations

import unittest
from pathlib import Path

from arx5_collection.episode.adapters.pedal import PRESS_REPORT, HidrawPedalIdentity
from arx5_collection.station.pedal_identifier import (
    PedalIdentificationError,
    PedalIdentifier,
)


class PedalIdentifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = (
            HidrawPedalIdentity(Path("/dev/hidraw4"), "8088", "0015", "one"),
            HidrawPedalIdentity(Path("/dev/hidraw2"), "8088", "0015", "two"),
        )
        self.next_fd = 20
        self.readable: list[int] = []
        self.reports: dict[int, bytes] = {}
        self.closed: list[int] = []

    def open(self, path, flags):
        self.next_fd += 1
        return self.next_fd

    def select(self, read, write, errors, timeout):
        return tuple(self.readable), (), ()

    def read(self, file_descriptor, size):
        return self.reports[file_descriptor]

    def identifier(self) -> PedalIdentifier:
        return PedalIdentifier(
            self.candidates,
            open_function=self.open,
            close_function=self.closed.append,
            read_function=self.read,
            select_function=self.select,
        )

    def test_binds_activate_then_abort_by_press_order(self) -> None:
        with self.identifier() as identifier:
            self.readable = [21]
            self.reports[21] = PRESS_REPORT
            activate = identifier.wait_for_press(1.0)
            self.readable = [22]
            self.reports[22] = PRESS_REPORT
            abort = identifier.wait_for_press(1.0, frozenset({activate.serial_number}))

        self.assertEqual(activate.serial_number, "one")
        self.assertEqual(abort.serial_number, "two")
        self.assertEqual(self.closed, [21, 22])

    def test_rejects_reusing_first_pedal(self) -> None:
        with self.identifier() as identifier:
            self.readable = [21]
            self.reports[21] = PRESS_REPORT
            with self.assertRaisesRegex(PedalIdentificationError, "exactly one"):
                identifier.wait_for_press(1.0, frozenset({"one"}))

    def test_rejects_unknown_report(self) -> None:
        with self.identifier() as identifier:
            self.readable = [22]
            self.reports[22] = bytes(64)
            with self.assertRaisesRegex(PedalIdentificationError, "exactly one"):
                identifier.wait_for_press(1.0)

    def test_partial_open_failure_closes_first_candidate(self) -> None:
        calls = 0

        def failing_open(path, flags):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("denied")
            return 31

        identifier = PedalIdentifier(
            self.candidates,
            open_function=failing_open,
            close_function=self.closed.append,
        )
        with self.assertRaisesRegex(RuntimeError, "cannot open"):
            identifier.__enter__()
        self.assertEqual(self.closed, [31])


if __name__ == "__main__":
    unittest.main()
