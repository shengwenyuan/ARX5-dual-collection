from __future__ import annotations

import unittest

from arx5_collection.dagger.observation import (
    GripperCalibration,
    ObservationConstraints,
    ObservationFailureCode,
    ObservationUnavailableError,
    Pi05ObservationEncoder,
    RawArmSample,
    RgbFrame,
    YuyvFrame,
    select_causal_step,
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
    def test_selects_latest_strictly_causal_real_step(self) -> None:
        step = select_causal_step(
            (frame(990), frame(1020)),
            (frame(1000),),
            (frame(995),),
            (arm(990, 1.0), arm(1001, 9.0)),
            (arm(998, 2.0), arm(1002, 9.0)),
            now_ns=1010,
            constraints=ObservationConstraints(20, 15, 100),
        )

        self.assertEqual(step.cutoff_ns, 1000)
        self.assertEqual(step.camera_left.stamp_ns, 990)
        self.assertEqual(step.left_arm.stamp_ns, 990)
        self.assertEqual(step.right_arm.stamp_ns, 998)

    def test_accepts_arbitrary_unsynchronized_30hz_camera_phase(self) -> None:
        step = select_causal_step(
            (frame(984_000_000),),
            (frame(1_000_000_000),),
            (frame(1_016_000_000),),
            (arm(1_015_000_000),),
            (arm(1_015_500_000),),
            now_ns=1_020_000_000,
        )

        self.assertEqual(step.cutoff_ns, 1_016_000_000)

    def test_reports_camera_span_with_observed_and_limit(self) -> None:
        with self.assertRaises(ObservationUnavailableError) as raised:
            select_causal_step(
                (frame(900),),
                (frame(1000),),
                (frame(995),),
                (arm(995),),
                (arm(995),),
                now_ns=1010,
                constraints=ObservationConstraints(20, 15, 100),
            )
        self.assertEqual(
            raised.exception.code, ObservationFailureCode.CAMERA_SPAN_EXCEEDED
        )
        self.assertEqual(raised.exception.observed_ns, 100)
        self.assertEqual(raised.exception.limit_ns, 20)

    def test_reports_missing_buffer_snapshot_and_arm_failures(self) -> None:
        cases = (
            (
                ((), (frame(1000),), (frame(1000),), (arm(999),), (arm(999),)),
                1001,
                ObservationFailureCode.BUFFERS_NOT_READY,
            ),
            (
                (
                    (frame(1000),),
                    (frame(1000),),
                    (frame(1000),),
                    (arm(999),),
                    (arm(999),),
                ),
                1200,
                ObservationFailureCode.SNAPSHOT_STALE,
            ),
            (
                (
                    (frame(1000),),
                    (frame(1000),),
                    (frame(1000),),
                    (arm(900),),
                    (arm(999),),
                ),
                1001,
                ObservationFailureCode.LEFT_ARM_STALE,
            ),
            (
                (
                    (frame(1000),),
                    (frame(1000),),
                    (frame(1000),),
                    (arm(999),),
                    (arm(900),),
                ),
                1001,
                ObservationFailureCode.RIGHT_ARM_STALE,
            ),
        )
        constraints = ObservationConstraints(20, 15, 100)
        for streams, now_ns, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ObservationUnavailableError) as raised:
                    select_causal_step(*streams, now_ns, constraints)
                self.assertEqual(raised.exception.code, code)

    def test_encoder_is_model_specific_and_sampler_step_is_not(self) -> None:
        step = select_causal_step(
            (frame(1000),),
            (frame(1000),),
            (frame(1000),),
            (arm(999, -3.0),),
            (arm(999, 0.0),),
            now_ns=1001,
            constraints=ObservationConstraints(20, 15, 100),
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
