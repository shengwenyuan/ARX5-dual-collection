from __future__ import annotations

import select
import sys
import termios
import time
import tty
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import TextIO

from arx5_collection.collection.episode.adapters.pedal import (
    PedalDeviceResolver,
    PedalUnavailable,
)
from arx5_collection.collection.episode.ports import RecordTrigger, TriggerEvent
from arx5_collection.collection.runtime.config import StationConfig
from arx5_collection.collection.runtime.triggers import open_configured_pedals

from .models import DaggerTriggerEvent, DaggerTriggerSignal
from .ports import DaggerTrigger
from arx5_collection.collection.environment import ENVIRONMENT


StatusSink = Callable[[str], None]


class DaggerPedalTriggerAdapter:
    """Apply the DAgger trigger profile to the existing bound pedal pair."""

    def __init__(self, trigger: RecordTrigger) -> None:
        self.trigger = trigger

    def arm(self) -> None:
        self.trigger.arm()

    def disarm(self) -> None:
        self.trigger.disarm()

    def wait(self, timeout_s: float) -> DaggerTriggerSignal | None:
        signal = self.trigger.wait(timeout_s)
        if signal is None:
            return None
        if signal.event is TriggerEvent.ACTIVATE:
            return DaggerTriggerSignal(
                DaggerTriggerEvent.RECORD_TOGGLE,
                signal.monotonic_time_ns,
            )
        if signal.event is TriggerEvent.ABORT:
            return DaggerTriggerSignal(
                DaggerTriggerEvent.OWNERSHIP_TOGGLE,
                signal.monotonic_time_ns,
            )
        return None


class DaggerKeyboardTrigger:
    """Keyboard fallback: SPACE records, T transfers ownership, A aborts."""

    def __init__(
        self,
        stream: TextIO | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stream = stream or sys.stdin
        self.monotonic_clock = monotonic_clock
        self._original_settings: list[object] | None = None
        self._armed = False

    def __enter__(self) -> DaggerKeyboardTrigger:
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

    def wait(self, timeout_s: float) -> DaggerTriggerSignal | None:
        if self._original_settings is None:
            raise RuntimeError("keyboard trigger must be used as a context manager")
        if not self._armed:
            raise RuntimeError("keyboard trigger is disarmed")
        readable, _, _ = select.select([self.stream], [], [], timeout_s)
        if not readable:
            return None
        key = self.stream.read(1)
        monotonic_time_ns = round(self.monotonic_clock() * 1e9)
        if key == ENVIRONMENT.trigger.keyboard_activate:
            return DaggerTriggerSignal(
                DaggerTriggerEvent.RECORD_TOGGLE,
                monotonic_time_ns,
            )
        if key.lower() == ENVIRONMENT.trigger.keyboard_ownership.lower():
            return DaggerTriggerSignal(
                DaggerTriggerEvent.OWNERSHIP_TOGGLE,
                monotonic_time_ns,
            )
        if key.lower() == ENVIRONMENT.trigger.keyboard_abort.lower():
            return DaggerTriggerSignal(DaggerTriggerEvent.ABORT, monotonic_time_ns)
        return None


class DaggerAutoTriggerFactory:
    def __init__(
        self,
        resolver: PedalDeviceResolver | None = None,
        keyboard_stream: TextIO | None = None,
        status_sink: StatusSink | None = None,
    ) -> None:
        self.resolver = resolver or PedalDeviceResolver()
        self.keyboard_stream = keyboard_stream or sys.stdin
        self.status_sink = status_sink or (lambda message: None)

    @contextmanager
    def open(self, station: StationConfig) -> Iterator[DaggerTrigger]:
        try:
            with open_configured_pedals(station, self.resolver) as pedal:
                self.status_sink("DAGGER_TRIGGER_MODE=pedal")
                yield DaggerPedalTriggerAdapter(pedal)
                return
        except PedalUnavailable as error:
            reason = str(error)

        self.status_sink(f"DAGGER_TRIGGER_MODE=keyboard-fallback reason={reason}")
        with DaggerKeyboardTrigger(self.keyboard_stream) as keyboard:
            yield keyboard
