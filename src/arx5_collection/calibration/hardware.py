"""Exclusive calibration runtime. Imports ROS/RealSense only when hardware opens."""

from __future__ import annotations

import math
from collections import deque
from contextlib import ExitStack
from threading import Lock, Thread
from time import monotonic, sleep, time


from arx5_collection.production.config import set_process_ros_domain_id
from arx5_collection.production.processes import (
    ManagedProcess,
    ProcessSpec,
    RosProcessSupervisor,
)
from arx5_collection.production.system import SystemBringup

from .camera import Camera
from .motion import MotionLimits, validate_sample
from .profiles import DEFAULT_PROFILE as PROFILE
from .profiles import validate_profile


class Arms:
    """Dedicated calibration nodes: raw six-joint feedback; gripper commands ignored in C++."""

    def __init__(self, limits=None):
        self.limits = limits or MotionLimits()
        self.lock = Lock()
        self.samples = {}
        self.history = deque(maxlen=10000)
        self.error = None
        self.context = self.node = self.executor = self.thread = None
        self.clients = {}
        self.controllers_enabled = False

    def open(self):
        import rclpy
        from arx5_arm_msg.msg import RobotStatus
        from rclpy.executors import SingleThreadedExecutor
        from std_srvs.srv import Trigger

        self.Trigger, self.Message = Trigger, RobotStatus
        self.context = rclpy.Context()
        rclpy.init(context=self.context)
        self.node = rclpy.create_node("calibration_io", context=self.context)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.publishers = {}
        for side in ("left", "right"):
            topic = f"/calibration/{side}/command"
            for _ in range(3):
                self.executor.spin_once(timeout_sec=0.05)
            if self.node.get_publishers_info_by_topic(topic):
                raise RuntimeError("another calibration command publisher exists")
            self.publishers[side] = self.node.create_publisher(RobotStatus, topic, 1)
            self.node.create_subscription(
                RobotStatus,
                f"/calibration/{side}/state",
                lambda m, side=side: self._observe(side, m),
                1,
            )
            for service in (
                "gravity_compensation",
                "enable_policy_control",
                "calibration_hold",
            ):
                self.clients[side, service] = self.node.create_client(
                    Trigger, f"/calibration_{side}/{service}"
                )
        self.thread = Thread(
            target=self._spin, name="calibration-feedback", daemon=True
        )
        self.thread.start()
        deadline = monotonic() + 20
        while monotonic() < deadline:
            if self.error:
                raise RuntimeError("arm feedback failed") from self.error
            if (
                all(client.service_is_ready() for client in self.clients.values())
                and len(self.samples) == 2
            ):
                self.read()
                return
            sleep(0.02)
        raise TimeoutError(
            "calibration services unavailable; rebuild calibration image with controller patch"
        )

    def _spin(self):
        try:
            self.executor.spin()
        except BaseException as error:
            self.error = error

    def _observe(self, side, message):
        try:
            now, wall = monotonic(), time()
            stamp = message.header.stamp.sec + message.header.stamp.nanosec * 1e-9
            if not -0.02 <= wall - stamp <= self.limits.feedback_age_s:
                raise RuntimeError("robot source timestamp stale/incompatible")
            arm = {
                "q": list(message.joint_pos[:6]),
                "velocity": list(message.joint_vel[:6]),
                "current": list(message.joint_cur[:6]),
                "gripper": float(message.joint_pos[6]),
                "gripper_velocity": float(message.joint_vel[6]),
                "gripper_current": float(message.joint_cur[6]),
                "vendor_end_pos": list(message.end_pos),
                "source_time_s": stamp,
                "received_wall_s": wall,
                "received_monotonic_s": now,
                "source_clock": "ROS publish wall time; motor acquisition timestamp unavailable",
            }
            if not all(
                math.isfinite(v) for v in [*arm["q"], *arm["velocity"], arm["gripper"]]
            ):
                raise ValueError("nonfinite robot readback")
            with self.lock:
                old = self.samples.get(side)
                if old and stamp <= old["source_time_s"]:
                    raise RuntimeError("non-increasing robot source timestamp")
                self.samples[side] = arm
                self.history.append({"side": side, **arm})
        except BaseException as error:
            self.error = error

    def read(self):
        if self.error:
            raise RuntimeError("arm feedback failed") from self.error
        with self.lock:
            state = dict(self.samples)
        if len(state) != 2:
            raise RuntimeError("both arms need fresh feedback")
        validate_sample(state, monotonic(), self.limits)
        return state

    def publish(self, q):
        self.read()
        for side in ("left", "right"):
            m = self.Message()
            m.header.stamp = self.node.get_clock().now().to_msg()
            m.joint_pos = [
                *map(float, q[side]),
                0.0,
            ]  # seventh value ignored by calibration-only callback
            self.publishers[side].publish(m)

    def call(self, service):
        if service == "enable_policy_control":
            self.read()
        futures = [
            self.clients[s, service].call_async(self.Trigger.Request())
            for s in ("left", "right")
        ]
        deadline = monotonic() + 3
        while not all(f.done() for f in futures):
            if monotonic() > deadline:
                raise TimeoutError(f"{service} timed out")
            # Hold/gravity must remain callable even when position feedback is faulty.
            sleep(0.005)
        for future in futures:
            response = future.result()
            if response is None or not response.success:
                raise RuntimeError(
                    f"{service} rejected: {getattr(response, 'message', 'no response')}"
                )
        self.controllers_enabled = service == "enable_policy_control"

    def evidence(self, begin, end):
        with self.lock:
            return [
                s for s in self.history if begin <= s["received_monotonic_s"] <= end
            ]

    def close(self):
        if self.executor:
            self.executor.shutdown(timeout_sec=2)
        if self.thread:
            self.thread.join(2)
        if self.node:
            self.node.destroy_node()
        if self.context and self.context.ok():
            self.context.shutdown()


