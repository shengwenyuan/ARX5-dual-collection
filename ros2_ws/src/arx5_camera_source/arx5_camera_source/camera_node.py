from __future__ import annotations

from time import monotonic
from typing import Any

import pyrealsense2 as rs
import rclpy
from arx5_collection_interfaces.msg import StreamStatus
from arx5_monitoring.reporter import StreamStatusReporter
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from sensor_msgs.msg import Image

from arx5_camera_source.image_contract import (
    RGB8_BYTES_PER_PIXEL,
    RGB8_ENCODING,
    timestamp_parts,
    validate_image_buffer,
)


class D405Source(Node):
    def __init__(self) -> None:
        super().__init__("d405_source")
        self.camera_name = str(self.declare_parameter("camera_name", "").value)
        self.serial = str(self.declare_parameter("serial", "").value)
        self.width = int(self.declare_parameter("width", 848).value)
        self.height = int(self.declare_parameter("height", 480).value)
        self.fps = int(self.declare_parameter("fps", 30).value)
        self.frame_timeout_ms = int(
            self.declare_parameter("frame_timeout_ms", 5000).value
        )
        self.status_period_s = float(
            self.declare_parameter("status_period_s", 1.0).value
        )
        self.reliability = str(
            self.declare_parameter("reliability", "reliable").value
        )
        self._validate_parameters()

        qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=2,
            reliability=(
                QoSReliabilityPolicy.RELIABLE
                if self.reliability == "reliable"
                else QoSReliabilityPolicy.BEST_EFFORT
            ),
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self.color_publisher = self.create_publisher(
            Image, "color/image_raw", qos
        )
        self.depth_publisher = self.create_publisher(
            Image, "aligned_depth/image_raw", qos
        )
        status_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=32,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self.status_publisher = self.create_publisher(
            StreamStatus, "/monitoring/stream_status", status_qos
        )
        topic_root = f"/sensors/camera_{self.camera_name}"
        self.status_reporters = (
            StreamStatusReporter(
                self,
                self.status_publisher,
                f"camera_{self.camera_name}_color",
                f"{topic_root}/color/image_raw",
            ),
            StreamStatusReporter(
                self,
                self.status_publisher,
                f"camera_{self.camera_name}_aligned_depth",
                f"{topic_root}/aligned_depth/image_raw",
            ),
        )
        self._next_status_s = monotonic() + self.status_period_s
        self.pipeline = rs.pipeline()
        self.align = rs.align(rs.stream.color)
        self.started = False

    def _validate_parameters(self) -> None:
        if self.camera_name not in {"left", "right", "overview"}:
            raise ValueError("camera_name must be left, right, or overview")
        if not self.serial:
            raise ValueError("serial must not be empty")
        if (self.width, self.height, self.fps) != (848, 480, 30):
            raise ValueError("camera stream is fixed at 848x480@30")
        if self.frame_timeout_ms <= 0:
            raise ValueError("frame_timeout_ms must be positive")
        if self.status_period_s <= 0:
            raise ValueError("status_period_s must be positive")
        if self.reliability not in {"best_effort", "reliable"}:
            raise ValueError("reliability must be best_effort or reliable")

    def _verify_global_time(self, device: Any) -> None:
        supported = 0
        for sensor in device.query_sensors():
            if sensor.supports(rs.option.global_time_enabled):
                if sensor.get_option(rs.option.global_time_enabled) != 1.0:
                    raise RuntimeError(f"RealSense {self.serial} does not enable Global Time")
                supported += 1
        if supported == 0:
            raise RuntimeError(f"RealSense {self.serial} does not expose Global Time")

    def _stream_config(self) -> Any:
        config = rs.config()
        config.enable_device(self.serial)
        config.enable_stream(
            rs.stream.color,
            self.width,
            self.height,
            rs.format.rgb8,
            self.fps,
        )
        config.enable_stream(
            rs.stream.depth,
            self.width,
            self.height,
            rs.format.z16,
            self.fps,
        )
        return config

    def start(self) -> None:
        profile = self.pipeline.start(self._stream_config())
        self.started = True
        self._verify_global_time(profile.get_device())
        self.get_logger().info(
            f"started {self.camera_name} D405 {self.serial} "
            f"at {self.width}x{self.height}@{self.fps} {RGB8_ENCODING} "
            f"{self.reliability}"
        )

    def stop(self) -> None:
        if self.started:
            self.pipeline.stop()
            self.started = False

    def _stamp(self, message: Image, timestamp_ms: float, frame_id: str) -> None:
        sec, nanosec = timestamp_parts(timestamp_ms)
        message.header.stamp.sec = sec
        message.header.stamp.nanosec = nanosec
        message.header.frame_id = frame_id

    def _image_message(
        self,
        frame: Any,
        encoding: str,
        bytes_per_pixel: int,
        timestamp_ms: float,
        frame_id: str,
    ) -> Image:
        payload = bytes(frame.get_data())
        width = int(frame.get_width())
        height = int(frame.get_height())
        step = int(frame.get_stride_in_bytes())
        validate_image_buffer(width, height, step, len(payload), bytes_per_pixel)

        message = Image()
        self._stamp(message, timestamp_ms, frame_id)
        message.height = height
        message.width = width
        message.encoding = encoding
        message.is_bigendian = 0
        message.step = step
        message.data = payload
        return message

    def publish_once(self) -> None:
        frames = self.align.process(
            self.pipeline.wait_for_frames(self.frame_timeout_ms)
        )
        color_frame = frames.get_color_frame()
        depth_frame = frames.get_depth_frame()
        if not color_frame or not depth_frame:
            raise RuntimeError(f"RealSense {self.serial} returned an incomplete frameset")

        expected_domain = getattr(rs.timestamp_domain, "global_time", None)
        timestamp_domain = color_frame.get_frame_timestamp_domain()
        if expected_domain is None or timestamp_domain != expected_domain:
            raise RuntimeError(
                f"RealSense {self.serial} timestamp domain is {timestamp_domain}, "
                "expected Global Time"
            )

        timestamp_ms = float(color_frame.get_timestamp())
        frame_id = f"camera_{self.camera_name}_color_optical_frame"
        color_message = self._image_message(
            color_frame,
            RGB8_ENCODING,
            RGB8_BYTES_PER_PIXEL,
            timestamp_ms,
            frame_id,
        )
        depth_message = self._image_message(
            depth_frame,
            "16UC1",
            2,
            timestamp_ms,
            frame_id,
        )
        self.color_publisher.publish(color_message)
        self.depth_publisher.publish(depth_message)
        sec, nanosec = timestamp_parts(timestamp_ms)
        message_stamp_ns = sec * 1_000_000_000 + nanosec
        now_s = monotonic()
        for reporter in self.status_reporters:
            reporter.observe(message_stamp_ns, now_s)
        if now_s >= self._next_status_s:
            for reporter in self.status_reporters:
                reporter.publish(now_s)
            self._next_status_s = now_s + self.status_period_s

    def run(self) -> None:
        self.start()
        while rclpy.ok():
            rclpy.spin_once(self, timeout_sec=0.0)
            self.publish_once()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node: D405Source | None = None
    exit_code = 0
    try:
        node = D405Source()
        node.run()
    except KeyboardInterrupt:
        pass
    except BaseException as error:
        if rclpy.ok():
            exit_code = 1
            if node is not None:
                node.get_logger().fatal(
                    f"camera source failed: {type(error).__name__}: {error}"
                )
            else:
                print(f"camera source failed: {type(error).__name__}: {error}")
    finally:
        if node is not None:
            try:
                node.stop()
            finally:
                node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    raise SystemExit(exit_code)
