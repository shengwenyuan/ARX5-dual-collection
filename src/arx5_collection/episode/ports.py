from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .models import StreamMetrics, StreamSpec


class TriggerEvent(str, Enum):
    ACTIVATE = "activate"
    ABORT = "abort"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class TriggerSignal:
    event: TriggerEvent
    monotonic_time_ns: int
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.monotonic_time_ns < 0:
            raise ValueError("monotonic_time_ns must not be negative")
        if self.event is TriggerEvent.FAIL and not self.detail:
            raise ValueError("fail trigger requires detail")
        if self.event is not TriggerEvent.FAIL and self.detail is not None:
            raise ValueError("only fail trigger may include detail")


@runtime_checkable
class RecordTrigger(Protocol):
    def wait(self, timeout_s: float) -> TriggerSignal | None:
        """Return the trigger event received before the timeout, if any."""
        ...


@runtime_checkable
class RecordingBackend(Protocol):
    def start(self, mcap_path: Path, streams: tuple[StreamSpec, ...]) -> None:
        """Start recording the configured streams into one MCAP file."""
        ...

    def stop(self) -> None:
        """Stop recording and close the MCAP file."""
        ...


@runtime_checkable
class StreamMonitor(Protocol):
    def start(self, streams: tuple[StreamSpec, ...]) -> None:
        """Start monitoring the configured streams."""
        ...

    def required_failure(self) -> str | None:
        """Return the current required-stream failure, if any."""
        ...

    def stop(self) -> tuple[StreamMetrics, ...]:
        """Stop monitoring and return final metrics."""
        ...
