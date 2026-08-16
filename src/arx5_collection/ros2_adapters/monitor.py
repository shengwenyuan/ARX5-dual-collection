from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from threading import Lock, Thread
from time import monotonic
from typing import Any

from arx5_collection.episode.models import StreamMetrics, StreamSpec

from .health import StatusSample, StreamHealthTracker
from .recording import RosbagRecordingBackend


class RosStreamMonitor:
    def __init__(
        self,
        backend: RosbagRecordingBackend,
        status_sink: Callable[[str], None] | None = print,
        display_period_s: float = 1.0,
        startup_grace_s: float = 3.0,
        heartbeat_timeout_s: float = 2.5,
        data_silence_timeout_s: float = 2.0,
        warning_ratio: float = 0.9,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if display_period_s <= 0:
            raise ValueError("display_period_s must be positive")
        self.backend = backend
        self.status_sink = status_sink
        self.display_period_s = display_period_s
        self.startup_grace_s = startup_grace_s
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.data_silence_timeout_s = data_silence_timeout_s
        self.warning_ratio = warning_ratio
        self.clock = clock
        self._lock = Lock()
        self._health: StreamHealthTracker | None = None
        self._streams: tuple[StreamSpec, ...] = ()
        self._next_display_s = 0.0
        self._context: Any | None = None
        self._node: Any | None = None
        self._executor: Any | None = None
        self._thread: Thread | None = None
        self._spin_error: BaseException | None = None

    def start(self, streams: tuple[StreamSpec, ...]) -> None:
        if self._health is not None:
            raise RuntimeError("stream monitor is already active")
        import rclpy
        from arx5_collection_interfaces.msg import StreamStatus
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import (
            QoSDurabilityPolicy,
            QoSHistoryPolicy,
            QoSProfile,
            QoSReliabilityPolicy,
        )

        started_s = self.clock()
        self._streams = streams
        self._health = StreamHealthTracker(
            streams,
            started_s,
            startup_grace_s=self.startup_grace_s,
            heartbeat_timeout_s=self.heartbeat_timeout_s,
            data_silence_timeout_s=self.data_silence_timeout_s,
            warning_ratio=self.warning_ratio,
        )
        self._next_display_s = started_s + self.display_period_s
        self._spin_error = None
        self._context = rclpy.Context()
        rclpy.init(context=self._context)
        self._node = rclpy.create_node(
            "episode_stream_monitor",
            context=self._context,
        )
        qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=64,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self._node.create_subscription(
            StreamStatus,
            "/monitoring/stream_status",
            self._status_callback,
            qos,
        )
        self._executor = SingleThreadedExecutor(context=self._context)
        self._executor.add_node(self._node)
        self._thread = Thread(
            target=self._spin,
            name="episode-stream-monitor",
            daemon=False,
        )
        self._thread.start()

    def _status_callback(self, message: Any) -> None:
        sample = StatusSample(
            stream_id=message.stream_id,
            topic=message.topic,
            total_count=int(message.total_count),
            window_count=int(message.window_count),
            observed_hz=float(message.observed_hz),
            max_gap_ms=float(message.max_gap_ms),
            silence_s=float(message.silence_s),
            non_monotonic_count=int(message.non_monotonic_count),
        )
        with self._lock:
            assert self._health is not None
            self._health.observe(sample, self.clock())

    def _spin(self) -> None:
        try:
            assert self._executor is not None
            self._executor.spin()
        except BaseException as error:
            self._spin_error = error

    def required_failure(self) -> str | None:
        if self._health is None:
            raise RuntimeError("stream monitor is not active")
        if self._spin_error is not None:
            return f"stream monitor failed: {self._spin_error}"
        now_s = self.clock()
        with self._lock:
            failure = self._health.required_failure(now_s)
            if self.status_sink is not None and now_s >= self._next_display_s:
                self.status_sink(self._health.display())
                self._next_display_s = now_s + self.display_period_s
            return failure

    def stop(self) -> tuple[StreamMetrics, ...]:
        if self._health is None:
            raise RuntimeError("stream monitor is not active")
        health = self._health
        streams = self._streams
        self._shutdown_ros()
        if self._spin_error is not None:
            raise RuntimeError("stream monitor executor failed") from self._spin_error
        metrics = self.backend.metrics(streams)
        merged = tuple(
            replace(
                metric,
                warnings=tuple(dict.fromkeys(metric.warnings + health.warnings_for(metric.id))),
            )
            for metric in metrics
        )
        self._health = None
        self._streams = ()
        return merged

    def _shutdown_ros(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(timeout_sec=5.0)
        if self._node is not None:
            self._node.destroy_node()
        if self._context is not None and self._context.ok():
            self._context.shutdown()
        if self._thread is not None:
            self._thread.join(5.0)
            if self._thread.is_alive():
                raise TimeoutError("stream monitor thread did not stop")
        self._executor = None
        self._node = None
        self._context = None
        self._thread = None
