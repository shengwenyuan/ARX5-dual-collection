from __future__ import annotations

import unittest

from arx5_collection.dagger.observation import (
    GripperCalibration,
    Pi05ObservationEncoder,
    RawArmSample,
    RgbFrame,
    VlaObservationStep,
    YuyvFrame,
)
from arx5_collection.dagger.ros_snapshot import OpenCvYuyvConverter

try:
    import cv2  # noqa: F401
except ImportError:
    cv2 = None


def frame(stamp_ns: int) -> YuyvFrame:
    return YuyvFrame(b"\x00" * 8, stamp_ns, width=2, height=2, step=4)


def arm(stamp_ns: int, value: float = 0.0) -> RawArmSample:
    return RawArmSample((value,) * 6, value, stamp_ns)


class Converter:
    def convert(self, source: YuyvFrame) -> RgbFrame:
        return RgbFrame(b"\x00" * 12, source.stamp_ns, width=2, height=2)


class ObservationTest(unittest.TestCase):
    def test_encoder_is_model_specific_and_sampler_step_is_not(self) -> None:
        step = VlaObservationStep(
            cutoff_ns=1000,
            camera_left=frame(1000),
            camera_overview=frame(1000),
            camera_right=frame(1000),
            left_arm=arm(999, -3.0),
            right_arm=arm(999, 0.0),
        )
        encoder = Pi05ObservationEncoder(
            GripperCalibration(-3.0, 0.0, -3.0, 0.0), Converter()
        )

        observation = encoder.encode(step)

        self.assertEqual(
            observation.state,
            (-3.0,) * 6 + (0.0,) + (0.0,) * 6 + (1.0,),
        )
        self.assertEqual(observation.cutoff_ns, 1000)

    @unittest.skipIf(cv2 is None, "headless OpenCV is unavailable")
    def test_headless_opencv_converter_preserves_black_and_white(self) -> None:
        # Two rows of black followed by two rows of white; each YUYV pair shares UV.
        black_pair = bytes((16, 128, 16, 128))
        white_pair = bytes((235, 128, 235, 128))
        payload = black_pair * 4 + white_pair * 4
        source = YuyvFrame(payload, 10, width=4, height=4, step=8)

        converted = OpenCvYuyvConverter(width=2, height=2).convert(source)

        self.assertEqual(converted.data[:6], b"\x00" * 6)
        self.assertEqual(converted.data[6:], b"\xff" * 6)


if __name__ == "__main__":
    unittest.main()
