from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from .checks import CheckPhase, CheckResult
from .config import ArmConfig, StationConfig
from .processes import ManagedProcess, ProcessExit, ProcessSpec


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def default_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, **kwargs)


class UsbfsManager:
    def __init__(
        self,
        required_mb: int = 256,
        parameter_path: Path = Path("/sys/module/usbcore/parameters/usbfs_memory_mb"),
    ) -> None:
        if required_mb <= 0:
            raise ValueError("required_mb must be positive")
        self.required_mb = required_mb
        self.parameter_path = parameter_path
        self._original_mb: int | None = None
        self._changed = False

    def apply(self) -> CheckResult:
        current = self._read()
        if self._original_mb is None:
            self._original_mb = current
        if current < self.required_mb:
            self.parameter_path.write_text(f"{self.required_mb}\n")
            self._changed = True
        return self.check()

    def check(self) -> CheckResult:
        try:
            current = self._read()
        except (OSError, ValueError) as error:
            return CheckResult("usbfs", CheckPhase.SYSTEM, False, str(error))
        return CheckResult(
            "usbfs",
            CheckPhase.SYSTEM,
            current >= self.required_mb,
            f"usbfs_memory_mb={current}, required>={self.required_mb}",
        )

    def restore(self) -> CheckResult:
        if self._changed and self._original_mb is not None:
            self.parameter_path.write_text(f"{self._original_mb}\n")
            self._changed = False
        current = self._read()
        return CheckResult(
            "usbfs_restore",
            CheckPhase.SHUTDOWN,
            self._original_mb is None or current == self._original_mb,
            f"usbfs_memory_mb={current}",
        )

    def _read(self) -> int:
        return int(self.parameter_path.read_text().strip())


class Usb2CanResolver:
    def __init__(
        self,
        device_root: Path = Path("/dev"),
        runner: CommandRunner = default_runner,
    ) -> None:
        self.device_root = device_root
        self.runner = runner

    def resolve(self, serial_number: str) -> Path:
        matches = []
        for path in sorted(self.device_root.glob("ttyACM*")):
            result = self.runner(
                ["udevadm", "info", "--query=property", f"--name={path}"],
                check=False,
                capture_output=True,
                text=True,
            )
            properties = _properties(result.stdout) if result.returncode == 0 else {}
            if properties.get("ID_SERIAL_SHORT") == serial_number:
                matches.append(path)
        if len(matches) != 1:
            raise RuntimeError(
                f"USB2CAN serial {serial_number} resolved to {len(matches)} devices"
            )
        return matches[0]


