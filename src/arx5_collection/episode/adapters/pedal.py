from __future__ import annotations

import os
import select
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol

from ..ports import TriggerEvent


PRESS_REPORT = bytes.fromhex("66cc030001" + "00" * 59)
VENDOR_REPORT_DESCRIPTOR_PREFIX = bytes.fromhex("0600ff")


class PedalUnavailable(RuntimeError):
    """The configured pedal pair cannot be opened for this Session."""


class PedalBinding(Protocol):
    role: str
    vendor_id: str
    product_id: str
    serial_number: str


@dataclass(frozen=True, slots=True)
class HidrawPedal:
    path: Path
    file_descriptor: int


SelectFunction = Callable[
    [Sequence[int], Sequence[int], Sequence[int], float],
    tuple[Sequence[int], Sequence[int], Sequence[int]],
]


class PedalDeviceResolver:
    def __init__(
        self,
        sysfs_root: Path = Path("/sys/class/hidraw"),
        device_root: Path = Path("/dev"),
        open_function: Callable[[str | Path, int], int] = os.open,
        close_function: Callable[[int], None] = os.close,
    ) -> None:
        self.sysfs_root = sysfs_root
        self.device_root = device_root
        self.open_function = open_function
        self.close_function = close_function

    def resolve(
        self,
        activate: PedalBinding,
        abort: PedalBinding,
    ) -> dict[TriggerEvent, HidrawPedal]:
        paths = {
            TriggerEvent.ACTIVATE: self._match(activate),
            TriggerEvent.ABORT: self._match(abort),
        }
        opened: dict[TriggerEvent, HidrawPedal] = {}
        try:
            for event, path in paths.items():
                opened[event] = HidrawPedal(
                    path=path,
                    file_descriptor=self.open_function(
                        path,
                        os.O_RDONLY | os.O_NONBLOCK,
                    ),
                )
        except OSError as error:
            for pedal in opened.values():
                self.close_function(pedal.file_descriptor)
            raise PedalUnavailable(f"cannot open configured pedals: {error}") from error
        return opened

    def _match(self, config: PedalBinding) -> Path:
        matches = []
        try:
            for node in sorted(self.sysfs_root.glob("hidraw*")):
                properties = _properties(node / "device" / "uevent")
                descriptor = (node / "device" / "report_descriptor").read_bytes()
                if (
                    properties.get("HID_ID") == _hid_id(config)
                    and properties.get("HID_UNIQ") == config.serial_number
                    and descriptor.startswith(VENDOR_REPORT_DESCRIPTOR_PREFIX)
                ):
                    matches.append(self.device_root / node.name)
        except OSError as error:
            raise PedalUnavailable(f"cannot inspect hidraw pedals: {error}") from error
        if len(matches) != 1:
            raise PedalUnavailable(
                f"trigger {config.role} expected one hidraw pedal "
                f"{config.vendor_id}:{config.product_id}/{config.serial_number}, "
                f"found {len(matches)}"
            )
        return matches[0]


class PedalTrigger:
    def __init__(
        self,
        devices: Mapping[TriggerEvent, HidrawPedal],
        debounce_s: float = 0.2,
        select_function: SelectFunction = select.select,
        read_function: Callable[[int, int], bytes] = os.read,
        close_function: Callable[[int], None] = os.close,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if set(devices) != {TriggerEvent.ACTIVATE, TriggerEvent.ABORT}:
            raise ValueError("pedal trigger requires activate and abort devices")
        if debounce_s < 0:
            raise ValueError("debounce_s must not be negative")
        self.devices = dict(devices)
        self.debounce_s = debounce_s
        self.select_function = select_function
        self.read_function = read_function
        self.close_function = close_function
        self.monotonic_clock = monotonic_clock
        self._events_by_fd = {
            pedal.file_descriptor: event for event, pedal in self.devices.items()
        }
        if len(self._events_by_fd) != 2:
            raise ValueError("pedal trigger devices must have different file descriptors")
        self._last_press = {
            TriggerEvent.ACTIVATE: float("-inf"),
            TriggerEvent.ABORT: float("-inf"),
        }
        self._entered = False

    def __enter__(self) -> PedalTrigger:
        self._entered = True
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if not self._entered:
            return
        for pedal in self.devices.values():
            try:
                self.close_function(pedal.file_descriptor)
            except OSError:
                pass
        self._entered = False

    def wait(self, timeout_s: float) -> TriggerEvent | None:
        if not self._entered:
            raise RuntimeError("pedal trigger must be used as a context manager")
        if timeout_s < 0:
            raise ValueError("timeout_s must not be negative")
        try:
            readable, _, _ = self.select_function(
                tuple(self._events_by_fd), (), (), timeout_s
            )
            candidates = set()
            for file_descriptor in readable:
                report = self.read_function(file_descriptor, len(PRESS_REPORT))
                if not report:
                    raise RuntimeError("pedal disconnected")
                if report == PRESS_REPORT:
                    candidates.add(self._events_by_fd[file_descriptor])
        except OSError as error:
            raise RuntimeError(f"pedal disconnected or unreadable: {error}") from error

        now = self.monotonic_clock()
        if TriggerEvent.ABORT in candidates:
            if now - self._last_press[TriggerEvent.ABORT] >= self.debounce_s:
                self._last_press[TriggerEvent.ABORT] = now
                return TriggerEvent.ABORT
            return None
        if (
            TriggerEvent.ACTIVATE in candidates
            and now - self._last_press[TriggerEvent.ACTIVATE] >= self.debounce_s
        ):
            self._last_press[TriggerEvent.ACTIVATE] = now
            return TriggerEvent.ACTIVATE
        return None


def _properties(path: Path) -> dict[str, str]:
    properties = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    return properties


def _hid_id(config: PedalBinding) -> str:
    return (
        f"0003:0000{config.vendor_id.upper()}:"
        f"0000{config.product_id.upper()}"
    )
