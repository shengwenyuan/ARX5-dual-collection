from __future__ import annotations

from time import monotonic_ns
from typing import Any

from .observation import (
    ObservationFailureCode,
    ObservationUnavailableError,
    RawArmSample,
    RgbFrame,
    VlaObservationStep,
    YuyvFrame,
)


SNAPSHOT_SERVICE = "/dagger/get_snapshot"


class RosVlaSnapshotClient:
    """Request bounded C++ snapshots without subscribing to image Topics in Python."""

    def __init__(
        self,
        timeout_s: float = 0.25,
        service_name: str = SNAPSHOT_SERVICE,
        monotonic_clock_ns=monotonic_ns,
    ) -> None:
        if timeout_s <= 0 or not service_name:
            raise ValueError("snapshot service timeout and name are invalid")
        import rclpy
        from arx5_collection_interfaces.srv import GetVlaSnapshot
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor

        self._timeout_s = timeout_s
        self._clock_ns = monotonic_clock_ns
        self._context = Context()
        rclpy.init(context=self._context)
        self._node = rclpy.create_node("vla_snapshot_client", context=self._context)
        self._executor = SingleThreadedExecutor(context=self._context)
        self._executor.add_node(self._node)
        self._service_type = GetVlaSnapshot
        self._client = self._node.create_client(GetVlaSnapshot, service_name)
        self._closed = False

    def __enter__(self) -> RosVlaSnapshotClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def capture(self) -> VlaObservationStep:
        if self._closed:
            raise RuntimeError("snapshot client is closed")
        started_ns = self._clock_ns()
        if not self._client.wait_for_service(timeout_sec=self._timeout_s):
            raise self._unavailable(started_ns, "snapshot service is unavailable")
        future = self._client.call_async(self._service_type.Request())
        self._executor.spin_until_future_complete(future, timeout_sec=self._timeout_s)
        if not future.done():
            future.cancel()
            raise self._unavailable(started_ns, "snapshot service timed out")
        error = future.exception()
        if error is not None:
            raise RuntimeError(f"snapshot service call failed: {error}") from error
        response = future.result()
        if response is None:
            raise RuntimeError("snapshot service returned no response")
        if not response.ready:
            raise _response_error(response)
        return VlaObservationStep(
            cutoff_ns=_stamp_ns(response.observation_cutoff),
            camera_left=_camera_frame(response.camera_left),
            camera_overview=_camera_frame(response.camera_overview),
            camera_right=_camera_frame(response.camera_right),
            left_arm=_arm_sample(response.left_arm),
            right_arm=_arm_sample(response.right_arm),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.remove_node(self._node)
        self._node.destroy_node()
        self._context.shutdown()

    def _unavailable(
        self, started_ns: int, detail: str
    ) -> ObservationUnavailableError:
        return ObservationUnavailableError(
            ObservationFailureCode.BUFFERS_NOT_READY,
            observed_ns=self._clock_ns() - started_ns,
            limit_ns=int(self._timeout_s * 1_000_000_000),
            detail=detail,
        )


class OpenCvYuyvConverter:
    """Run sensor-format conversion in the headless OpenCV native runtime."""

    def __init__(self, width: int = 640, height: int = 360) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("transport image dimensions must be positive")
        self.width = width
        self.height = height

    def convert(self, frame: YuyvFrame) -> RgbFrame:
        import cv2
        import numpy as np

        source = np.frombuffer(frame.data, dtype=np.uint8).reshape(
            frame.height, frame.width, 2
        )
        rgb = cv2.cvtColor(source, cv2.COLOR_YUV2RGB_YUY2)
        if (frame.width, frame.height) != (self.width, self.height):
            rgb = cv2.resize(
                rgb, (self.width, self.height), interpolation=cv2.INTER_AREA
            )
        return RgbFrame(
            data=rgb.tobytes(),
            stamp_ns=frame.stamp_ns,
            width=self.width,
            height=self.height,
        )


def _response_error(response: Any) -> ObservationUnavailableError:
    try:
        code = ObservationFailureCode(str(response.failure_code))
    except ValueError as error:
        raise RuntimeError(
            f"snapshot service returned unknown failure code {response.failure_code!r}"
        ) from error
    return ObservationUnavailableError(
        code,
        observed_ns=_optional_ns(response.observed_ns),
        limit_ns=_optional_ns(response.limit_ns),
        detail=str(response.detail),
    )


def _optional_ns(value: int) -> int | None:
    value = int(value)
    return None if value < 0 else value


def _camera_frame(message: Any) -> YuyvFrame:
    encoding = str(message.encoding).lower()
    if encoding not in {"yuyv", "yuy2", "yuv422_yuy2"}:
        raise RuntimeError(f"snapshot image encoding is unsupported: {encoding!r}")
    return YuyvFrame(
        data=message.data,
        stamp_ns=_stamp_ns(message.header.stamp),
        width=int(message.width),
        height=int(message.height),
        step=int(message.step),
    )


def _arm_sample(message: Any) -> RawArmSample:
    return RawArmSample(
        joint_positions=tuple(float(value) for value in message.joint_positions),
        gripper_position=float(message.gripper_position),
        stamp_ns=_stamp_ns(message.header.stamp),
    )


def _stamp_ns(stamp: Any) -> int:
    return int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
