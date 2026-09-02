from __future__ import annotations

from pathlib import Path
import unittest

from arx5_collection.dataset_pipeline.source.models import ArmSample
from arx5_collection.collection.capture import CaptureProfile
from arx5_collection.dataset_pipeline.source.models import CleaningPolicy
from arx5_collection.dataset_pipeline.source.models import EpisodeScan
from arx5_collection.dataset_pipeline.source.models import LEFT_ARM_TOPIC
from arx5_collection.dataset_pipeline.source.models import MessageRef
from arx5_collection.dataset_pipeline.source.models import REQUIRED_TOPICS
from arx5_collection.dataset_pipeline.source.models import RIGHT_ARM_TOPIC
from arx5_collection.dataset_pipeline.source.models import camera_topic
from arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.frame_alignment import (
    build_frame_groups,
)


def ref(topic: str, sequence: int, stamp: int) -> MessageRef:
    return MessageRef(topic, sequence, stamp, stamp)


def make_scan(right_camera_stamp: int = 100, arm_stamp: int = 99) -> EpisodeScan:
    refs = {topic: [] for topic in REQUIRED_TOPICS}
    for role, stamp in (("overview", 100), ("left", 95), ("right", right_camera_stamp)):
        for leaf in ("color", "aligned_depth"):
            topic = camera_topic(role, leaf)
            refs[topic].extend(
                (ref(topic, 0, 0), ref(topic, 1, stamp), ref(topic, 2, 200))
            )
    left_samples = (
        ArmSample(ref(LEFT_ARM_TOPIC, 0, 0), (0, 0, 0, 0, 0, 0), 0),
        ArmSample(ref(LEFT_ARM_TOPIC, 1, arm_stamp), (1, 2, 3, 4, 5, 6), 0.1),
        ArmSample(ref(LEFT_ARM_TOPIC, 2, 200), (1, 2, 3, 4, 5, 6), 0.1),
    )
    right_samples = (
        ArmSample(ref(RIGHT_ARM_TOPIC, 0, 0), (0, 0, 0, 0, 0, 0), 0),
        ArmSample(ref(RIGHT_ARM_TOPIC, 1, arm_stamp), (7, 8, 9, 10, 11, 12), 0.2),
        ArmSample(ref(RIGHT_ARM_TOPIC, 2, 200), (7, 8, 9, 10, 11, 12), 0.2),
    )
    refs[LEFT_ARM_TOPIC].extend(sample.ref for sample in left_samples)
    refs[RIGHT_ARM_TOPIC].extend(sample.ref for sample in right_samples)
    return EpisodeScan(
        episode_dir=Path("/episode"),
        refs_by_topic={topic: tuple(items) for topic, items in refs.items()},
        left_arm=left_samples,
        right_arm=right_samples,
        topic_types={},
    )


class PairingTest(unittest.TestCase):
    def test_builds_causal_group(self) -> None:
        result = build_frame_groups(
            make_scan(),
            CleaningPolicy(cross_camera_tolerance_ns=10, arm_max_age_ns=2),
        )

        self.assertEqual(len(result.frame_groups), 3)
        group = result.frame_groups[1]
        self.assertEqual(group.observation_cutoff_ns, 100)
        self.assertEqual(group.left_arm.ref.header_stamp_ns, 99)
        self.assertEqual(result.coverage, 1.0)

    def test_rejects_camera_outside_tolerance(self) -> None:
        result = build_frame_groups(
            make_scan(right_camera_stamp=120),
            CleaningPolicy(cross_camera_tolerance_ns=10, arm_max_age_ns=2),
        )

        self.assertEqual(len(result.frame_groups), 2)
        self.assertEqual(result.rejected_cross_camera, 1)

    def test_rejects_stale_arm(self) -> None:
        result = build_frame_groups(
            make_scan(arm_stamp=90),
            CleaningPolicy(cross_camera_tolerance_ns=10, arm_max_age_ns=2),
        )

        self.assertEqual(len(result.frame_groups), 2)
        self.assertEqual(result.rejected_arm_age, 1)

    def test_rgb_only_groups_real_color_frames_without_depth_stub(self) -> None:
        rgbd_scan = make_scan()
        refs = {
            topic: items
            for topic, items in rgbd_scan.refs_by_topic.items()
            if "aligned_depth" not in topic
        }
        scan = EpisodeScan(
            episode_dir=rgbd_scan.episode_dir,
            refs_by_topic=refs,
            left_arm=rgbd_scan.left_arm,
            right_arm=rgbd_scan.right_arm,
            topic_types={},
            capture_profile=CaptureProfile.RGB_ONLY,
        )

        result = build_frame_groups(
            scan,
            CleaningPolicy(cross_camera_tolerance_ns=10, arm_max_age_ns=2),
        )

        self.assertEqual(len(result.frame_groups), 3)
        self.assertIsNone(result.frame_groups[1].overview.depth)
        self.assertEqual(result.camera_stats["overview"].color_only_count, 0)


if __name__ == "__main__":
    unittest.main()
