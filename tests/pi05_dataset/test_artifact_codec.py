from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.artifact_codec import write_selection_artifacts
from arx5_collection.pi05_dataset.selection import Pi05Policy
from arx5_collection.pi05_dataset.selection import Pi05Sample
from arx5_collection.pi05_dataset.selection import Pi05Segment
from arx5_collection.pi05_dataset.selection_pipeline import DatasetSelection
from arx5_collection.pi05_dataset.selection_pipeline import EpisodeSelection


class SelectionArtifactCodecTest(unittest.TestCase):
    def test_writes_v1_selection_contract(self) -> None:
        image = MessageRef("/camera", 1, 100, 101)
        arm_ref = MessageRef("/arm", 2, 100, 102)
        arm = ArmSample(arm_ref, (0.0,) * 6, 0.0)
        state = (0.0,) * 14
        sample = Pi05Sample(0, 100, 7, image, image, image, arm, arm, state, state)
        segment = Pi05Segment(0, 0, 1, (sample,))
        episode = EpisodeSelection("episode-a", "task", (sample,), (segment,))
        selection = DatasetSelection((episode,), ())

        with tempfile.TemporaryDirectory() as temporary:
            output = write_selection_artifacts(
                Path(temporary),
                selection,
                Pi05Policy(),
                GripperCalibration(-1, 0),
                GripperCalibration(-1, 0),
            )
            sample_row = json.loads((output / "sample_index.jsonl").read_text())
            report = json.loads((output / "selection.json").read_text())

        self.assertEqual(sample_row["schema_version"], 1)
        self.assertEqual(sample_row["filter_version"], "pi05-arx-filter-v1")
        self.assertEqual(sample_row["images"]["cam_high"]["header_stamp_ns"], 100)
        self.assertEqual(sample_row["arms"]["left"]["bag_timestamp_ns"], 102)
        self.assertTrue(sample_row["training_eligible"])
        self.assertEqual(report["eligible_sample_count"], 1)


if __name__ == "__main__":
    unittest.main()
