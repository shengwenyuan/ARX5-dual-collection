from __future__ import annotations

from pathlib import Path
import unittest

from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import EpisodeScan
from arx5_collection.cleaning.models import FrameGroup
from arx5_collection.cleaning.models import ImagePair
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.eef_selection import build_equal_eef_samples
from arx5_collection.pi05_dataset.eef_selection import EqualEefPolicy


def ref(topic: str, sequence: int, stamp: int) -> MessageRef:
    return MessageRef(topic, sequence, stamp, stamp)


def arm(
    side: str,
    sequence: int,
    stamp: int,
    *,
    x: float = 0.0,
    gripper: float = 0.0,
) -> ArmSample:
    return ArmSample(
        ref(f"/{side}", sequence, stamp),
        (0.0,) * 6,
        gripper,
        (x, 0.0, 0.0, 0.0, 0.0, 0.0),
    )


def pair(role: str, sequence: int, stamp: int) -> ImagePair:
    return ImagePair(
        ref(f"/{role}/color", sequence, stamp),
        ref(f"/{role}/depth", sequence, stamp),
    )


def group(index: int, stamp: int, left: ArmSample, right: ArmSample) -> FrameGroup:
    return FrameGroup(
        index,
        pair("overview", index, stamp),
        pair("left", index, stamp),
        pair("right", index, stamp),
        stamp,
        left,
        right,
    )


def scan(left: tuple[ArmSample, ...], right: tuple[ArmSample, ...]) -> EpisodeScan:
    return EpisodeScan(Path("/episode"), {}, left, right, {})


class EqualEefSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calibration = GripperCalibration(0.0, 1.0, tolerance=1.0)

    def test_samples_when_either_arm_crosses_endpoint_distance(self) -> None:
        left = tuple(
            arm("left", index, index, x=x)
            for index, x in enumerate((0.0, 0.003, 0.0051, 0.0051))
        )
        right = tuple(
            arm("right", index, index, x=x)
            for index, x in enumerate((0.0, 0.0, 0.0, 0.0052))
        )
        policy = EqualEefPolicy(
            eef_distance_m=0.005,
            gripper_delta_threshold=0.5,
            max_sample_interval_ns=100,
            image_max_age_ns=10,
            arm_max_age_ns=0,
        )

        samples = build_equal_eef_samples(
            scan(left, right),
            (group(0, 0, left[0], right[0]),),
            self.calibration,
            self.calibration,
            policy,
        )

        self.assertEqual([sample.tick_ns for sample in samples], [0, 2, 3])
        self.assertEqual(samples[1].sampling_reasons, ("eef_distance",))
        self.assertAlmostEqual(samples[1].left_eef_delta_m, 0.0051)
        self.assertAlmostEqual(samples[2].right_eef_delta_m, 0.0052)

    def test_gripper_change_and_max_interval_are_independent_triggers(self) -> None:
        left = (
            arm("left", 0, 0),
            arm("left", 1, 2, gripper=0.03),
            arm("left", 2, 6, gripper=0.03),
        )
        right = tuple(arm("right", index, stamp) for index, stamp in enumerate((0, 2, 6)))
        policy = EqualEefPolicy(
            eef_distance_m=0.005,
            gripper_delta_threshold=0.02,
            max_sample_interval_ns=4,
            image_max_age_ns=10,
            arm_max_age_ns=0,
        )

        samples = build_equal_eef_samples(
            scan(left, right),
            (group(0, 0, left[0], right[0]),),
            self.calibration,
            self.calibration,
            policy,
        )

        self.assertEqual([sample.tick_ns for sample in samples], [0, 2, 6])
        self.assertEqual(samples[1].sampling_reasons, ("gripper",))
        self.assertEqual(samples[2].sampling_reasons, ("max_interval",))

    def test_uses_latest_complete_frame_group_not_future_images(self) -> None:
        left = (
            arm("left", 0, 0, x=0.0),
            arm("left", 1, 2, x=0.006),
            arm("left", 2, 4, x=0.012),
        )
        right = tuple(arm("right", index, stamp) for index, stamp in enumerate((0, 2, 4)))
        groups = (
            group(0, 0, left[0], right[0]),
            group(1, 3, left[1], right[1]),
        )
        policy = EqualEefPolicy(
            image_max_age_ns=10,
            arm_max_age_ns=0,
            gripper_delta_threshold=0.5,
            max_sample_interval_ns=100,
        )

        samples = build_equal_eef_samples(
            scan(left, right),
            groups,
            self.calibration,
            self.calibration,
            policy,
        )

        self.assertEqual([sample.frame_group_id for sample in samples], [0, 0, 1])
        self.assertTrue(
            all(
                max(
                    sample.overview_color.header_stamp_ns,
                    sample.left_color.header_stamp_ns,
                    sample.right_color.header_stamp_ns,
                )
                <= sample.tick_ns
                for sample in samples
            )
        )

    def test_rejects_tick_when_complete_frame_group_is_too_old(self) -> None:
        left = (arm("left", 0, 0), arm("left", 1, 2, x=0.006))
        right = (arm("right", 0, 0), arm("right", 1, 2))
        policy = EqualEefPolicy(
            image_max_age_ns=1,
            arm_max_age_ns=0,
            gripper_delta_threshold=0.5,
            max_sample_interval_ns=100,
        )

        samples = build_equal_eef_samples(
            scan(left, right),
            (group(0, 0, left[0], right[0]),),
            self.calibration,
            self.calibration,
            policy,
        )

        self.assertEqual([sample.tick_ns for sample in samples], [0])


if __name__ == "__main__":
    unittest.main()
