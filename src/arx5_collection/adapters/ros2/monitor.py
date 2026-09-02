from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Any

from arx5_collection.collection.episode.models import StreamMetrics, StreamSpec
from arx5_collection.collection.environment import ENVIRONMENT
from arx5_collection.common.specs import RUNTIME_INTERFACE_SPEC

from .health import StatusSample, StreamHealthTracker
from .recording import RosbagRecordingBackend


class RosStreamMonitor:
    """Subscribe once per Session and reset health baselines per Episode."""

    def __init__(
        self,
        backend: RosbagRecordingBackend,
        status_sink: Callable[[str], None] | None = print,
        display_period_s: float = ENVIRONMENT.monitor.display_period_s,
        startup_grace_s: float = ENVIRONMENT.monitor.startup_grace_s,
        heartbeat_timeout_s: float = ENVIRONMENT.monitor.heartbeat_timeout_s,
        data_silence_timeout_s: float = ENVIRONMENT.monitor.data_silence_timeout_s,
        warning_ratio: float = ENVIRONMENT.monitor.warning_ratio,
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
        self._latest: dict[str, tuple[StatusSample, float]] = {}
        self._health: StreamHealthTracker | None = None
        self._streams: tuple[StreamSpec, ...] = ()
        self._next_display_s = 0.0
        self._context: Any | None = None
        self._node: Any | None = None
        self._executor: Any | None = None
        self._thread: Thread | None = None
        self._spin_error: BaseException | None = None

    def open(self) -> None:
        if self._context is not None:
            raise RuntimeError("stream monitor is already open")
        import rclpy
        from arx5_collection_interfaces.msg import StreamStatus
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import (
            QoSDurabilityPolicy,
            QoSHistoryPolicy,
            QoSProfile,
            QoSReliabilityPolicy,
        )

        self._spin_error = None
        self._context = rclpy.Context()
        try:
            rclpy.init(context=self._context)
            self._node = rclpy.create_node(
                "session_stream_monitor",
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
                RUNTIME_INTERFACE_SPEC.stream_status_topic,
                self._status_callback,
                qos,
            )
            self._executor = SingleThreadedExecutor(context=self._context)
            self._executor.add_node(self._node)
            self._thread = Thread(
                target=self._spin,
                name="session-stream-monitor",
                daemon=False,
            )
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def wait_until_ready(
        self,
        stream_ids: tuple[str, ...],
        timeout_s: float,
        process_check: Callable[[], None],
    ) -> None:
        if self._context is None:
            raise RuntimeError("stream monitor is not open")
        deadline = self.clock() + timeout_s
        missing = set(stream_ids)
        while missing and self.clock() < deadline:
            process_check()
            self._require_spin()
            now_s = self.clock()
            with self._lock:
                missing = {
                    stream_id
                    for stream_id in stream_ids
                    if stream_id not in self._latest
                    or now_s - self._latest[stream_id][1] > self.heartbeat_timeout_s
                }
            if missing:
                sleep(0.05)
        if missing:
            raise TimeoutError(
                "session stream monitor did not receive fresh telemetry: "
                + ", ".join(sorted(missing))
            )

    def start(self, streams: tuple[StreamSpec, ...]) -> None:
        if self._context is None:
            raise RuntimeError("stream monitor is not open")
        if self._health is not None:
            raise RuntimeError("stream monitor is already active")
        started_s = self.clock()
        health = StreamHealthTracker(
            streams,
            started_s,
            startup_grace_s=self.startup_grace_s,
            heartbeat_timeout_s=self.heartbeat_timeout_s,
            data_silence_timeout_s=self.data_silence_timeout_s,
            warning_ratio=self.warning_ratio,
        )
        with self._lock:
            for stream in streams:
                cached = self._latest.get(stream.id)
                if cached is None:
                    continue
                sample, arrival_s = cached
                if started_s - arrival_s <= self.heartbeat_timeout_s:
                    health.observe(sample, arrival_s)
            self._health = health
            self._streams = streams
            self._next_display_s = started_s + self.display_period_s

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
        arrival_s = self.clock()
        with self._lock:
            self._latest[sample.stream_id] = (sample, arrival_s)
            if self._health is not None:
                self._health.observe(sample, arrival_s)

    def _spin(self) -> None:
        try:
            assert self._executor is not None
            self._executor.spin()
        except BaseException as error:
            self._spin_error = error

    def _require_spin(self) -> None:
        if self._spin_error is not None:
            raise RuntimeError("stream monitor executor failed") from self._spin_error

    def required_failure(self) -> str | None:
        if self._health is None:
            raise RuntimeError("stream monitor is not active")
        self._require_spin()
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
        self._require_spin()
        with self._lock:
            health = self._health
            streams = self._streams
            self._health = None
            self._streams = ()
        metrics = self.backend.metrics(streams)
        return tuple(
            replace(
                metric,
                warnings=tuple(
                    dict.fromkeys(metric.warnings + health.warnings_for(metric.id))
                ),
            )
            for metric in metrics
        )

    def close(self) -> None:
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
        with self._lock:
            self._latest.clear()
            self._health = None
            self._streams = ()
