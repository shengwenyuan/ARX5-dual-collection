from __future__ import annotations

from pathlib import Path
import unittest

from arx5_collection.dataset_pipeline.source.models import ArmSample
from arx5_collection.dataset_pipeline.source.models import EpisodeScan
from arx5_collection.dataset_pipeline.source.models import FrameGroup
from arx5_collection.dataset_pipeline.source.models import ImagePair
from arx5_collection.dataset_pipeline.source.models import MessageRef
from arx5_collection.common.gripper import GripperCalibration
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    Pi05Policy,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.models import (
    Pi05Sample,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.motion_segmenter import (
    build_samples,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.motion_segmenter import (
    select_nonidle_segments,
)


def ref(topic: str, sequence: int, stamp: int) -> MessageRef:
    return MessageRef(topic, sequence, stamp, stamp)


def arm(topic: str, sequence: int, stamp: int, position: float) -> ArmSample:
    return ArmSample(ref(topic, sequence, stamp), (position,) * 6, position)


def pair(role: str, sequence: int, stamp: int) -> ImagePair:
    return ImagePair(
        ref(f"/{role}/color", sequence, stamp),
        ref(f"/{role}/depth", sequence, stamp),
    )


class SelectionTest(unittest.TestCase):
    def test_builds_50hz_samples_with_past_images_and_arms(self) -> None:
        left_arm = tuple(
            arm("/left", i, stamp, i / 100)
            for i, stamp in enumerate(range(80, 201, 10))
        )
        right_arm = tuple(
            arm("/right", i, stamp, i / 100)
            for i, stamp in enumerate(range(80, 201, 10))
        )
        groups = (
            FrameGroup(
                0,
                pair("overview", 0, 100),
                pair("left", 0, 100),
                pair("right", 0, 100),
                100,
                left_arm[2],
                right_arm[2],
            ),
            FrameGroup(
                1,
                pair("overview", 1, 140),
                pair("left", 1, 140),
                pair("right", 1, 140),
                140,
                left_arm[6],
                right_arm[6],
            ),
            FrameGroup(
                2,
                pair("overview", 2, 180),
                pair("left", 2, 180),
                pair("right", 2, 180),
                180,
                left_arm[10],
                right_arm[10],
            ),
        )
        scan = EpisodeScan(Path("/episode"), {}, left_arm, right_arm, {})
        policy = Pi05Policy(fps=50_000_000, image_max_age_ns=40, arm_max_age_ns=10)

        samples = build_samples(
            scan,
            groups,
            GripperCalibration(0, 1, open_tolerance=1, closed_tolerance=1),
            GripperCalibration(0, 1, open_tolerance=1, closed_tolerance=1),
            policy,
        )

        self.assertEqual(
            [sample.tick_ns for sample in samples], [100, 120, 140, 160, 180]
        )
        self.assertEqual([sample.frame_group_id for sample in samples], [0, 0, 1, 1, 2])
        self.assertTrue(
            all(
                sample.left_arm.ref.header_stamp_ns <= sample.tick_ns
                for sample in samples
            )
        )

    def test_filters_long_idle_and_trims_segment_end(self) -> None:
        reference = ref("/image", 0, 0)
        arm_sample = arm("/arm", 0, 0, 0)
        samples = []
        values = (
            [index * 0.1 for index in range(5)]
            + [0.4] * 5
            + [0.5 + index * 0.1 for index in range(7)]
        )
        for index, value in enumerate(values):
            state = (value,) * 14
            samples.append(
                Pi05Sample(
                    index,
                    index,
                    0,
                    reference,
                    reference,
                    reference,
                    arm_sample,
                    arm_sample,
                    state,
                    state,
                )
            )
        policy = Pi05Policy(
            min_idle_frames=3,
            min_motion_frames=3,
            trim_segment_end_frames=1,
            idle_delta_threshold=0.01,
        )

        segments = select_nonidle_segments(tuple(samples), policy)

        self.assertEqual(len(segments), 2)
        self.assertEqual([len(segment.samples) for segment in segments], [4, 6])


if __name__ == "__main__":
    unittest.main()
