from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from arx5_collection.episode.adapters.composite import CompositeTrigger
from arx5_collection.episode.adapters.keyboard import KeyboardTrigger
from arx5_collection.episode.adapters.pedal import (
    PedalDeviceResolver,
    PedalTrigger,
    PedalUnavailable,
)
from arx5_collection.episode.adapters.remote import UnixSocketTrigger
from arx5_collection.episode.ports import RecordTrigger

from .config import StationConfig


StatusSink = Callable[[str], None]


class AutoTriggerFactory:
    def __init__(
        self,
        resolver: PedalDeviceResolver | None = None,
        keyboard_stream: TextIO | None = None,
        status_sink: StatusSink | None = None,
        remote_socket: Path | None = None,
    ) -> None:
        self.resolver = resolver or PedalDeviceResolver()
        self.keyboard_stream = keyboard_stream or sys.stdin
        self.status_sink = status_sink or (lambda message: None)
        self.remote_socket = remote_socket

    @contextmanager
    def open(self, station: StationConfig) -> Iterator[RecordTrigger]:
        reason = "station configuration has no pedal pair"
        if self.remote_socket is not None:
            with UnixSocketTrigger(self.remote_socket) as remote:
                if station.triggers is not None:
                    try:
                        devices = self.resolver.resolve(
                            station.triggers.activate,
                            station.triggers.abort,
                        )
                        with PedalTrigger(devices) as pedal:
                            self.status_sink("TRIGGER_MODE=pedal+remote")
                            yield CompositeTrigger((pedal, remote))
                            return
                    except PedalUnavailable as error:
                        reason = str(error)
                self.status_sink(f"TRIGGER_MODE=remote reason={reason}")
                yield remote
                return

        if station.triggers is not None:
            try:
                devices = self.resolver.resolve(
                    station.triggers.activate,
                    station.triggers.abort,
                )
                with PedalTrigger(devices) as trigger:
                    self.status_sink("TRIGGER_MODE=pedal")
                    yield trigger
                    return
            except PedalUnavailable as error:
                reason = str(error)

        self.status_sink(f"TRIGGER_MODE=keyboard-fallback reason={reason}")
        with KeyboardTrigger(stream=self.keyboard_stream) as keyboard:
            yield keyboard
