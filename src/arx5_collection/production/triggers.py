from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

from arx5_collection.episode.adapters.keyboard import KeyboardTrigger
from arx5_collection.episode.adapters.pedal import (
    PedalDeviceResolver,
    PedalTrigger,
    PedalUnavailable,
)
from arx5_collection.episode.ports import RecordTrigger

from .config import StationConfig


StatusSink = Callable[[str], None]


@contextmanager
def open_configured_pedals(
    station: StationConfig,
    resolver: PedalDeviceResolver,
) -> Iterator[PedalTrigger]:
    if station.triggers is None:
        raise PedalUnavailable("station configuration has no pedal pair")
    devices = resolver.resolve(
        station.triggers.activate,
        station.triggers.abort,
    )
    with PedalTrigger(devices) as trigger:
        yield trigger


class AutoTriggerFactory:
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
    def open(self, station: StationConfig) -> Iterator[RecordTrigger]:
        try:
            with open_configured_pedals(station, self.resolver) as trigger:
                self.status_sink("TRIGGER_MODE=pedal")
                yield trigger
                return
        except PedalUnavailable as error:
            reason = str(error)

        self.status_sink(f"TRIGGER_MODE=keyboard-fallback reason={reason}")
        with KeyboardTrigger(stream=self.keyboard_stream) as keyboard:
            yield keyboard
