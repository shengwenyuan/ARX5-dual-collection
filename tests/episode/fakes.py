from __future__ import annotations

from pathlib import Path

from arx5_collection.episode.models import StreamMetrics, StreamSpec


class FakeTrigger:
    def __init__(self, events: list[bool | BaseException]) -> None:
        self.events = iter(events)

    def wait(self, timeout_s: float) -> bool:
        event = next(self.events, False)
        if isinstance(event, BaseException):
            raise event
        return event


class FakeBackend:
    def __init__(self, stop_error: Exception | None = None) -> None:
        self.stop_error = stop_error
        self.mcap_path: Path | None = None
        self.stop_count = 0

    def start(self, mcap_path: Path, streams: tuple[StreamSpec, ...]) -> None:
        self.mcap_path = mcap_path

    def stop(self) -> None:
        self.stop_count += 1
        if self.stop_error is not None:
            raise self.stop_error
        assert self.mcap_path is not None
        self.mcap_path.write_bytes(b"mcap")


class FakeMonitor:
    def __init__(
        self,
        metrics: tuple[StreamMetrics, ...],
        failure: str | None = None,
    ) -> None:
        self.metrics = metrics
        self.failure = failure
        self.stop_count = 0

    def start(self, streams: tuple[StreamSpec, ...]) -> None:
        self.streams = streams

    def required_failure(self) -> str | None:
        return self.failure

    def stop(self) -> tuple[StreamMetrics, ...]:
        self.stop_count += 1
        return self.metrics