class Hardware:
    """CAN bringup only; replay performs bounded HOME through joint commands."""

    def __init__(self, station, role, logs, limits, profile=None):
        self.station, self.role, self.logs, self.limits = station, role, logs, limits
        self.profile = validate_profile(PROFILE if profile is None else profile)
        self.stack = ExitStack()

    def __enter__(self):
        from .storage import ROLES

        try:
            if self.station.sdk_type != 2:
                raise ValueError(
                    "calibration supports this station's X5 v2 configuration only"
                )
            set_process_ros_domain_id(self.station.ros_domain_id)
            system = SystemBringup(self.station, self.logs)
            system.start()
            self.stack.callback(system.stop)
            camera_role = ROLES[self.role][0]
            serial = next(
                c.serial_number for c in self.station.cameras if c.role == camera_role
            )
            self.camera = Camera(serial, self.logs / "camera.log", profile=self.profile)
            self.stack.callback(self.camera.close)
            self.camera.open()
            self.supervisor = RosProcessSupervisor()
            self.stack.callback(self.supervisor.stop_all)
            for arm in self.station.arms:
                side = arm.role
                argv = (
                    "ros2",
                    "run",
                    "arx_x5_controller",
                    "X5Controller",
                    "--ros-args",
                    "-r",
                    f"__node:=calibration_{side}",
                    "-p",
                    "arm_control_type:=remote_slave",
                    "-p",
                    "calibration_mode:=true",
                    "-p",
                    "arm_end_type:=2",
                    "-p",
                    f"arm_can_id:={arm.can_interface}",
                    "-p",
                    f"arm_pub_topic_name:=/calibration/{side}/state",
                    "-p",
                    f"arm_sub_topic_name:=/calibration/{side}/command",
                )
                self.supervisor.start(
                    ManagedProcess(
                        ProcessSpec(
                            f"calibration-{side}", argv, self.logs / f"{side}.log"
                        )
                    )
                )
            self.arms = Arms(self.limits)
            self.stack.callback(self.arms.close)
            self.arms.open()
            self.arms.call("gravity_compensation")
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *_):
        self.stack.close()
