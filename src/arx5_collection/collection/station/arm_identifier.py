from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Protocol

from arx5_collection.collection.runtime.config import ArmConfig
from arx5_collection.collection.runtime.processes import (
    RosCommandSet,
    RosProcessSupervisor,
)
from arx5_collection.collection.runtime.profiles import TEACHING_ARM_PROFILE
from arx5_collection.collection.runtime.system import SystemBringup, Usb2CanDevice
from arx5_collection.adapters.ros2.reset import RosDualArmResetController
from arx5_collection.collection.environment import ENVIRONMENT


class ArmIdentificationError(RuntimeError):
    pass


class ArmStation(Protocol):
    arms: tuple[ArmConfig, ...]


class ArmObserver(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def enable_gravity_compensation(self) -> None: ...
    def wait_for_samples(self, timeout_s: float | None = None) -> None: ...
    def sample_positions(self) -> dict[str, tuple[float, ...]]: ...


@dataclass(frozen=True, slots=True)
class _ProvisionalStation:
    arms: tuple[ArmConfig, ...]


class MovementDetector:
    def __init__(
        self,
        movement_threshold_rad: float = ENVIRONMENT.station.movement_threshold_rad,
    ) -> None:
        if movement_threshold_rad <= 0:
            raise ValueError("movement threshold must be positive")
        self.movement_threshold_rad = movement_threshold_rad
        self.quiet_threshold_rad = movement_threshold_rad / 2.0

    def scores(
        self,
        baseline: Mapping[str, Sequence[float]],
        current: Mapping[str, Sequence[float]],
    ) -> dict[str, float]:
        if set(baseline) != {"left", "right"} or set(current) != set(baseline):
            raise ArmIdentificationError("both provisional arm samples are required")
        result = {}
        for role in ("left", "right"):
            before, after = tuple(baseline[role]), tuple(current[role])
            if len(before) != 6 or len(after) != 6:
                raise ArmIdentificationError("each arm sample must contain six joints")
            result[role] = max(abs(a - b) for a, b in zip(after, before))
        return result

    def classify(self, peak_scores: Mapping[str, float]) -> str | None:
        moved = [
            role
            for role in ("left", "right")
            if peak_scores.get(role, 0.0) >= self.movement_threshold_rad
        ]
        if len(moved) > 1:
            raise ArmIdentificationError(
                f"both provisional arms moved; scores={dict(peak_scores)}"
            )
        if len(moved) == 1:
            other = "right" if moved[0] == "left" else "left"
            if peak_scores.get(other, 0.0) <= self.quiet_threshold_rad:
                return moved[0]
        return None


class ArmIdentifier:
    """Identify physical left USB2CAN by a supervised left-arm movement."""

    def __init__(
        self,
        log_dir: Path,
        movement_timeout_s: float = ENVIRONMENT.station.movement_timeout_s,
        detector: MovementDetector | None = None,
        clock: Callable[[], float] = monotonic,
        sleep_fn: Callable[[float], None] = sleep,
        system_factory: Callable[[ArmStation, Path], SystemBringup] | None = None,
        supervisor_factory: Callable[[], RosProcessSupervisor] = RosProcessSupervisor,
        commands_factory: Callable[[Path], RosCommandSet] = RosCommandSet,
        observer_factory: Callable[[], ArmObserver] = RosDualArmResetController,
    ) -> None:
        self.log_dir = log_dir
        self.movement_timeout_s = movement_timeout_s
        self.detector = detector or MovementDetector()
        self.clock = clock
        self.sleep_fn = sleep_fn
        self.system_factory = system_factory or (
            lambda station, logs: SystemBringup(station, logs)  # type: ignore[arg-type]
        )
        self.supervisor_factory = supervisor_factory
        self.commands_factory = commands_factory
        self.observer_factory = observer_factory

    def identify(
        self,
        candidates: Sequence[Usb2CanDevice],
        movement_prompt: Callable[[], None],
    ) -> tuple[ArmConfig, ArmConfig]:
        unique = {candidate.serial_number: candidate for candidate in candidates}
        if len(candidates) != 2 or len(unique) != 2:
            raise ArmIdentificationError(
                f"expected exactly two distinct USB2CAN devices, found {len(unique)}"
            )
        ordered = sorted(unique.values(), key=lambda value: value.serial_number)
        provisional = _ProvisionalStation(
            arms=tuple(
                ArmConfig(role, candidate.serial_number, interface)
                for role, candidate, interface in zip(
                    ENVIRONMENT.station.arm_roles,
                    ordered,
                    ENVIRONMENT.station.provisional_can_interfaces,
                )
            )
        )
        system = self.system_factory(provisional, self.log_dir)
        supervisor = self.supervisor_factory()
        commands = self.commands_factory(self.log_dir)
        observer = self.observer_factory()
        system_started = False
        observer_open = False
        try:
            system.start()
            system_started = True
            supervisor.start(commands.arx5_controller(TEACHING_ARM_PROFILE))
            observer.open()
            observer_open = True
            observer.wait_for_samples()
            observer.enable_gravity_compensation()
            self.sleep_fn(0.5)
            baseline = observer.sample_positions()
            self.detector.scores(baseline, baseline)
            movement_prompt()
            moved_role = self._wait_for_movement(observer, baseline)
        finally:
            errors = []
            if observer_open:
                try:
                    observer.close()
                except BaseException as error:
                    errors.append(error)
            if supervisor.names:
                try:
                    supervisor.stop_all()
                except BaseException as error:
                    errors.append(error)
            if system_started:
                try:
                    system.stop()
                except BaseException as error:
                    errors.append(error)
            if errors:
                raise RuntimeError("arm identification cleanup failed") from errors[0]

        left_serial = provisional.arms[0 if moved_role == "left" else 1].usb_serial
        right_serial = provisional.arms[1 if moved_role == "left" else 0].usb_serial
        interfaces = ENVIRONMENT.station.provisional_can_interfaces
        return (
            ArmConfig("left", left_serial, interfaces[0]),
            ArmConfig("right", right_serial, interfaces[1]),
        )

    def _wait_for_movement(
        self,
        observer: ArmObserver,
        baseline: Mapping[str, Sequence[float]],
    ) -> str:
        deadline = self.clock() + self.movement_timeout_s
        peaks = {"left": 0.0, "right": 0.0}
        while self.clock() < deadline:
            scores = self.detector.scores(baseline, observer.sample_positions())
            for role, score in scores.items():
                peaks[role] = max(peaks[role], score)
            classified = self.detector.classify(peaks)
            if classified is not None:
                return classified
            self.sleep_fn(0.01)
        raise ArmIdentificationError(
            f"left-arm movement was not identified; peak_scores={peaks}"
        )
