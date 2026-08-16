from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import Lock, Thread
from time import monotonic, sleep
from typing import Any

from .checks import CheckFailure, CheckPhase, CheckResult, require_passed
from .config import EXPECTED_STREAMS


EXPECTED_TOPIC_TYPES = {
    stream_id: (
        "arx5_collection_interfaces/msg/ArmState"
        if stream_id.endswith("_arm_state")
        else "sensor_msgs/msg/Image"
    )
    for stream_id in EXPECTED_STREAMS
}


@dataclass(frozen=True, slots=True)
class StreamObservation:
    stream_id: str
    topic: str
    total_count: int
    received_at_s: float


class ReadinessLedger:
    def __init__(self) -> None:
        self._lock = Lock()
        self._observations: dict[str, StreamObservation] = {}

    def observe(
        self,
        stream_id: str,
        topic: str,
        total_count: int,
        received_at_s: float,
    ) -> None:
        if stream_id not in EXPECTED_STREAMS:
            return
        with self._lock:
            self._observations[stream_id] = StreamObservation(
                stream_id, topic, total_count, received_at_s
            )

    def check(
        self,
        stream_id: str,
        topic_types: dict[str, tuple[str, ...]],
        now_s: float,
        heartbeat_timeout_s: float,
    ) -> CheckResult:
        with self._lock:
            observation = self._observations.get(stream_id)
        expected_topic = EXPECTED_STREAMS[stream_id]
        expected_type = EXPECTED_TOPIC_TYPES[stream_id]
        actual_types = topic_types.get(expected_topic, ())
        age_s = None if observation is None else now_s - observation.received_at_s
        passed = (
            observation is not None
            and observation.topic == expected_topic
            and observation.total_count > 0
            and age_s is not None
            and 0 <= age_s <= heartbeat_timeout_s
            and actual_types == (expected_type,)
        )
        detail = (
            f"topic={expected_topic} count={None if observation is None else observation.total_count} "
            f"age_s={None if age_s is None else round(age_s, 3)} "
            f"types={list(actual_types)}"
        )
        return CheckResult(
            name=f"telemetry_{stream_id}",
            phase=CheckPhase.ROS,
            passed=passed,
            detail=detail,
        )


class RosReadinessGate:
    def __init__(
        self,
        heartbeat_timeout_s: float = 2.5,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if heartbeat_timeout_s <= 0:
            raise ValueError("heartbeat_timeout_s must be positive")
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.clock = clock
        self.ledger = ReadinessLedger()
        self._context: Any | None = None
        self._node: Any | None = None
        self._executor: Any | None = None
        self._thread: Thread | None = None
        self._spin_error: BaseException | None = None

    def start(self) -> None:
        if self._context is not None:
            raise RuntimeError("ROS readiness gate is already active")
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
        rclpy.init(context=self._context)
        self._node = rclpy.create_node("production_readiness_gate", context=self._context)
        qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=64,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self._node.create_subscription(
            StreamStatus, "/monitoring/stream_status", self._status_callback, qos
        )
        self._executor = SingleThreadedExecutor(context=self._context)
        self._executor.add_node(self._node)
        self._thread = Thread(
            target=self._spin, name="production-readiness-gate", daemon=False
        )
        self._thread.start()

    def wait_for(
        self,
        stream_ids: Iterable[str],
        timeout_s: float,
        process_check: Callable[[], None],
    ) -> tuple[CheckResult, ...]:
        requested = tuple(stream_ids)
        deadline = self.clock() + timeout_s
        last_results: tuple[CheckResult, ...] = ()
        while self.clock() < deadline:
            process_check()
            last_results = self.results(requested)
            if all(result.passed for result in last_results):
                return last_results
            sleep(0.1)
        if not last_results:
            last_results = self.results(requested)
        raise CheckFailure(last_results)

    def require_ready(self, stream_ids: Iterable[str] = EXPECTED_STREAMS) -> None:
        require_passed(self.results(stream_ids))

    def results(self, stream_ids: Iterable[str]) -> tuple[CheckResult, ...]:
        if self._spin_error is not None:
            return (
                CheckResult(
                    "readiness_executor",
                    CheckPhase.ROS,
                    False,
                    f"{type(self._spin_error).__name__}: {self._spin_error}",
                ),
            )
        if self._node is None:
            raise RuntimeError("ROS readiness gate is not active")
        topic_types = {
            topic: tuple(types)
            for topic, types in self._node.get_topic_names_and_types()
        }
        now_s = self.clock()
        return tuple(
            self.ledger.check(
                stream_id, topic_types, now_s, self.heartbeat_timeout_s
            )
            for stream_id in stream_ids
        )

    def stop(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(timeout_sec=5.0)
        if self._node is not None:
            self._node.destroy_node()
        if self._context is not None and self._context.ok():
            self._context.shutdown()
        if self._thread is not None:
            self._thread.join(5.0)
            if self._thread.is_alive():
                raise TimeoutError("ROS readiness gate did not stop")
        self._context = None
        self._node = None
        self._executor = None
        self._thread = None

    def _status_callback(self, message: Any) -> None:
        self.ledger.observe(
            str(message.stream_id),
            str(message.topic),
            int(message.total_count),
            self.clock(),
        )

    def _spin(self) -> None:
        try:
            assert self._executor is not None
            self._executor.spin()
        except BaseException as error:
            self._spin_error = error
