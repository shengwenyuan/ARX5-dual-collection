from __future__ import annotations

from threading import Lock, Thread
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from .action_gateway import DualArmJointCommand, DualArmJointState
from .authority_ros import ACTION_OUTPUT_TOPICS
from arx5_collection.collection.capture import RGBD_STREAMS


CANONICAL_STATE_TOPICS = (
    RGBD_STREAMS["left_arm_state"],
    RGBD_STREAMS["right_arm_state"],
)


class RosDualArmControlPort:
    """Single ROS boundary for fresh feedback and paired Vendor commands."""

    def __init__(
        self,
        command_topics: tuple[str, str],
        state_topics: tuple[str, str] = CANONICAL_STATE_TOPICS,
        policy_enable_services: tuple[str, str] | None = None,
        state_timeout_s: float = 0.1,
        allow_vendor_commands: bool = False,
    ) -> None:
        if len(command_topics) != 2 or len(state_topics) != 2:
            raise ValueError("dual-arm ROS port requires two state and command topics")
        if policy_enable_services is not None and len(policy_enable_services) != 2:
            raise ValueError("dual-arm ROS port requires two policy enable services")
        if any(not topic.startswith("/") for topic in (*command_topics, *state_topics)):
            raise ValueError("dual-arm ROS topics must be absolute")
        if policy_enable_services is not None and any(
            not service.startswith("/") for service in policy_enable_services
        ):
            raise ValueError("dual-arm ROS services must be absolute")
        if state_timeout_s <= 0:
            raise ValueError("state timeout must be positive")
        if command_topics == ACTION_OUTPUT_TOPICS and not allow_vendor_commands:
            raise ValueError("Vendor command topics require explicit authorization")
        self.command_topics = command_topics
        self.state_topics = state_topics
        self.policy_enable_services = policy_enable_services
        self.state_timeout_s = state_timeout_s
        self._lock = Lock()
        self._states: dict[str, tuple[tuple[float, ...], float]] = {}
        self._context: Any | None = None
        self._node: Any | None = None
        self._executor: Any | None = None
        self._thread: Thread | None = None
        self._publishers: tuple[Any, Any] | None = None
        self._message_type: Any | None = None
        self._trigger_type: Any | None = None
        self._enable_clients: tuple[Any, Any] | None = None
        self._spin_error: BaseException | None = None

    def __enter__(self) -> RosDualArmControlPort:
        self.open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def open(self) -> None:
        if self._context is not None:
            raise RuntimeError("dual-arm ROS port is already open")
        self._spin_error = None
        import rclpy
        from arx5_arm_msg.msg import RobotStatus
        from arx5_collection_interfaces.msg import ArmState
        from rclpy.executors import SingleThreadedExecutor
        from std_srvs.srv import Trigger

        self._context = rclpy.Context()
        rclpy.init(context=self._context)
        try:
            self._node = rclpy.create_node(
                f"dagger_control_{uuid4().hex[:8]}",
                context=self._context,
            )
            self._executor = SingleThreadedExecutor(context=self._context)
            self._executor.add_node(self._node)
            self._reject_existing_command_publishers()
            for side, topic in zip(("left", "right"), self.state_topics):
                self._node.create_subscription(
                    ArmState,
                    topic,
                    lambda message, side=side: self._observe(side, message),
                    10,
                )
            self._publishers = (
                self._node.create_publisher(RobotStatus, self.command_topics[0], 10),
                self._node.create_publisher(RobotStatus, self.command_topics[1], 10),
            )
            self._message_type = RobotStatus
            self._trigger_type = Trigger
            if self.policy_enable_services is not None:
                self._enable_clients = tuple(
                    self._node.create_client(Trigger, service)
                    for service in self.policy_enable_services
                )
            self._thread = Thread(
                target=self._spin,
                name="dagger-control-ros",
                daemon=False,
            )
            self._thread.start()
            self._wait_for_enable_services()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise RuntimeError("dual-arm ROS executor did not stop")
        if self._node is not None:
            self._node.destroy_node()
        if self._context is not None and self._context.ok():
            self._context.shutdown()
        self._publishers = None
        self._message_type = None
        self._trigger_type = None
        self._enable_clients = None
        self._thread = None
        self._executor = None
        self._node = None
        self._context = None
        with self._lock:
            self._states.clear()

    def read(self) -> DualArmJointState:
        self._raise_spin_error()
        now = monotonic()
        with self._lock:
            try:
                left, left_at = self._states["left"]
                right, right_at = self._states["right"]
            except KeyError as error:
                raise RuntimeError("dual-arm feedback is not ready") from error
        oldest_age = max(now - left_at, now - right_at)
        if oldest_age > self.state_timeout_s:
            raise RuntimeError(
                f"dual-arm feedback is stale: age={oldest_age:.6f}s "
                f"limit={self.state_timeout_s:.6f}s"
            )
        return DualArmJointState(left, right)

    def publish(self, command: DualArmJointCommand) -> None:
        self._raise_spin_error()
        if self._publishers is None or self._message_type is None:
            raise RuntimeError("dual-arm ROS port is not open")
        left = self._message_type()
        right = self._message_type()
        left.joint_pos = list(command.left)
        right.joint_pos = list(command.right)
        self._publishers[0].publish(left)
        self._publishers[1].publish(right)

    def enable_policy_control(self) -> None:
        self._raise_spin_error()
        if self._enable_clients is None or self._trigger_type is None:
            raise RuntimeError("policy enable services are not configured")
        futures = [
            client.call_async(self._trigger_type.Request())
            for client in self._enable_clients
        ]
        deadline = monotonic() + 5.0
        while not all(future.done() for future in futures):
            if monotonic() >= deadline:
                raise RuntimeError("policy enable service timeout")
            self._raise_spin_error()
            sleep(0.01)
        failures = []
        for service, future in zip(self.policy_enable_services or (), futures):
            response = future.result()
            if response is None or not response.success:
                message = response.message if response is not None else "no response"
                failures.append(f"{service}: {message}")
        if failures:
            raise RuntimeError("policy enable failed: " + "; ".join(failures))

    def _observe(self, side: str, message: Any) -> None:
        joints = tuple(float(value) for value in message.joint_positions)
        if len(joints) != 6:
            return
        with self._lock:
            self._states[side] = (joints, monotonic())

    def _reject_existing_command_publishers(self) -> None:
        assert self._node is not None and self._executor is not None
        for _ in range(5):
            self._executor.spin_once(timeout_sec=0.1)
        active = {
            topic: tuple(
                f"{info.node_namespace}/{info.node_name}"
                for info in self._node.get_publishers_info_by_topic(topic)
            )
            for topic in self.command_topics
        }
        active = {topic: nodes for topic, nodes in active.items() if nodes}
        if active:
            raise RuntimeError(f"action publisher already active: {active}")

    def _wait_for_enable_services(self) -> None:
        if self._enable_clients is None:
            return
        unavailable = [
            service
            for service, client in zip(
                self.policy_enable_services or (), self._enable_clients
            )
            if not client.wait_for_service(timeout_sec=10.0)
        ]
        if unavailable:
            raise RuntimeError(
                "policy enable service unavailable: " + ", ".join(unavailable)
            )

    def _spin(self) -> None:
        assert self._executor is not None
        try:
            self._executor.spin()
        except BaseException as error:
            self._spin_error = error

    def _raise_spin_error(self) -> None:
        if self._spin_error is not None:
            raise RuntimeError("dual-arm ROS executor failed") from self._spin_error
