from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic_ns, time_ns
from typing import Any
from uuid import uuid4

from arx5_collection.dagger.action_gateway import DualArmJointCommand, DualArmJointState
from arx5_collection.dagger.action_runtime import TakeoverControlPort


COMMAND_TOPIC = "/infer/command"


@dataclass(frozen=True, slots=True)
class CommandRecord:
    episode_id: str
    step_index: int
    monotonic_time_ns: int
    ros_time_ns: int
    action: tuple[float, ...]


class RecordedControl:
    """Record the final paired publish arguments, never measured joint positions.

    The existing scheduler serializes publish and close_gate. Start/finish are called
    with that gate closed; no second command queue or execution thread is introduced.
    """

    def __init__(
        self,
        control: TakeoverControlPort,
        sink: Callable[[CommandRecord], None],
        monotonic_clock: Callable[[], int] = monotonic_ns,
        ros_clock: Callable[[], int] = time_ns,
    ) -> None:
        self.control = control
        self.sink = sink
        self.monotonic_clock = monotonic_clock
        self.ros_clock = ros_clock
        self.episode_id: str | None = None
        self.command_count = 0
        self.failure: BaseException | None = None
        self.last_command_monotonic_ns: int | None = None

    def start(self, episode_id: str) -> None:
        if self.episode_id is not None:
            raise RuntimeError("command recording is already active")
        self.episode_id = episode_id
        self.failure = None
        self.command_count = 0
        self.last_command_monotonic_ns = None

    def finish(self) -> None:
        self.episode_id = None

    def read(self) -> DualArmJointState:
        return self.control.read()

    def enable_policy_control(self) -> None:
        self.control.enable_policy_control()

    def publish(self, command: DualArmJointCommand) -> None:
        if self.episode_id is None:
            raise RuntimeError("command issued outside an infer episode")
        record = CommandRecord(
            self.episode_id,
            self.command_count,
            self.monotonic_clock(),
            self.ros_clock(),
            (*command.left, *command.right),
        )
        try:
            self.control.publish(command)
            # Count successful sends even if the subsequent record publish fails.
            self.command_count += 1
            self.last_command_monotonic_ns = record.monotonic_time_ns
            self.sink(record)
        except BaseException as error:
            # Keep the fault visible even if a concurrent label closes the RTC gate
            # before its worker publishes the fault notification.
            self.failure = error
            raise


class RosCommandPublisher:
    """One small ROS message per successful paired send; no hardware ACK claim."""

    def __init__(self) -> None:
        self._context: Any = None
        self._node: Any = None
        self._publisher: Any = None
        self._message_type: Any = None

    def __enter__(self) -> RosCommandPublisher:
        import rclpy
        from arx5_collection_interfaces.msg import InferCommand
        from rclpy.qos import QoSProfile, ReliabilityPolicy

        self._context = rclpy.Context()
        try:
            rclpy.init(context=self._context)
            self._node = rclpy.create_node(
                f"infer_commands_{uuid4().hex[:8]}", context=self._context
            )
            if self._node.get_parameter("use_sim_time").value:
                raise RuntimeError("infer command timestamps require the host system clock")
            self._publisher = self._node.create_publisher(
                InferCommand, COMMAND_TOPIC,
                QoSProfile(depth=256, reliability=ReliabilityPolicy.RELIABLE),
            )
            self._message_type = InferCommand
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *args: object) -> None:
        if self._node is not None:
            self._node.destroy_node()
        if self._context is not None and self._context.ok():
            self._context.shutdown()
        self._publisher = self._node = self._context = self._message_type = None

    def now_ns(self) -> int:
        if self._node is None:
            raise RuntimeError("command publisher is not open")
        return self._node.get_clock().now().nanoseconds

    def __call__(self, record: CommandRecord) -> None:
        if self._publisher is None:
            raise RuntimeError("command publisher is not open")
        message = self._message_type()
        message.header.stamp.sec, message.header.stamp.nanosec = divmod(
            record.ros_time_ns, 1_000_000_000
        )
        message.episode_id = record.episode_id
        message.step_index = record.step_index
        message.monotonic_time_ns = record.monotonic_time_ns
        message.action = list(record.action)
        self._publisher.publish(message)
