from __future__ import annotations

import select
import sys
import termios
import time
import tty
from collections.abc import Callable
from types import TracebackType
from typing import TextIO

from ..ports import TriggerEvent, TriggerSignal


class KeyboardTrigger:
    def __init__(
        self,
        stream: TextIO | None = None,
        key: str = " ",
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(key) != 1:
            raise ValueError("trigger key must be one character")
        self.stream = stream or sys.stdin
        self.key = key
        self.monotonic_clock = monotonic_clock
        self._original_settings: list[object] | None = None
        self._armed = False

    def __enter__(self) -> KeyboardTrigger:
        if not self.stream.isatty():
            raise RuntimeError("keyboard trigger requires a TTY")
        file_descriptor = self.stream.fileno()
        self._original_settings = termios.tcgetattr(file_descriptor)
        tty.setcbreak(file_descriptor)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._original_settings is not None:
            termios.tcsetattr(
                self.stream.fileno(),
                termios.TCSADRAIN,
                self._original_settings,
            )
            self._original_settings = None
            self._armed = False

    def arm(self) -> None:
        if self._original_settings is None:
            raise RuntimeError("keyboard trigger must be used as a context manager")
        termios.tcflush(self.stream.fileno(), termios.TCIFLUSH)
        self._armed = True

    def disarm(self) -> None:
        self._armed = False

    def wait(self, timeout_s: float) -> TriggerSignal | None:
        if self._original_settings is None:
            raise RuntimeError("keyboard trigger must be used as a context manager")
        if not self._armed:
            raise RuntimeError("keyboard trigger is disarmed")
        readable, _, _ = select.select([self.stream], [], [], timeout_s)
        if not readable:
            return None
        key = self.stream.read(1)
        monotonic_time_ns = round(self.monotonic_clock() * 1e9)
        if key == self.key:
            return TriggerSignal(TriggerEvent.ACTIVATE, monotonic_time_ns)
        if key in {"a", "A"}:
            return TriggerSignal(TriggerEvent.ABORT, monotonic_time_ns)
        return None
