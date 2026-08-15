from __future__ import annotations

from pathlib import Path

from arx5_collection.episode import ports
from arx5_collection.episode.models import StreamMetrics, StreamSpec


class Trigger:
    def wait(self, timeout_s: float) -> bool:
        return True


class Backend:
    def start(self, mcap_path: Path, streams: tuple[StreamSpec, ...]) -> None:
        self.mcap_path = mcap_path

    def stop(self) -> None:
        self.closed = True


class Monitor:
    def start(self, streams: tuple[StreamSpec, ...]) -> None:
        self.stream = streams[0]

    def required_failure(self) -> str | None:
        return None

    def stop(self) -> tuple[StreamMetrics, ...]:
        return (StreamMetrics(self.stream.id, 30, 1.0, 30.0, 35.0),)


def main() -> None:
    stream = StreamSpec("camera_front", "/sensors/camera_front/color", True, 30.0)
    trigger = Trigger()
    backend = Backend()
    monitor = Monitor()

    assert isinstance(trigger, ports.RecordTrigger)
    assert isinstance(backend, ports.RecordingBackend)
    assert isinstance(monitor, ports.StreamMonitor)

    backend.start(Path("episode.mcap"), (stream,))
    monitor.start((stream,))
    assert trigger.wait(0.1)
    assert monitor.required_failure() is None
    backend.stop()
    metrics = monitor.stop()

    assert backend.closed
    assert metrics[0].id == stream.id
    print(f"installed_from={ports.__file__}")
    print("episode_ports_link=ok")


if __name__ == "__main__":
    main()
