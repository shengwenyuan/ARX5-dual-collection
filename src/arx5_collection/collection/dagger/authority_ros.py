from __future__ import annotations

from time import monotonic
from typing import Any
from uuid import uuid4

from arx5_collection.collection.runtime.profiles import TEACHING_ARM_PROFILE

from .takeover import AuthorityEvent
from .topics import AUTHORITY_TOPIC


ACTION_OUTPUT_TOPICS = (
    TEACHING_ARM_PROFILE.left_input_topic,
    TEACHING_ARM_PROFILE.right_input_topic,
)


def require_no_action_publishers(
    topics: tuple[str, ...] = ACTION_OUTPUT_TOPICS,
    discovery_timeout_s: float = 1.0,
) -> None:
    """Refuse dry-run when another process could command the DAgger controller."""
    if discovery_timeout_s <= 0:
        raise ValueError("ROS graph discovery timeout must be positive")
    import rclpy
    from rclpy.executors import SingleThreadedExecutor

    context = rclpy.Context()
    node = None
    executor = None
    try:
        rclpy.init(context=context)
        node = rclpy.create_node(
            f"dagger_no_action_guard_{uuid4().hex[:8]}",
            context=context,
        )
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)
        deadline = monotonic() + discovery_timeout_s
        while monotonic() < deadline:
            executor.spin_once(timeout_sec=min(0.1, deadline - monotonic()))
            publishers = {
                topic: node.get_publishers_info_by_topic(topic) for topic in topics
            }
            active = {
                topic: tuple(
                    f"{info.node_namespace}/{info.node_name}" for info in infos
                )
                for topic, infos in publishers.items()
                if infos
            }
            if active:
                raise RuntimeError(f"action publisher already active: {active}")
    finally:
        if executor is not None:
            executor.shutdown()
        if node is not None:
            node.destroy_node()
        if context.ok():
            context.shutdown()


class RosAuthorityEventPublisher:
    """Publish sparse authority transitions without owning an executor thread."""

    def __init__(self, topic: str = AUTHORITY_TOPIC) -> None:
        if not topic.startswith("/"):
            raise ValueError("authority event topic must be absolute")
        self.topic = topic
        self._context: Any | None = None
        self._node: Any | None = None
        self._publisher: Any | None = None
        self._message_type: Any | None = None

    def __enter__(self) -> RosAuthorityEventPublisher:
        self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def open(self) -> None:
        if self._context is not None:
            raise RuntimeError("authority event publisher is already open")
        import rclpy
        from arx5_collection_interfaces.msg import AuthorityEvent as RosAuthorityEvent
        from rclpy.qos import QoSProfile, ReliabilityPolicy

        self._context = rclpy.Context()
        rclpy.init(context=self._context)
        self._node = rclpy.create_node(
            f"dagger_authority_{uuid4().hex[:8]}",
            context=self._context,
        )
        self._publisher = self._node.create_publisher(
            RosAuthorityEvent,
            self.topic,
            QoSProfile(depth=32, reliability=ReliabilityPolicy.RELIABLE),
        )
        self._message_type = RosAuthorityEvent

    def close(self) -> None:
        if self._node is not None:
            self._node.destroy_node()
        if self._context is not None and self._context.ok():
            self._context.shutdown()
        self._publisher = None
        self._message_type = None
        self._node = None
        self._context = None

    def __call__(self, event: AuthorityEvent) -> None:
        if self._publisher is None or self._node is None or self._message_type is None:
            raise RuntimeError("authority event publisher is not open")
        message = self._message_type()
        message.header.stamp = self._node.get_clock().now().to_msg()
        message.sequence = event.sequence
        message.monotonic_time_ns = event.monotonic_time_ns
        message.intervention_id = event.intervention_id
        message.control_epoch = event.control_epoch
        message.event_type = int(event.event_type)
        message.reason = event.reason
        self._publisher.publish(message)
