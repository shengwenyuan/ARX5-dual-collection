from __future__ import annotations

import io
import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from arx5_collection.control.manager import (
    CollectorControlConfig,
    CollectorManager,
    ControlConflict,
    ControlStatus,
    discover_episodes,
)
from arx5_collection.episode.ports import TriggerEvent


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 424242
        self.stdout = io.StringIO("collector output\n")
        self.stderr = io.StringIO("")
        self.return_code: int | None = None
        self.stopped = threading.Event()

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        if not self.stopped.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        assert self.return_code is not None
        return self.return_code

    def finish(self, return_code: int = 0) -> None:
        self.return_code = return_code
        self.stopped.set()


class CollectorManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.task = self.root / "task.json"
        self.task.write_text(
            json.dumps({"task_id": "fold", "task_description": "Fold shirt"})
        )
        self.station = self.root / "station.json"
        self.station.write_text("{}")
        self.process = FakeProcess()
        self.argv: list[str] = []
        self.signals: list[tuple[int, int]] = []

        def launch(argv):
            self.argv = list(argv)
            return self.process

        self.manager = CollectorManager(
            CollectorControlConfig(
                runtime_dir=self.root / "runtime",
                station_config=self.station,
                task_config=self.task,
                output_root=self.root / "episodes",
                session_log_root=self.root / "logs",
            ),
            popen_factory=launch,
            command_runner=lambda argv: subprocess.CompletedProcess(
                argv, 0, '[{"id":"arm_left","matched":true}]', ""
            ),
            signal_group=lambda pid, signal_number: self.signals.append(
                (pid, signal_number)
            ),
        )

    def tearDown(self) -> None:
        if self.process.poll() is None:
            self.process.finish()
        self.manager.close()
        self.temporary_directory.cleanup()

    def test_devices_and_session_use_only_fixed_argv(self) -> None:
        devices = self.manager.inspect_devices()
        self.assertTrue(devices[0]["matched"])
        self.manager.start_session()
        self.assertEqual(self.manager.status, ControlStatus.STARTING)
        self.assertEqual(self.argv[0:2], ["arx5-collect", "run"])
        self.assertIn(str(self.task), self.argv)
        with self.assertRaises(ControlConflict):
            self.manager.start_session()

    def test_structured_events_drive_state_and_remote_trigger(self) -> None:
        self.manager.start_session()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(str(self.manager.trigger_socket))
            self.manager.handle_event(self.event("session.ready", {}))
            self.manager.trigger(TriggerEvent.ACTIVATE)
            payload = json.loads(receiver.recv(4096))
        self.assertEqual(payload["event"], "activate")
        self.manager.handle_event(
            self.event("episode.state", {"state": "recording"})
        )
        self.assertEqual(self.manager.status, ControlStatus.RECORDING)
        self.manager.handle_event(
            self.event("episode.state", {"state": "ready"})
        )
        self.manager.stop_session()
        self.assertEqual(self.manager.status, ControlStatus.SHUTTING_DOWN)
        self.assertEqual(self.signals[-1][0], self.process.pid)

    def test_unexpected_nonzero_exit_is_not_masked_by_stopped_event(self) -> None:
        self.manager.start_session()
        self.manager.handle_event(self.event("session.stopped", {}))
        self.process.finish(2)

        deadline = time.monotonic() + 1.0
        while self.manager.status is not ControlStatus.ERROR and time.monotonic() < deadline:
            time.sleep(0.01)

        snapshot = self.manager.snapshot()
        self.assertEqual(snapshot["status"], "ERROR")
        self.assertEqual(snapshot["error"], "collector exited with 2")

    @staticmethod
    def event(event_type: str, payload: dict) -> dict:
        return {
            "schema_version": 1,
            "type": event_type,
            "timestamp": "2026-08-18T00:00:00Z",
            "payload": payload,
        }


class EpisodeDiscoveryTest(unittest.TestCase):
    def test_only_committed_metadata_is_listed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            episode = root / "episode-1"
            episode.mkdir()
            (episode / "episode.mcap").write_bytes(b"1234")
            (episode / "metadata.json").write_text(
                json.dumps(
                    {
                        "episode_id": "episode-1",
                        "outcome": "success",
                        "timing": {
                            "started_at": "2026-08-18T00:00:00Z",
                            "duration_s": 3.0,
                        },
                        "streams": [],
                        "errors": [],
                    }
                )
            )
            partial = root / ".partial.partial"
            partial.mkdir()
            (partial / "metadata.json").write_text("{}")
            episodes = discover_episodes(root)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["size_bytes"], 4)


if __name__ == "__main__":
    unittest.main()
