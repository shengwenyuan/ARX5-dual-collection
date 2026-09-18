"""Exclusive calibration runtime. Imports ROS/RealSense only when hardware opens."""

from __future__ import annotations

from collections import deque
from contextlib import ExitStack
from threading import Lock, Thread, Event
from time import monotonic, time, sleep
import math
from pathlib import Path
import traceback

import numpy as np

from arx5_collection.production.config import set_process_ros_domain_id
from arx5_collection.production.processes import (
    ManagedProcess,
    ProcessSpec,
    RosProcessSupervisor,
)
from arx5_collection.production.system import SystemBringup
from .motion import MotionLimits, validate_sample

PROFILE = {"width": 848, "height": 480, "fps": 30, "format": "rgb8"}


class Camera:
    """One selected D405, one persistent pipeline, RGB copied off the SDK thread."""

    def __init__(self, serial, log_path=None):
        self.serial = serial
        self.pipeline = None
        self.started = False
        self.lock, self.stop_event = Lock(), Event()
        self.frame = self.error = self.thread = None
        self.metadata = {}
        self.log_path = Path(log_path) if log_path else None

    def _log(self, message):
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as stream:
                stream.write(f"{time():.6f} {message}\n")

    def open(self):
        import pyrealsense2 as rs

        self.rs = rs
        context = rs.context()
        devices = [
            d
            for d in context.query_devices()
            if d.get_info(rs.camera_info.serial_number) == self.serial
        ]
        if len(devices) != 1 or "D405" not in devices[0].get_info(rs.camera_info.name):
            raise RuntimeError("configured D405 not uniquely found")
        device = devices[0]
        if not device.get_info(rs.camera_info.usb_type_descriptor).startswith("3"):
            raise RuntimeError("calibration camera requires USB3")
        for sensor in device.query_sensors():
            if sensor.supports(rs.option.global_time_enabled):
                sensor.set_option(rs.option.global_time_enabled, 1)
        self.pipeline = rs.pipeline(context)
        config = rs.config()
        config.enable_device(self.serial)
        config.enable_stream(rs.stream.color, 848, 480, rs.format.rgb8, 30)
        # D405 color is generated from the depth imager; retain its native depth profile metadata.
        config.enable_stream(rs.stream.depth, 848, 480, rs.format.z16, 30)
        profile = self.pipeline.start(config)
        self.started = True
        color = profile.get_stream(rs.stream.color).as_video_stream_profile()
        depth = profile.get_stream(rs.stream.depth).as_video_stream_profile()
        if (color.width(), color.height(), color.fps(), color.format()) != (
            848,
            480,
            30,
            rs.format.rgb8,
        ):
            raise RuntimeError("actual RGB profile differs from route")

        def intrinsics(v):
            k = v.get_intrinsics()
            return {
                "width": k.width,
                "height": k.height,
                "fx": k.fx,
                "fy": k.fy,
                "ppx": k.ppx,
                "ppy": k.ppy,
                "model": str(k.model),
                "coeffs": list(k.coeffs),
            }

        extrinsic = depth.get_extrinsics_to(color)
        self.metadata = {
            "serial": self.serial,
            "profile": PROFILE,
            "factory_color": intrinsics(color),
            "factory_depth": intrinsics(depth),
            "depth_scale": profile.get_device().first_depth_sensor().get_depth_scale(),
            "depth_to_color": {
                "rotation_column_major": list(extrinsic.rotation),
                "translation_m": list(extrinsic.translation),
            },
            "alignment": "raw RGB; depth is not used in RGB calibration",
        }
        self.thread = Thread(target=self._run, name="calibration-camera", daemon=True)
        self.thread.start()
        deadline = monotonic() + 15
        while self.frame is None and monotonic() < deadline:
            if self.error:
                raise RuntimeError(
                    f"camera startup failed: {self.error}"
                ) from self.error
            sleep(0.02)
        if self.frame is None:
            raise TimeoutError("no fresh global-time RGB frames")

    def _run(self):
        try:
            warmup_until = monotonic() + 2
            previous_clock_error = None
            while not self.stop_event.is_set():
                frames = self.pipeline.wait_for_frames(1000)
                color = frames.get_color_frame()
                if not color or monotonic() < warmup_until:
                    continue
                received, wall = monotonic(), time()
                source = color.get_timestamp() / 1000
                global_time = (
                    color.get_frame_timestamp_domain()
                    == self.rs.timestamp_domain.global_time
                )
                age = wall - source
                # RealSense's host-clock estimate can settle after the image stream
                # starts. Keep the live preview; never admit these frames to capture.
                clock_error = (
                    "waiting for camera global time"
                    if not global_time
                    else "invalid camera timestamp"
                    if not math.isfinite(source)
                    else "camera clock settling / frame outside time bounds"
                    if not -0.02 <= age <= 0.20
                    else ""
                )
                if clock_error != previous_clock_error:
                    self._log(
                        f"frame={color.get_frame_number()} age_ms={age * 1000:.3f} clock={clock_error or 'ready'}"
                    )
                    previous_clock_error = clock_error
                finite_source = source if math.isfinite(source) else None
                frame = {
                    "image": np.asanyarray(color.get_data())[:, :, ::-1].copy(),
                    "number": color.get_frame_number(),
                    "source_time_s": finite_source,
                    "received_wall_s": wall,
                    "received_monotonic_s": received,
                    "source_monotonic_s": received - age
                    if finite_source is not None
                    else None,
                    "clock": "global_time"
                    if global_time
                    else str(color.get_frame_timestamp_domain()),
                    "clock_error": clock_error,
                }
                with self.lock:
                    self.frame = frame
        except BaseException as error:
            if not self.stop_event.is_set():
                self.error = error
                self._log(traceback.format_exc())

    def latest(self):
        # HighGUI can briefly hold the GIL while creating the first window.
        # Yield so acquisition can recover; never return the old frame as fresh.
        deadline = monotonic() + 1.0
        while True:
            if self.error:
                raise RuntimeError(
                    f"camera worker failed: {self.error}"
                ) from self.error
            with self.lock:
                frame = self.frame
            now = monotonic()
            if frame is not None and now - frame["received_monotonic_s"] <= 0.25:
                return frame
            if now >= deadline:
                raise RuntimeError("camera frame is stale (no fresh frame within 1 s)")
            sleep(0.005)

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(2)
        if self.pipeline and self.started:
            self.pipeline.stop()
            self.started = False
        self.pipeline = None


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
        from rclpy.executors import SingleThreadedExecutor
        from arx5_arm_msg.msg import RobotStatus
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
    """Uses the same CAN bringup boundary as collection; never calls HOME."""

    def __init__(self, station, role, logs, limits):
        self.station, self.role, self.logs, self.limits = station, role, logs, limits
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
            self.camera = Camera(serial, self.logs / "camera.log")
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
