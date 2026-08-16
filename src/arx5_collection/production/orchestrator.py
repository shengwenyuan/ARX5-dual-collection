from __future__ import annotations

import shutil
import signal
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Iterator

from arx5_collection.episode.models import EpisodeRequest
from arx5_collection.episode.ports import RecordTrigger
from arx5_collection.episode.runtime import EpisodeRuntime
from arx5_collection.episode.store import EpisodeStore
from arx5_collection.ros2_adapters.monitor import RosStreamMonitor
from arx5_collection.ros2_adapters.recording import RosbagRecordingBackend

from .checks import CheckPhase, CheckResult, require_passed
from .config import StationConfig
from .devices import DeviceIdentityVerifier
from .processes import RosCommandSet, RosProcessSupervisor
from .readiness import RosReadinessGate
from .system import SystemBringup


GIB = 1024**3
CheckSink = Callable[[CheckResult], None]
WarningSink = Callable[[str], None]


class ProductionSession:
    """Own hardware and ROS once while Episode Runtime cycles independently."""

    def __init__(
        self,
        station: StationConfig,
        output_root: Path,
        log_dir: Path,
        software_version: str,
        min_free_bytes: int = 80 * GIB,
        readiness_timeout_s: float = 30.0,
        identity: DeviceIdentityVerifier | None = None,
        system: SystemBringup | None = None,
        supervisor: RosProcessSupervisor | None = None,
        readiness: RosReadinessGate | None = None,
        commands: RosCommandSet | None = None,
        check_sink: CheckSink | None = None,
        warning_sink: WarningSink | None = None,
    ) -> None:
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes must not be negative")
        self.station = station
        self.output_root = output_root
        self.log_dir = log_dir
        self.software_version = software_version
        self.min_free_bytes = min_free_bytes
        self.readiness_timeout_s = readiness_timeout_s
        self.identity = identity or DeviceIdentityVerifier(station)
        self.system = system or SystemBringup(station, log_dir)
        self.supervisor = supervisor or RosProcessSupervisor()
        self.readiness = readiness or RosReadinessGate()
        self.commands = commands or RosCommandSet(log_dir)
        self.check_sink = check_sink or (lambda result: None)
        self.warning_sink = warning_sink or (lambda warning: None)
        self._system_started = False
        self._readiness_started = False
        self._started = False

    def __enter__(self) -> ProductionSession:
        self.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.stop()
        except BaseException as cleanup_error:
            if exception_type is None:
                raise
            self.warning_sink(f"Session cleanup failed: {cleanup_error}")

    def start(self) -> None:
        if self._started:
            raise RuntimeError("production Session is already active")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._report(require_passed(self.identity.checks()))
            self._report(require_passed((self._disk_check(CheckPhase.SESSION),)))
            self._report(self.system.start())
            self._system_started = True
            self.readiness.start()
            self._readiness_started = True

            self.supervisor.start(self.commands.arx5_v2_collect())
            self.supervisor.start(self.commands.arm_state_adapter())
            self._report(
                self.readiness.wait_for(
                    ("left_arm_state", "right_arm_state"),
                    self.readiness_timeout_s,
                    self.supervisor.require_running,
                )
            )

            for camera in self.station.cameras:
                self.supervisor.start(self.commands.d405_source(camera))
                self._report(
                    self.readiness.wait_for(
                        (
                            f"camera_{camera.role}_color",
                            f"camera_{camera.role}_aligned_depth",
                        ),
                        self.readiness_timeout_s,
                        self.supervisor.require_running,
                    )
                )
            self.pre_episode_check()
            self._started = True
        except BaseException:
            self.stop()
            raise

    def pre_episode_check(self) -> None:
        self.supervisor.require_running()
        self._report(require_passed(self.system.check()))
        self.readiness.require_ready()
        self._report(self.readiness.results(tuple(self._stream_ids())))
        self._report(require_passed((self._disk_check(CheckPhase.EPISODE),)))

    def create_runtime(
        self,
        request: EpisodeRequest,
        trigger: RecordTrigger,
        pre_recording_action: Callable[[], None] | None = None,
    ) -> EpisodeRuntime:
        def prepare_episode() -> None:
            self.pre_episode_check()
            if pre_recording_action is not None:
                pre_recording_action()

        backend = RosbagRecordingBackend()
        return EpisodeRuntime(
            store=EpisodeStore(request.output_root, min_free_bytes=self.min_free_bytes),
            trigger=trigger,
            backend=backend,
            monitor=RosStreamMonitor(backend),
            software_version=self.software_version,
            pre_episode_check=prepare_episode,
        )

    def stop(self) -> None:
        with ignore_repeated_termination():
            self._stop_owned_resources()

    def _stop_owned_resources(self) -> None:
        errors: list[BaseException] = []
        if self.supervisor.names:
            try:
                exits = self.supervisor.stop_all()
                for exit_result in exits:
                    if (
                        exit_result.name == "arx5-v2-collect"
                        and exit_result.return_code != 0
                    ):
                        self.warning_sink(
                            "Vendor shutdown warning: arx5-v2-collect returned "
                            f"{exit_result.return_code} after requested shutdown"
                        )
            except BaseException as error:
                errors.append(error)
        if self._readiness_started:
            try:
                self.readiness.stop()
            except BaseException as error:
                errors.append(error)
            self._readiness_started = False
        if self._system_started:
            try:
                self._report(self.system.stop())
            except BaseException as error:
                errors.append(error)
            self._system_started = False
        self._started = False
        if errors:
            raise RuntimeError("production Session cleanup failed") from errors[0]

    def _report(self, results: tuple[CheckResult, ...]) -> None:
        for result in results:
            self.check_sink(result)

    def _disk_check(self, phase: CheckPhase) -> CheckResult:
        self.output_root.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(self.output_root).free
        return CheckResult(
            "disk_space",
            phase,
            free_bytes >= self.min_free_bytes,
            f"free_bytes={free_bytes}, required>={self.min_free_bytes}",
        )

    @staticmethod
    def _stream_ids() -> tuple[str, ...]:
        return (
            "left_arm_state",
            "right_arm_state",
            "camera_left_color",
            "camera_left_aligned_depth",
            "camera_right_color",
            "camera_right_aligned_depth",
            "camera_overview_color",
            "camera_overview_aligned_depth",
        )


@contextmanager
def ignore_repeated_termination() -> Iterator[None]:
    previous_interrupt = signal.getsignal(signal.SIGINT)
    previous_terminate = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_interrupt)
        signal.signal(signal.SIGTERM, previous_terminate)
