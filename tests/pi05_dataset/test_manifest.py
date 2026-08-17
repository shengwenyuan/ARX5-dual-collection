from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import EpisodeScan
from arx5_collection.cleaning.models import LEFT_ARM_TOPIC
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.cleaning.models import REQUIRED_TOPICS
from arx5_collection.cleaning.models import RIGHT_ARM_TOPIC
from arx5_collection.cleaning.models import camera_topic
from arx5_collection.pi05_dataset.artifact_codec import load_frame_groups


def ref(topic: str, sequence: int, stamp: int) -> MessageRef:
    return MessageRef(topic, sequence, stamp, stamp + 1)


class ManifestTest(unittest.TestCase):
    def test_reconstructs_frame_group_from_immutable_references(self) -> None:
        refs = {topic: (ref(topic, 0, 100),) for topic in REQUIRED_TOPICS}
        left = ArmSample(refs[LEFT_ARM_TOPIC][0], (1, 2, 3, 4, 5, 6), 0.1)
        right = ArmSample(refs[RIGHT_ARM_TOPIC][0], (7, 8, 9, 10, 11, 12), 0.2)
        row = {
            "schema_version": 1,
            "frame_group_id": 0,
            "observation_cutoff_ns": 100,
            "images": {},
            "arms": {},
        }
        for role in ("overview", "left", "right"):
            row["images"][role] = {
                leaf: {
                    "topic": camera_topic(role, "color" if leaf == "color" else "aligned_depth"),
                    "sequence": 0,
                    "header_stamp_ns": 100,
                    "bag_timestamp_ns": 101,
                }
                for leaf in ("color", "depth")
            }
        for side, topic in (("left", LEFT_ARM_TOPIC), ("right", RIGHT_ARM_TOPIC)):
            row["arms"][side] = {
                "ref": {
                    "topic": topic,
                    "sequence": 0,
                    "header_stamp_ns": 100,
                    "bag_timestamp_ns": 101,
                }
            }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "frame_index.jsonl"
            path.write_text(json.dumps(row) + "\n")
            groups = load_frame_groups(
                path,
                EpisodeScan(Path("/episode"), refs, (left,), (right,), {}),
            )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].left_arm.joint_positions, (1, 2, 3, 4, 5, 6))
if __name__ == "__main__":
    unittest.main()
