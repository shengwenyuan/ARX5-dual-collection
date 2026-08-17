from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path

from arx5_collection.production.events import UnixDatagramEventEmitter


class UnixDatagramEventEmitterTest(unittest.TestCase):
    def test_emits_versioned_json_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.bind(str(path))
                UnixDatagramEventEmitter(path).emit(
                    "episode.state", {"state": "recording"}
                )
                payload = json.loads(receiver.recv(4096))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["type"], "episode.state")
        self.assertEqual(payload["payload"], {"state": "recording"})
        self.assertIn("timestamp", payload)

    def test_delivery_failure_is_non_fatal_and_reported(self) -> None:
        warnings: list[str] = []
        UnixDatagramEventEmitter(
            Path("/tmp/does-not-exist/arx5-events.sock"),
            warning_sink=warnings.append,
        ).emit("session.ready")
        self.assertIn("delivery failed", warnings[0])


if __name__ == "__main__":
    unittest.main()
