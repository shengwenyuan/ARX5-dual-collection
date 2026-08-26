from __future__ import annotations

import unittest
from types import SimpleNamespace

from arx5_collection.dagger.observation import (
    Pi05ObservationEncoder,
    RawArmSample,
    RgbFrame,
    VlaObservationStep,
)
from arx5_collection.gripper import ARX5_GRIPPER_CALIBRATION
from arx5_collection.dagger.ros_snapshot import OpenCvRgbResizer, _camera_frame

try:
    import cv2  # noqa: F401
except ImportError:
    cv2 = None


def frame(stamp_ns: int) -> RgbFrame:
    return RgbFrame(b"\x00" * 12, stamp_ns, width=2, height=2)


def arm(stamp_ns: int, value: float = 0.0) -> RawArmSample:
    return RawArmSample((value,) * 6, value, stamp_ns)


class Preprocessor:
    def prepare(self, source: RgbFrame) -> RgbFrame:
        return RgbFrame(b"\x00" * 12, source.stamp_ns, width=2, height=2)


class ObservationTest(unittest.TestCase):
    def test_encoder_is_model_specific_and_sampler_step_is_not(self) -> None:
        step = VlaObservationStep(
            cutoff_ns=1000,
            camera_left=frame(1000),
            camera_overview=frame(1000),
            camera_right=frame(1000),
            left_arm=arm(999, -3.4),
            right_arm=arm(999, 0.0),
        )
        encoder = Pi05ObservationEncoder(
            ARX5_GRIPPER_CALIBRATION, Preprocessor()
        )

        observation = encoder.encode(step)

        self.assertEqual(
            observation.state,
            (-3.4,) * 6 + (0.0,) + (0.0,) * 6 + (1.0,),
        )
        self.assertEqual(observation.cutoff_ns, 1000)

    @unittest.skipIf(cv2 is None, "headless OpenCV is unavailable")
    def test_headless_opencv_resizer_preserves_rgb_channel_order(self) -> None:
        source = RgbFrame(
            bytes((255, 0, 0, 0, 0, 255)),
            10,
            width=2,
            height=1,
        )

        converted = OpenCvRgbResizer(width=2, height=1).prepare(source)

        self.assertEqual(converted.data, source.data)
        self.assertEqual(converted.stamp_ns, source.stamp_ns)

    def test_snapshot_accepts_only_tightly_packed_rgb8(self) -> None:
        message = SimpleNamespace(
            encoding="rgb8",
            data=bytes((255, 0, 0, 0, 0, 255)),
            width=2,
            height=1,
            step=6,
            header=SimpleNamespace(stamp=SimpleNamespace(sec=1, nanosec=2)),
        )

        converted = _camera_frame(message)

        self.assertEqual(converted.data, message.data)
        self.assertEqual(converted.stamp_ns, 1_000_000_002)

    def test_snapshot_rejects_legacy_encoding_and_row_padding(self) -> None:
        message = SimpleNamespace(
            encoding="yuv422_yuy2",
            data=b"\x00" * 4,
            width=2,
            height=1,
            step=4,
            header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=1)),
        )
        with self.assertRaises(RuntimeError):
            _camera_frame(message)

        message.encoding = "rgb8"
        message.data = b"\x00" * 8
        message.step = 8
        with self.assertRaises(RuntimeError):
            _camera_frame(message)


if __name__ == "__main__":
    unittest.main()
