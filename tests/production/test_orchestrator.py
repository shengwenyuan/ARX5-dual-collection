from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path

from arx5_collection.episode.models import (
    EpisodeBlocked,
    EpisodeOutcome,
    EpisodeRequest,
    RecordingStopping,
)
from arx5_collection.capture import RGB_ONLY_STREAMS
from arx5_collection.production.checks import CheckFailure, CheckPhase, CheckResult
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

    def require_ready(self, stream_ids):
        self.events.append(f"gate:require:{','.join(stream_ids)}")

    def results(self, stream_ids):
        return tuple(
            CheckResult(f"telemetry_{stream_id}", CheckPhase.ROS, True, "ready")
            for stream_id in stream_ids
        )

    def stop(self):
        self.events.append("gate:stop")


class FailingReadiness(FakeReadiness):
    def require_ready(self, stream_ids):
        self.events.append(f"gate:require:{','.join(stream_ids)}")
        raise CheckFailure(
            (CheckResult("camera_left", CheckPhase.EPISODE, False, "stopped"),)
        )


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


class RepeatedInterruptSupervisor(FakeSupervisor):
    def stop_all(self):
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGTERM)
        return super().stop_all()


class FakeCommands:
    def arx5_controller(self, profile):
        return NamedProcess("arx5-controller")

    def arm_state_adapter(self, profile):
        return NamedProcess("arm-state-adapter")

    def d405_source(self, cameras, snapshot=None):
        return NamedProcess("d405-source")


class FakeSessionMonitor:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def open(self) -> None:
        self.events.append("monitor:open")

    def wait_until_ready(self, stream_ids, timeout_s, process_check) -> None:
        process_check()
        self.events.append("monitor:ready")

    def start(self, streams) -> None:
        self.events.append("monitor:episode-start")

    def required_failure(self):
        return None

    def stop(self):
        self.events.append("monitor:episode-stop")
        return ()

    def close(self) -> None:
        self.events.append("monitor:close")


class FakeHomeController:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def open(self) -> None:
        self.events.append("home:open")

    def reset_both(self) -> None:
        self.events.append("home:reset")

    def enable_gravity_compensation(self) -> None:
        self.events.append("home:gcomp")

    def close(self) -> None:
        self.events.append("home:close")


