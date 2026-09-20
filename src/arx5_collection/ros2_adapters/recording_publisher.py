from __future__ import annotations

from time import monotonic, sleep
from typing import Any
from uuid import uuid4


class RosRecordingPublisher:
    """Keep the ROS node/context open, but give each Recorder a fresh writer.

    RosbagRecordingBackend owns start/wait/stop_episode. These publishers only
    record facts; they never publish robot commands or own a scheduler thread.
    """

    def __init__(
        self, topic: str, message_name: str, node_prefix: str, depth: int,
        require_system_clock: bool = False,
    ) -> None:
        if not topic.startswith("/"):
            raise ValueError("recording publisher topic must be absolute")
        self.topic = topic
        self.message_name = message_name
        self.node_prefix = node_prefix
        self.depth = depth
        self.require_system_clock = require_system_clock
        self._context: Any = None
        self._node: Any = None
        self._publisher: Any = None
        self._message_type: Any = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def open(self) -> None:
        if self._context is not None:
            raise RuntimeError("recording publisher is already open")
        import rclpy
        from arx5_collection_interfaces import msg

        self._context = rclpy.Context()
        try:
            rclpy.init(context=self._context)
            self._node = rclpy.create_node(
                f"{self.node_prefix}_{uuid4().hex[:8]}", context=self._context
            )
            if self.require_system_clock and self._node.get_parameter("use_sim_time").value:
                raise RuntimeError("infer command timestamps require the host system clock")
            self._message_type = getattr(msg, self.message_name)
        except BaseException:
            self.close()
            raise

    def start_episode(self) -> None:
        if self._node is None or self._message_type is None:
            raise RuntimeError("recording publisher is not open")
        if self._publisher is not None:
            raise RuntimeError("recording publisher episode is already active")
        from rclpy.qos import QoSProfile, ReliabilityPolicy

        self._publisher = self._node.create_publisher(
            self._message_type, self.topic,
            QoSProfile(depth=self.depth, reliability=ReliabilityPolicy.RELIABLE),
        )

    def wait_for_recorder(self, recorder_name: str, timeout_s: float) -> None:
        if self._publisher is None:
            raise RuntimeError("recording publisher episode is not active")
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            readers = self._node.get_subscriptions_info_by_topic(self.topic)
            # A graph endpoint alone is insufficient. Require RMW matches too;
            # neither API is an acknowledgement that a particular sample arrived.
            if (
                any(info.node_name == recorder_name for info in readers)
                and self._publisher.get_subscription_count() >= len(readers)
            ):
                return
            sleep(min(0.01, max(0.0, deadline - monotonic())))
        raise TimeoutError(f"recording publisher {self.topic} did not match {recorder_name}")

    def stop_episode(self) -> None:
        if self._publisher is not None:
            publisher, self._publisher = self._publisher, None
            if not self._node.destroy_publisher(publisher):
                raise RuntimeError(f"failed to destroy recording publisher {self.topic}")

    def close(self) -> None:
        try:
            self.stop_episode()
        finally:
            try:
                if self._node is not None:
                    self._node.destroy_node()
            finally:
                if self._context is not None and self._context.ok():
                    self._context.shutdown()
                self._publisher = self._node = self._context = self._message_type = None
