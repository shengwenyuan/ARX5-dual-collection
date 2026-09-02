from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from arx5_collection.collection.runtime.config import CameraConfig

from .inventory import D405Device
from arx5_collection.collection.environment import ENVIRONMENT


CAMERA_BINDING_ORDER = ENVIRONMENT.station.camera_binding_order


class CameraIdentificationError(RuntimeError):
    pass


class CameraIdentifier:
    def __init__(
        self,
        candidates: Sequence[D405Device],
        stream_validator: RealSenseStreamValidator | None = None,
    ) -> None:
        self.candidates = {
            candidate.serial_number: candidate for candidate in candidates
        }
        if len(self.candidates) != len(candidates):
            raise CameraIdentificationError("D405 serial numbers must be unique")
        self.stream_validator = stream_validator or RealSenseStreamValidator()
        self._selected: set[str] = set()

    def bind(self, role: str, serial_number: str) -> CameraConfig:
        if role not in CAMERA_BINDING_ORDER:
            raise ValueError(f"unknown camera role {role}")
        if serial_number not in self.candidates:
            raise CameraIdentificationError(
                f"camera {serial_number} is not on the current librealsense chain"
            )
        if serial_number in self._selected:
            raise CameraIdentificationError(f"camera {serial_number} is already bound")
        candidate = self.candidates[serial_number]
        if not _is_usb3(candidate.usb_type):
            raise CameraIdentificationError(
                f"camera {serial_number} requires USB3, detected {candidate.usb_type!r}"
            )
        self.stream_validator.validate(serial_number)
        self._selected.add(serial_number)
        return CameraConfig(role=role, serial_number=serial_number)


class RealSenseStreamValidator:
    """Open one D405 and verify the exact production RGB-D contract."""

    def __init__(
        self,
        rs_module: Any | None = None,
        frame_timeout_ms: int = ENVIRONMENT.camera.frame_timeout_ms,
    ) -> None:
        self.rs_module = rs_module
        self.frame_timeout_ms = frame_timeout_ms

    def validate(self, serial_number: str) -> None:
        rs = self.rs_module
        if rs is None:
            try:
                import pyrealsense2 as rs
            except ImportError as error:
                raise CameraIdentificationError(
                    f"librealsense unavailable: {error}"
                ) from error

        pipeline = rs.pipeline()
        config = rs.config()
        config.enable_device(serial_number)
        color_format = getattr(rs.format, ENVIRONMENT.camera.color_format)
        depth_format = getattr(rs.format, ENVIRONMENT.camera.depth_format)
        config.enable_stream(
            rs.stream.color,
            ENVIRONMENT.camera.width,
            ENVIRONMENT.camera.height,
            color_format,
            ENVIRONMENT.camera.fps,
        )
        config.enable_stream(
            rs.stream.depth,
            ENVIRONMENT.camera.width,
            ENVIRONMENT.camera.height,
            depth_format,
            ENVIRONMENT.camera.fps,
        )
        started = False
        try:
            profile = pipeline.start(config)
            started = True
            device = profile.get_device()
            usb_type = device.get_info(rs.camera_info.usb_type_descriptor)
            if not _is_usb3(usb_type):
                raise CameraIdentificationError(
                    f"camera {serial_number} requires USB3, detected {usb_type!r}"
                )
            frames = rs.align(rs.stream.color).process(
                pipeline.wait_for_frames(self.frame_timeout_ms)
            )
            color = frames.get_color_frame()
            depth = frames.get_depth_frame()
            if not color or not depth:
                raise CameraIdentificationError(
                    f"camera {serial_number} returned incomplete aligned RGB-D"
                )
            color_shape = (int(color.get_width()), int(color.get_height()))
            depth_shape = (int(depth.get_width()), int(depth.get_height()))
            expected_shape = (ENVIRONMENT.camera.width, ENVIRONMENT.camera.height)
            if color_shape != expected_shape or depth_shape != color_shape:
                raise CameraIdentificationError(
                    f"camera {serial_number} shape mismatch color={color_shape} depth={depth_shape}"
                )
        except CameraIdentificationError:
            raise
        except RuntimeError as error:
            raise CameraIdentificationError(
                f"camera {serial_number} stream validation failed: {error}"
            ) from error
        finally:
            if started:
                pipeline.stop()


def _is_usb3(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith("3") or normalized.startswith("usb3")
