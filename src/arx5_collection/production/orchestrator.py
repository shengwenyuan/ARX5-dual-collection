from __future__ import annotations

import shutil
import signal
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Iterator

from arx5_collection.collection_metadata import MetadataContext
from arx5_collection.episode.finalization import McapFinalizer
from arx5_collection.episode.models import (
    EpisodeBlocked,
    EpisodeOutcome,
    EpisodeRequest,
    RecordingStarted,
    RecordingStopping,
)
from arx5_collection.episode.ports import EpisodeArtifactFinalizer, RecordTrigger
from arx5_collection.episode.runtime import EpisodeRuntime
from arx5_collection.episode.store import EpisodeStore
from arx5_collection.reset import ResetCoordinator, ResetState
from arx5_collection.ros2_adapters.monitor import RosStreamMonitor
from arx5_collection.ros2_adapters.recording import RosbagRecordingBackend
from arx5_collection.ros2_adapters.reset import RosDualArmResetController

from .checks import CheckFailure, CheckPhase, CheckResult, require_passed
from .config import StationConfig, set_process_ros_domain_id
from .devices import DeviceIdentityVerifier
from .processes import (
    CameraSnapshotConfig,
    ManagedProcess,
    ProcessUnavailableError,
    RosCommandSet,
    RosProcessSupervisor,
)
from .ports import SessionArmController, SessionStreamMonitor
from .profiles import ArmRuntimeProfile, TEACHING_ARM_PROFILE, reset_specs_for
from .readiness import RosReadinessGate
from .system import SystemBringup


GIB = 1024**3
CheckSink = Callable[[CheckResult], None]
WarningSink = Callable[[str], None]
ResetStateSink = Callable[[ResetState], None]
HomeTimingSink = Callable[[str, float], None]


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
        camera_snapshot: CameraSnapshotConfig | None = None,
        arm_profile: ArmRuntimeProfile = TEACHING_ARM_PROFILE,
        additional_processes: tuple[ManagedProcess, ...] = (),
        backend: RosbagRecordingBackend | None = None,
        monitor: SessionStreamMonitor | None = None,
        home_controller: SessionArmController | None = None,
        home_state_sink: ResetStateSink | None = None,
        home_timing_sink: HomeTimingSink | None = None,
        check_sink: CheckSink | None = None,
        warning_sink: WarningSink | None = None,
        fail_directory: str = "fail",
        compression_enabled: bool = True,
        finalizer: EpisodeArtifactFinalizer | None = None,
    ) -> None:
        if min_free_bytes < 0:
            raise ValueError("min_free_bytes must not be negative")
        set_process_ros_domain_id(station.ros_domain_id)
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
        self.camera_snapshot = camera_snapshot
        self.arm_profile = arm_profile
        self.additional_processes = additional_processes
        self.backend = backend or RosbagRecordingBackend()
        self.monitor = monitor or RosStreamMonitor(self.backend)
        self.home_controller = home_controller or RosDualArmResetController(
            arms=reset_specs_for(self.arm_profile),
            timing_sink=home_timing_sink,
        )
        self.home = ResetCoordinator(
            self.home_controller,
            state_sink=home_state_sink,
        )
        self.check_sink = check_sink or (lambda result: None)
        self.warning_sink = warning_sink or (lambda warning: None)
        self.fail_directory = fail_directory
        self.finalizer = finalizer or McapFinalizer(
            enabled=compression_enabled,
            warning_sink=self.warning_sink,
        )
        self._system_started = False
        self._readiness_started = False
        self._monitor_open = False
        self._home_open = False
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
            self.monitor.open()
            self._monitor_open = True

            self.supervisor.start(self.commands.arx5_controller(self.arm_profile))
            self.supervisor.start(
                self.commands.arm_state_adapter(self.arm_profile)
            )
            self._report(
                self.readiness.wait_for(
                    ("left_arm_state", "right_arm_state"),
                    self.readiness_timeout_s,
                    self.supervisor.require_running,
                )
            )
            self.home_controller.open()
            self._home_open = True

            self.supervisor.start(
                self.commands.d405_source(
                    self.station.cameras,
                    snapshot=self.camera_snapshot,
                )
            )
            self._report(
                self.readiness.wait_for(
                    tuple(
                        stream_id
                        for role in ("left", "right", "overview")
                        for stream_id in (
                            f"camera_{role}_color",
                            f"camera_{role}_aligned_depth",
                        )
                    ),
                    self.readiness_timeout_s,
                    self.supervisor.require_running,
                )
            )
            for process in self.additional_processes:
                self.supervisor.start(process)
            self.monitor.wait_until_ready(
                self._stream_ids(),
                self.readiness_timeout_s,
                self.supervisor.require_running,
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
        metadata_context_provider: Callable[[], MetadataContext] | None = None,
        recording_started_hook: Callable[[RecordingStarted], None] | None = None,
        recording_stopping_hook: Callable[[RecordingStopping], None] | None = None,
        metadata_extensions: dict[str, object] | None = None,
    ) -> EpisodeRuntime:
        def prepare_episode() -> None:
            try:
                self.pre_episode_check()
            except CheckFailure as error:
                safety = self._confirm_gravity_compensation(str(error))
                raise EpisodeBlocked(str(error), safety) from error
            except ProcessUnavailableError as error:
                safety = self._confirm_gravity_compensation(str(error))
                if error.process_name == "arx5-controller":
                    raise RuntimeError(f"{error}; {safety}") from error
                raise EpisodeBlocked(str(error), safety) from error
            self.home.run()

        def stop_episode(stopping: RecordingStopping) -> None:
            if recording_stopping_hook is not None:
                recording_stopping_hook(stopping)
            elif stopping.outcome is not EpisodeOutcome.SUCCESS:
                self._confirm_gravity_compensation(
                    f"Episode {stopping.outcome.value}"
                )

        return EpisodeRuntime(
            store=EpisodeStore(
                request.output_root,
                min_free_bytes=self.min_free_bytes,
                fail_directory=self.fail_directory,
            ),
            trigger=trigger,
            backend=self.backend,
            monitor=self.monitor,
            software_version=self.software_version,
            pre_episode_check=prepare_episode,
            runtime_check=self.supervisor.require_running,
            metadata_context_provider=metadata_context_provider,
            recording_started_hook=recording_started_hook,
            recording_stopping_hook=stop_episode,
            finalizer=self.finalizer,
            metadata_extensions=metadata_extensions,
        )

    def _confirm_gravity_compensation(self, reason: str) -> str:
        try:
            self.home_controller.enable_gravity_compensation()
        except BaseException as error:
            raise RuntimeError(
                f"{reason}; dual-arm G_COMPENSATION recovery failed: {error}"
            ) from error
        return "dual-arm G_COMPENSATION confirmed"

    def stop(self) -> None:
        with ignore_repeated_termination():
            self._stop_owned_resources()

    def _stop_owned_resources(self) -> None:
        errors: list[BaseException] = []
        if self._monitor_open:
            try:
                self.monitor.close()
            except BaseException as error:
                errors.append(error)
            self._monitor_open = False
        if self._home_open:
            try:
                self.home_controller.close()
            except BaseException as error:
                errors.append(error)
            self._home_open = False
        if self.supervisor.names:
            try:
                exits = self.supervisor.stop_all()
                for exit_result in exits:
                    if (
                        exit_result.name == "arx5-controller"
                        and exit_result.return_code != 0
                    ):
                        self.warning_sink(
                            "Vendor shutdown warning: arx5-controller returned "
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