class CanInterfaceManager:
    def __init__(
        self,
        arm: ArmConfig,
        tty_path: Path,
        log_dir: Path,
        runner: CommandRunner = default_runner,
        startup_timeout_s: float = 3.0,
    ) -> None:
        self.arm = arm
        self.tty_path = tty_path
        self.runner = runner
        self.startup_timeout_s = startup_timeout_s
        self.process = ManagedProcess(
            ProcessSpec(
                name=f"slcand-{arm.role}",
                argv=(
                    "slcand",
                    "-F",
                    "-o",
                    "-c",
                    "-f",
                    "-s8",
                    str(tty_path),
                    arm.can_interface,
                ),
                log_path=log_dir / f"slcand-{arm.role}.log",
            )
        )

    def start(self) -> CheckResult:
        if self._link().returncode == 0:
            raise RuntimeError(
                f"refusing to adopt existing CAN interface {self.arm.can_interface}"
            )
        self.process.start()
        try:
            deadline = monotonic() + self.startup_timeout_s
            while monotonic() < deadline:
                self.process.require_running()
                if self._link().returncode == 0:
                    break
                sleep(0.05)
            else:
                raise TimeoutError(f"slcand did not create {self.arm.can_interface}")
            self.runner(
                ["ip", "link", "set", self.arm.can_interface, "up"],
                check=True,
                capture_output=True,
                text=True,
            )
            result = self.check()
            if not result.passed:
                raise RuntimeError(result.detail)
            return result
        except BaseException:
            self.process.stop(interrupt_timeout_s=1.0, terminate_timeout_s=1.0)
            raise

    def check(self) -> CheckResult:
        if not self.process.running:
            return CheckResult(
                f"can_{self.arm.role}",
                CheckPhase.SYSTEM,
                False,
                "managed slcand is not running",
            )
        result = self.runner(
            [
                "ip",
                "-json",
                "-details",
                "-statistics",
                "link",
                "show",
                self.arm.can_interface,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return CheckResult(
                f"can_{self.arm.role}", CheckPhase.SYSTEM, False, result.stderr.strip()
            )
        entries = json.loads(result.stdout)
        entry = entries[0] if entries else {}
        is_up = "UP" in entry.get("flags", [])
        errors = _can_errors(entry)
        return CheckResult(
            f"can_{self.arm.role}",
            CheckPhase.SYSTEM,
            is_up and errors == 0,
            f"{self.arm.can_interface} up={is_up} errors={errors} tty={self.tty_path}",
        )

    def stop(self) -> ProcessExit:
        self.runner(
            ["ip", "link", "set", self.arm.can_interface, "down"],
            check=False,
            capture_output=True,
            text=True,
        )
        return self.process.stop(interrupt_timeout_s=1.0, terminate_timeout_s=1.0)

    def _link(self) -> subprocess.CompletedProcess[str]:
        return self.runner(
            ["ip", "link", "show", self.arm.can_interface],
            check=False,
            capture_output=True,
            text=True,
        )


class SystemBringup:
    def __init__(
        self,
        station: StationConfig,
        log_dir: Path,
        usbfs: UsbfsManager | None = None,
        resolver: Usb2CanResolver | None = None,
        interface_factory: Callable[[ArmConfig, Path, Path], CanInterfaceManager]
        | None = None,
    ) -> None:
        self.station = station
        self.log_dir = log_dir
        self.usbfs = usbfs or UsbfsManager()
        self.resolver = resolver or Usb2CanResolver()
        self.interface_factory = interface_factory or (
            lambda arm, tty, logs: CanInterfaceManager(arm, tty, logs)
        )
        self.interfaces: list[CanInterfaceManager] = []

    def start(self) -> tuple[CheckResult, ...]:
        results = [self.usbfs.apply()]
        if not results[-1].passed:
            raise RuntimeError(results[-1].detail)
        try:
            for arm in self.station.arms:
                tty_path = self.resolver.resolve(arm.usb_serial)
                interface = self.interface_factory(arm, tty_path, self.log_dir)
                results.append(interface.start())
                if not results[-1].passed:
                    raise RuntimeError(results[-1].detail)
                self.interfaces.append(interface)
            return tuple(results)
        except BaseException:
            self.stop()
            raise

    def check(self) -> tuple[CheckResult, ...]:
        return (self.usbfs.check(), *(interface.check() for interface in self.interfaces))

    def stop(self) -> tuple[CheckResult, ...]:
        errors: list[str] = []
        for interface in reversed(self.interfaces):
            try:
                interface.stop()
            except BaseException as error:
                errors.append(f"{interface.arm.role}: {error}")
        self.interfaces.clear()
        try:
            restore = self.usbfs.restore()
        except BaseException as error:
            restore = CheckResult("usbfs_restore", CheckPhase.SHUTDOWN, False, str(error))
        result = CheckResult(
            "system_cleanup",
            CheckPhase.SHUTDOWN,
            not errors and restore.passed,
            "; ".join(errors) if errors else restore.detail,
        )
        return (restore, result)


def _properties(output: str) -> dict[str, str]:
    return {
        key: value
        for line in output.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def _can_errors(entry: dict[str, Any]) -> int:
    stats = entry.get("stats64") or entry.get("stats") or {}
    receive = stats.get("rx") or {}
    transmit = stats.get("tx") or {}
    return int(receive.get("errors", 0)) + int(transmit.get("errors", 0))
