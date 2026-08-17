from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path

from arx5_collection.episode.adapters.composite import CompositeTrigger
from arx5_collection.episode.adapters.remote import UnixSocketTrigger, send_trigger
from arx5_collection.episode.ports import TriggerEvent


class StubTrigger:
    def __init__(self, events: list[TriggerEvent | None]) -> None:
        self.events = events

    def wait(self, timeout_s: float) -> TriggerEvent | None:
        return self.events.pop(0) if self.events else None


class RemoteTriggerTest(unittest.TestCase):
    def test_unix_socket_receives_only_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trigger.sock"
            with UnixSocketTrigger(path) as trigger:
                send_trigger(path, TriggerEvent.ACTIVATE)
                self.assertIs(trigger.wait(0.1), TriggerEvent.ACTIVATE)
                self.assertIsNone(trigger.wait(0.0))
            self.assertFalse(path.exists())

    def test_invalid_envelope_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trigger.sock"
            with UnixSocketTrigger(path) as trigger:
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                    client.sendto(json.dumps({"event": "activate"}).encode(), str(path))
                with self.assertRaisesRegex(RuntimeError, "envelope"):
                    trigger.wait(0.1)

    def test_composite_trigger_gives_abort_priority(self) -> None:
        trigger = CompositeTrigger(
            (
                StubTrigger([TriggerEvent.ACTIVATE]),
                StubTrigger([TriggerEvent.ABORT]),
            )
        )
        self.assertIs(trigger.wait(0.0), TriggerEvent.ABORT)


if __name__ == "__main__":
    unittest.main()
