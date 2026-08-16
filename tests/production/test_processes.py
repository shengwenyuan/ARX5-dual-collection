from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from arx5_collection.production.processes import (
    ManagedProcess,
    ProcessExit,
    ProcessSpec,
    RosProcessSupervisor,
)


class ManagedProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.log_path = Path(self.directory.name) / "worker.log"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_process_has_owned_group_and_stops_on_interrupt(self) -> None:
        process = ManagedProcess(
            ProcessSpec(
                "worker",
                (
                    sys.executable,
                    "-c",
                    "import time; print('ready', flush=True); time.sleep(60)",
                ),
                self.log_path,
            )
        )
        process.start()
        process.require_running()
        exit_result = process.stop(interrupt_timeout_s=2.0)
        self.assertFalse(process.running)
        self.assertEqual(exit_result.name, "worker")
        self.assertIsNone(exit_result.escalated_to)

    def test_argv_is_required(self) -> None:
        with self.assertRaises(ValueError):
            ProcessSpec("worker", (), self.log_path)


class FakeProcess:
    def __init__(self, name: str, events: list[str]) -> None:
        self.spec = ProcessSpec(name, ("fake",), Path(f"{name}.log"))
        self.events = events
        self.running = False

    def start(self) -> None:
        self.running = True
        self.events.append(f"start:{self.spec.name}")

    def require_running(self) -> None:
        if not self.running:
            raise RuntimeError("not running")

    def stop(self) -> ProcessExit:
        self.running = False
        self.events.append(f"stop:{self.spec.name}")
        return ProcessExit(self.spec.name, 0, None)


class SupervisorTest(unittest.TestCase):
    def test_starts_in_order_and_stops_in_reverse(self) -> None:
        events: list[str] = []
        supervisor = RosProcessSupervisor()
        supervisor.start(FakeProcess("arx", events))  # type: ignore[arg-type]
        supervisor.start(FakeProcess("camera", events))  # type: ignore[arg-type]
        supervisor.require_running()
        supervisor.stop_all()
        self.assertEqual(
            events,
            ["start:arx", "start:camera", "stop:camera", "stop:arx"],
        )


if __name__ == "__main__":
    unittest.main()