class ProductionSessionTest(unittest.TestCase):
    def test_rgb_only_waits_for_only_five_required_streams(self) -> None:
        events: list[str] = []
        station = load_station_config(ROOT / "config" / "station.example.json")
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
                monitor=FakeSessionMonitor(events),
                home_controller=FakeHomeController(events),
                required_stream_ids=tuple(RGB_ONLY_STREAMS),
            )
            session.start()
            session.stop()

        camera_wait = next(
            event for event in events if event.startswith("gate:wait:camera_")
        )
        self.assertNotIn("aligned_depth", camera_wait)
        self.assertIn("camera_overview_color", camera_wait)
        require = next(
            event for event in events if event.startswith("gate:require:")
        )
        self.assertNotIn("aligned_depth", require)

    def test_one_session_starts_sources_once_and_stops_in_reverse(self) -> None:
        events: list[str] = []
        station = load_station_config(ROOT / "config" / "station.example.json")
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
                monitor=FakeSessionMonitor(events),
                home_controller=FakeHomeController(events),
            )
            session.start()
            session.pre_episode_check()
            session.pre_episode_check()
            session.stop()

        starts = [event for event in events if event.startswith("ros:start")]
        self.assertEqual(
            starts,
            [
                "ros:start:arx5-controller",
                "ros:start:arm-state-adapter",
                "ros:start:d405-source",
            ],
        )
        self.assertEqual(
            [event for event in events if event.startswith("ros:stop")],
            [
                "ros:stop:d405-source",
                "ros:stop:arm-state-adapter",
                "ros:stop:arx5-controller",
            ],
        )
        self.assertEqual(events[-2:], ["gate:stop", "system:stop"])
        self.assertLess(events.index("monitor:close"), events.index("home:close"))
        self.assertLess(events.index("home:close"), events.index("ros:stop:d405-source"))

    def test_repeated_signals_do_not_interrupt_owned_cleanup(self) -> None:
        events: list[str] = []
        station = load_station_config(ROOT / "config" / "station.example.json")
        with tempfile.TemporaryDirectory() as directory:
            session = ProductionSession(
                station,
                Path(directory) / "episodes",
                Path(directory) / "logs",
                "0.1.0",
                min_free_bytes=0,
                identity=FakeIdentity(),  # type: ignore[arg-type]
                system=FakeSystem(events),  # type: ignore[arg-type]
                supervisor=RepeatedInterruptSupervisor(events),  # type: ignore[arg-type]
                readiness=FakeReadiness(events),  # type: ignore[arg-type]
                commands=FakeCommands(),  # type: ignore[arg-type]
                monitor=FakeSessionMonitor(events),
                home_controller=FakeHomeController(events),
            )
            session.start()
            session.stop()
        self.assertEqual(events[-2:], ["gate:stop", "system:stop"])

    def test_pre_recording_action_runs_after_session_checks(self) -> None:
        events: list[str] = []
        station = load_station_config(ROOT / "config" / "station.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = ProductionSession(
                station,
                root / "episodes",
                root / "logs",
                "0.1.0",
                min_free_bytes=0,
                identity=FakeIdentity(),  # type: ignore[arg-type]
                system=FakeSystem(events),  # type: ignore[arg-type]
                supervisor=FakeSupervisor(events),  # type: ignore[arg-type]
                readiness=FakeReadiness(events),  # type: ignore[arg-type]
                commands=FakeCommands(),  # type: ignore[arg-type]
                monitor=FakeSessionMonitor(events),
                home_controller=FakeHomeController(events),
            )
            session.start()
            request = EpisodeRequest(
                "test",
                "test",
                root / "episodes",
                ROOT / "config" / "station.example.json",
                (),
            )
            runtime = session.create_runtime(request, object())  # type: ignore[arg-type]
            assert runtime.pre_episode_check is not None
            runtime.pre_episode_check()
            session.stop()

        require_index = next(
            index
            for index, event in enumerate(events)
            if event.startswith("gate:require:")
        )
        self.assertLess(require_index, events.index("home:reset"))

    def test_pre_episode_failure_enters_gcomp_and_is_recoverable(self) -> None:
        events: list[str] = []
        station = load_station_config(ROOT / "config" / "station.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = ProductionSession(
                station,
                root / "episodes",
                root / "logs",
                "0.1.0",
                min_free_bytes=0,
                identity=FakeIdentity(),  # type: ignore[arg-type]
                system=FakeSystem(events),  # type: ignore[arg-type]
                supervisor=FakeSupervisor(events),  # type: ignore[arg-type]
                readiness=FailingReadiness(events),  # type: ignore[arg-type]
                commands=FakeCommands(),  # type: ignore[arg-type]
                monitor=FakeSessionMonitor(events),
                home_controller=FakeHomeController(events),
            )
            request = EpisodeRequest(
                "test",
                "test",
                root / "episodes",
                ROOT / "config" / "station.example.json",
                (),
            )
            runtime = session.create_runtime(request, object())  # type: ignore[arg-type]
            assert runtime.pre_episode_check is not None

            with self.assertRaises(EpisodeBlocked):
                runtime.pre_episode_check()

            self.assertFalse((root / "episodes").exists())

        self.assertEqual(events[-1], "home:gcomp")

    def test_ordinary_aborted_episode_stop_enters_gcomp(self) -> None:
        events: list[str] = []
        station = load_station_config(ROOT / "config" / "station.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = ProductionSession(
                station,
                root / "episodes",
                root / "logs",
                "0.1.0",
                min_free_bytes=0,
                identity=FakeIdentity(),  # type: ignore[arg-type]
                system=FakeSystem(events),  # type: ignore[arg-type]
                supervisor=FakeSupervisor(events),  # type: ignore[arg-type]
                readiness=FakeReadiness(events),  # type: ignore[arg-type]
                commands=FakeCommands(),  # type: ignore[arg-type]
                monitor=FakeSessionMonitor(events),
                home_controller=FakeHomeController(events),
            )
            request = EpisodeRequest(
                "test",
                "test",
                root / "episodes",
                ROOT / "config" / "station.example.json",
                (),
            )
            runtime = session.create_runtime(request, object())  # type: ignore[arg-type]
            assert runtime.recording_stopping_hook is not None
            runtime.recording_stopping_hook(
                RecordingStopping(EpisodeOutcome.ABORTED, 1)
            )

        self.assertEqual(events, ["home:gcomp"])

    def test_ordinary_failed_episode_stop_enters_gcomp(self) -> None:
        events: list[str] = []
        station = load_station_config(ROOT / "config" / "station.example.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = FakeSupervisor(events)
            session = ProductionSession(
                station,
                root / "episodes",
                root / "logs",
                "0.1.0",
                min_free_bytes=0,
                identity=FakeIdentity(),  # type: ignore[arg-type]
                system=FakeSystem(events),  # type: ignore[arg-type]
                supervisor=supervisor,  # type: ignore[arg-type]
                readiness=FakeReadiness(events),  # type: ignore[arg-type]
                commands=FakeCommands(),  # type: ignore[arg-type]
                monitor=FakeSessionMonitor(events),
                home_controller=FakeHomeController(events),
            )
            request = EpisodeRequest(
                "test",
                "test",
                root / "episodes",
                ROOT / "config" / "station.example.json",
                (),
            )
            runtime = session.create_runtime(request, object())  # type: ignore[arg-type]
            assert runtime.recording_stopping_hook is not None
            assert runtime.runtime_check == supervisor.require_running
            runtime.recording_stopping_hook(RecordingStopping(EpisodeOutcome.FAIL, 1))

        self.assertEqual(events, ["home:gcomp"])

if __name__ == "__main__":
    unittest.main()
