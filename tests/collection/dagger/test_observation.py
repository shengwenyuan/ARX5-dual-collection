from __future__ import annotations

import unittest

from arx5_collection.collection.dagger.observation import (
    Pi05ObservationEncoder,
    RawArmSample,
    RgbFrame,
    VlaObservationStep,
)
from arx5_collection.common.gripper import ARX5_GRIPPER_CALIBRATION


def frame(stamp_ns: int) -> RgbFrame:
    return RgbFrame(b"\x00" * 12, stamp_ns, width=2, height=2)


def arm(stamp_ns: int, value: float = 0.0) -> RawArmSample:
    return RawArmSample((value,) * 6, value, stamp_ns)


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
        encoder = Pi05ObservationEncoder(ARX5_GRIPPER_CALIBRATION)

        observation = encoder.encode(step)

        self.assertEqual(
            observation.state,
            (-3.4,) * 6 + (0.0,) + (0.0,) * 6 + (1.0,),
        )
        self.assertEqual(observation.cutoff_ns, 1000)


if __name__ == "__main__":
    unittest.main()
