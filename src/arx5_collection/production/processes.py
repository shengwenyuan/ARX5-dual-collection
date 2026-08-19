from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .config import CameraConfig


@dataclass(frozen=True, slots=True)
class CameraSnapshotConfig:
    max_camera_span_ms: float
    max_arm_age_ms: float
    max_snapshot_age_ms: float

    def __post_init__(self) -> None:
        if min(
            self.max_camera_span_ms,
            self.max_arm_age_ms,
            self.max_snapshot_age_ms,
        ) <= 0:
            raise ValueError("camera snapshot limits must be positive")


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    argv: tuple[str, ...]
    log_path: Path
    cwd: Path | None = None
    environment: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.argv:
            raise ValueError("process name and argv must not be empty")


@dataclass(frozen=True, slots=True)
class ProcessExit:
    name: str
    return_code: int
    escalated_to: str | None


class ManagedProcess:
    """Own one subprocess and its independent POSIX process group."""

    def __init__(self, spec: ProcessSpec) -> None:
        self.spec = spec
        self._process: subprocess.Popen[Any] | None = None
        self._log: IO[bytes] | None = None

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def return_code(self) -> int | None:
        return None if self._process is None else self._process.poll()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError(f"process {self.spec.name} has already been started")
        self.spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.spec.log_path.open("ab", buffering=0)
        environment = None
        if self.spec.environment is not None:
            environment = dict(os.environ)
            environment.update(self.spec.environment)
        try:
            self._process = subprocess.Popen(
                list(self.spec.argv),
                cwd=self.spec.cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            self._close_log()
            raise

    def require_running(self) -> None:
        if self._process is None:
            raise RuntimeError(f"process {self.spec.name} has not been started")
        return_code = self._process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"process {self.spec.name} exited with {return_code}; "
                f"log={self.spec.log_path}"
            )

    def stop(
        self,
        interrupt_timeout_s: float = 10.0,
        terminate_timeout_s: float = 3.0,
        kill_timeout_s: float = 1.0,
    ) -> ProcessExit:
        if self._process is None:
            raise RuntimeError(f"process {self.spec.name} has not been started")
        process = self._process
        escalated_to: str | None = None
        try:
            if process.poll() is None:
                self._signal_group(signal.SIGINT)
                if not self._wait(interrupt_timeout_s):
                    escalated_to = "TERM"
                    self._signal_group(signal.SIGTERM)
                    if not self._wait(terminate_timeout_s):
                        escalated_to = "KILL"
                        self._signal_group(signal.SIGKILL)
                        if not self._wait(kill_timeout_s):
                            raise TimeoutError(
                                f"process group {self.spec.name} did not stop after SIGKILL"
                            )
            assert process.returncode is not None
            return ProcessExit(self.spec.name, process.returncode, escalated_to)
        finally:
            self._close_log()

    def _signal_group(self, signal_number: signal.Signals) -> None:
        assert self._process is not None
        try:
            os.killpg(self._process.pid, signal_number)
        except ProcessLookupError:
            pass

    def _wait(self, timeout_s: float) -> bool:
        assert self._process is not None
        try:
            self._process.wait(timeout=max(0.0, timeout_s))
            return True
        except subprocess.TimeoutExpired:
            return False

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None


class RosProcessSupervisor:
    """Start named ROS processes in order and stop only owned groups in reverse."""

    def __init__(self) -> None:
        self._processes: list[ManagedProcess] = []

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(process.spec.name for process in self._processes)

    def start(self, process: ManagedProcess) -> None:
        if process.spec.name in self.names:
            raise ValueError(f"duplicate process name: {process.spec.name}")
        process.start()
        try:
            process.require_running()
        except BaseException:
            if process.running:
                process.stop()
            raise
        self._processes.append(process)

    def require_running(self) -> None:
        for process in self._processes:
            process.require_running()

    def stop_all(self) -> tuple[ProcessExit, ...]:
        exits: list[ProcessExit] = []
        errors: list[BaseException] = []
        for process in reversed(self._processes):
            try:
                exits.append(process.stop())
            except BaseException as error:
                errors.append(error)
        self._processes.clear()
        if errors:
            raise RuntimeError("one or more ROS process groups failed to stop") from errors[0]
        return tuple(exits)


class RosCommandSet:
    """Build the frozen production argv contract without invoking a shell."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    def arx5_v2_collect(self) -> ManagedProcess:
        return self._process(
            "arx5-v2-collect",
            (
                "ros2",
                "launch",
                "/opt/arx_ws/install/share/arx_x5_controller/launch/x5_v2/v2_collect.launch.py",
            ),
        )

    def arm_state_adapter(self) -> ManagedProcess:
        return self._process(
            "arm-state-adapter",
            ("ros2", "run", "arx5_arm_adapter", "arm_state_adapter"),
        )

    def d405_source(
        self,
        cameras: tuple[CameraConfig, ...],
        snapshot: CameraSnapshotConfig | None = None,
    ) -> ManagedProcess:
        by_role = {camera.role: camera for camera in cameras}
        if set(by_role) != {"left", "right", "overview"}:
            raise ValueError("unified D405 source requires left, right, and overview")
        arguments = [
            "ros2",
            "run",
            "arx5_d405_source_cpp",
            "multi_d405_source",
            "--ros-args",
        ]
        for role in ("left", "overview", "right"):
            arguments.extend(
                ("-p", f"serial_{role}:='{by_role[role].serial_number}'")
            )
        arguments.extend(
            (
                "-p",
                "width:=848",
                "-p",
                "height:=480",
                "-p",
                "fps:=30",
                "-p",
                "enable_snapshot_service:=" + ("true" if snapshot else "false"),
            )
        )
        if snapshot is not None:
            arguments.extend(
                (
                    "-p",
                    f"max_camera_span_ms:={snapshot.max_camera_span_ms}",
                    "-p",
                    f"max_arm_age_ms:={snapshot.max_arm_age_ms}",
                    "-p",
                    f"max_snapshot_age_ms:={snapshot.max_snapshot_age_ms}",
                )
            )
        return self._process("d405-source", tuple(arguments))

    def _process(self, name: str, argv: tuple[str, ...]) -> ManagedProcess:
        return ManagedProcess(
            ProcessSpec(name=name, argv=argv, log_path=self.log_dir / f"{name}.log")
        )
