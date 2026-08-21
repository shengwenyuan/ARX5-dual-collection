from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator

from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.artifact_codec import write_selection_artifacts
from arx5_collection.pi05_dataset.artifact_codec import write_equal_eef_selection_artifacts
from arx5_collection.pi05_dataset.eef_selection import EqualEefPolicy
from arx5_collection.pi05_dataset.eef_selection import EqualEefSample
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
            source_row = json.loads((output / "source_manifest.jsonl").read_text())

        self.assertEqual(sample_row["schema_version"], 1)
        self.assertEqual(sample_row["filter_version"], "pi05-arx-filter-v1")
        self.assertEqual(sample_row["images"]["cam_high"]["header_stamp_ns"], 100)
        self.assertEqual(sample_row["arms"]["left"]["bag_timestamp_ns"], 102)
        self.assertTrue(sample_row["training_eligible"])
        self.assertNotIn("sampling_reasons", sample_row)
        self.assertNotIn("delta_time_ns", sample_row)
        self.assertEqual(report["eligible_sample_count"], 1)
        source_schema = json.loads(
            (Path(__file__).parents[2] / "schemas/dataset-source-manifest-v1.json").read_text()
        )
        Draft202012Validator(source_schema).validate(source_row)
        self.assertEqual(source_row["collection_type"], "demonstration")
        self.assertEqual(source_row["source_session_id"], "episode-a")

    def test_writes_equal_eef_v2_provenance(self) -> None:
        image = MessageRef("/camera", 1, 100, 101)
        arm_ref = MessageRef("/arm", 2, 100, 102)
        arm = ArmSample(arm_ref, (0.0,) * 6, 0.0)
        state = (0.0,) * 14
        sample = EqualEefSample(
            0,
            100,
            7,
            image,
            image,
            image,
            arm,
            arm,
            state,
            state,
            delta_time_ns=20,
            sampling_reasons=("eef_distance",),
            left_eef_delta_m=0.0051,
        )
        segment = Pi05Segment(0, 0, 1, (sample,))
        selection = DatasetSelection((EpisodeSelection("episode-a", "task", (sample,), (segment,)),), ())

        with tempfile.TemporaryDirectory() as temporary:
            output = write_equal_eef_selection_artifacts(
                Path(temporary),
                selection,
                EqualEefPolicy(),
                GripperCalibration(-1, 0),
                GripperCalibration(-1, 0),
            )
            sample_row = json.loads((output / "sample_index.jsonl").read_text())
            segment_row = json.loads((output / "segments.jsonl").read_text())
            report = json.loads((output / "selection.json").read_text())

        root = Path(__file__).parents[2]
        sample_schema = json.loads((root / "schemas/pi05-sample-v2.json").read_text())
        segment_schema = json.loads((root / "schemas/pi05-segment-v2.json").read_text())
        Draft202012Validator(sample_schema).validate(sample_row)
        Draft202012Validator(segment_schema).validate(segment_row)
        self.assertEqual(sample_row["schema_version"], 2)
        self.assertEqual(sample_row["source_header_stamp_ns"], 100)
        self.assertEqual(sample_row["sampling_reasons"], ["eef_distance"])
        self.assertEqual(report["sampling_contract"]["horizon_semantics"], "trajectory_steps")


if __name__ == "__main__":
    unittest.main()
