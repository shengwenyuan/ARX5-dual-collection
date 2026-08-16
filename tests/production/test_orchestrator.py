from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arx5_collection.production.checks import CheckPhase, CheckResult
from arx5_collection.production.config import load_station_config
from arx5_collection.production.orchestrator import ProductionSession
from arx5_collection.production.processes import ProcessExit, ProcessSpec


ROOT = Path(__file__).parents[2]


class NamedProcess:
    def __init__(self, name: str) -> None:
        self.spec = ProcessSpec(name, ("fake",), Path(f"{name}.log"))


class FakeIdentity:
    def checks(self):
        return (CheckResult("devices", CheckPhase.SESSION, True, "five"),)


class FakeSystem:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self):
        self.events.append("system:start")
        return (CheckResult("system", CheckPhase.SYSTEM, True, "up"),)

    def check(self):
        self.events.append("system:check")
        return (CheckResult("system", CheckPhase.SYSTEM, True, "up"),)

    def stop(self):
        self.events.append("system:stop")
        return (CheckResult("system", CheckPhase.SHUTDOWN, True, "down"),)


class FakeReadiness:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self):
        self.events.append("gate:start")

    def wait_for(self, stream_ids, timeout_s, process_check):
        process_check()
        label = ",".join(stream_ids)
        self.events.append(f"gate:wait:{label}")
        return tuple(
            CheckResult(f"telemetry_{stream_id}", CheckPhase.ROS, True, "ready")
            for stream_id in stream_ids
        )

    def require_ready(self):
        self.events.append("gate:require")

    def results(self, stream_ids):
        return tuple(
            CheckResult(f"telemetry_{stream_id}", CheckPhase.ROS, True, "ready")
            for stream_id in stream_ids
        )

    def stop(self):
        self.events.append("gate:stop")


class FakeSupervisor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.processes = []

    @property
    def names(self):
        return tuple(process.spec.name for process in self.processes)

    def start(self, process):
        self.processes.append(process)
        self.events.append(f"ros:start:{process.spec.name}")

    def require_running(self):
        self.events.append("ros:check")

    def stop_all(self):
        exits = tuple(
            ProcessExit(process.spec.name, 0, None)
            for process in reversed(self.processes)
        )
        for result in exits:
            self.events.append(f"ros:stop:{result.name}")
        self.processes.clear()
        return exits


class FakeCommands:
    def arx5_v2_collect(self):
        return NamedProcess("arx5-v2-collect")

    def arm_state_adapter(self):
        return NamedProcess("arm-state-adapter")

    def d405_source(self, camera):
        return NamedProcess(f"d405-{camera.role}")


class ProductionSessionTest(unittest.TestCase):
    def test_one_session_starts_sources_once_and_stops_in_reverse(self) -> None:
        events: list[str] = []
        station = load_station_config(ROOT / "config" / "station.w3.json")
        with tempfile.TemporaryDirectory() as directory:
            session = ProductionSession(
                station,
                Path(directory) / "episodes",
                Path(directory) / "logs",
                "0.1.0",
                min_free_bytes=0,
                identity=FakeIdentity(),  # type: ignore[arg-type]
                system=FakeSystem(events),  # type: ignore[arg-type]
                supervisor=FakeSupervisor(events),  # type: ignore[arg-type]
                readiness=FakeReadiness(events),  # type: ignore[arg-type]
                commands=FakeCommands(),  # type: ignore[arg-type]
            )
            session.start()
            session.pre_episode_check()
            session.pre_episode_check()
            session.stop()

        starts = [event for event in events if event.startswith("ros:start")]
        self.assertEqual(
            starts,
            [
                "ros:start:arx5-v2-collect",
                "ros:start:arm-state-adapter",
                "ros:start:d405-left",
                "ros:start:d405-right",
                "ros:start:d405-overview",
            ],
        )
        self.assertEqual(
            [event for event in events if event.startswith("ros:stop")],
            [
                "ros:stop:d405-overview",
                "ros:stop:d405-right",
                "ros:stop:d405-left",
                "ros:stop:arm-state-adapter",
                "ros:stop:arx5-v2-collect",
            ],
        )
        self.assertEqual(events[-2:], ["gate:stop", "system:stop"])


if __name__ == "__main__":
    unittest.main()
