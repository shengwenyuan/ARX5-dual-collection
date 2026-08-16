from __future__ import annotations

from typing import Any

import pyrealsense2 as rs
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from arx5_camera_source.image_contract import (
    ColorContract,
    color_contract,
    timestamp_parts,
    validate_image_buffer,
)


class D405Source(Node):
    def __init__(self) -> None:
        super().__init__("d405_source")
        self.camera_name = str(self.declare_parameter("camera_name", "").value)
        self.serial = str(self.declare_parameter("serial", "").value)
        self.width = int(self.declare_parameter("width", 1280).value)
        self.height = int(self.declare_parameter("height", 720).value)
        self.fps = int(self.declare_parameter("fps", 30).value)
        self.frame_timeout_ms = int(
            self.declare_parameter("frame_timeout_ms", 5000).value
        )
        self.color = color_contract(
            str(self.declare_parameter("color_format", "yuyv").value)
        )
        self._validate_parameters()

        self.color_publisher = self.create_publisher(
            Image, "color/image_raw", qos_profile_sensor_data
        )
        self.depth_publisher = self.create_publisher(
            Image, "aligned_depth/image_raw", qos_profile_sensor_data
        )
        self.pipeline = rs.pipeline()
        self.align = rs.align(rs.stream.color)
        self.started = False

    def _validate_parameters(self) -> None:
        if self.camera_name not in {"left", "right", "overview"}:
            raise ValueError("camera_name must be left, right, or overview")
        if not self.serial:
            raise ValueError("serial must not be empty")
        if (self.width, self.height, self.fps) != (1280, 720, 30):
            raise ValueError("v0.1 camera stream is fixed at 1280x720@30")
        if self.frame_timeout_ms <= 0:
            raise ValueError("frame_timeout_ms must be positive")

    def _configure_global_time(self) -> None:
        matching_device = None
        for device in rs.context().query_devices():
            if device.get_info(rs.camera_info.serial_number) == self.serial:
                matching_device = device
                break
        if matching_device is None:
            raise RuntimeError(f"RealSense {self.serial} was not found")

        supported = 0
        for sensor in matching_device.query_sensors():
            if sensor.supports(rs.option.global_time_enabled):
                sensor.set_option(rs.option.global_time_enabled, 1.0)
                if sensor.get_option(rs.option.global_time_enabled) != 1.0:
                    raise RuntimeError(f"RealSense {self.serial} rejected Global Time")
                supported += 1
        if supported == 0:
            raise RuntimeError(f"RealSense {self.serial} does not expose Global Time")

    def _stream_config(self) -> Any:
        realsense_format = getattr(rs.format, self.color.realsense_format)
        config = rs.config()
        config.enable_device(self.serial)
        config.enable_stream(
            rs.stream.color,
            self.width,
            self.height,
            realsense_format,
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
        self._configure_global_time()
        self.pipeline.start(self._stream_config())
        self.started = True
        self.get_logger().info(
            f"started {self.camera_name} D405 {self.serial} "
            f"at {self.width}x{self.height}@{self.fps} {self.color.name}"
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
            self.color.ros_encoding,
            self.color.bytes_per_pixel,
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
        exit_code = 1
        if node is not None:
            node.get_logger().fatal(f"camera source failed: {type(error).__name__}: {error}")
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
