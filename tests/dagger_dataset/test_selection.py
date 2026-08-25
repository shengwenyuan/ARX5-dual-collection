from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from arx5_collection.artifacts import read_jsonl
from arx5_collection.cleaning.models import ArmSample
from arx5_collection.cleaning.models import EpisodeScan
from arx5_collection.cleaning.models import FrameGroup
from arx5_collection.cleaning.models import ImagePair
from arx5_collection.cleaning.models import MessageRef
from arx5_collection.dagger_dataset.selection import select_equal_eef_dagger_dataset
from arx5_collection.pi05_dataset.actions import GripperCalibration
from arx5_collection.pi05_dataset.eef_selection import EqualEefPolicy


def make_scan_and_groups() -> tuple[EpisodeScan, tuple[FrameGroup, ...]]:
    left = []
    right = []
    groups = []
    for index in range(6):
        header = 1_000_000_000 + index * 10_000_000
        bag = 10_000_000_000 + index * 10_000_000
        left_ref = MessageRef("/left", index, header, bag)
        right_ref = MessageRef("/right", index, header, bag)
        left_sample = ArmSample(
            left_ref,
            (index * 0.01,) * 6,
            0.0,
            (index * 0.006, 0.0, 0.0, 0.0, 0.0, 0.0),
        )
        right_sample = ArmSample(
            right_ref,
            (index * 0.01,) * 6,
            0.0,
            (index * 0.006, 0.0, 0.0, 0.0, 0.0, 0.0),
        )
        left.append(left_sample)
        right.append(right_sample)
        image = MessageRef("/camera", index, header, bag)
        pair = ImagePair(image, image)
        groups.append(
            FrameGroup(
                index,
                pair,
                pair,
                pair,
                header,
                left_sample,
                right_sample,
            )
        )
    return (
        EpisodeScan(Path("/episode"), {}, tuple(left), tuple(right), {}),
        tuple(groups),
    )


class DaggerSelectionTest(unittest.TestCase):
    def test_runs_v2_recipe_only_inside_complete_correction(self) -> None:
        scan, groups = make_scan_and_groups()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            episode = root / "raw" / "episode-a"
            episode.mkdir(parents=True)
            (episode / "metadata.json").write_text(
                json.dumps(
                    {
                        "station": {"id": "w3"},
                        "timing": {"started_at": "2026-08-20T01:02:03Z"},
                    }
                )
            )
            audit = root / "audit" / "episode-a"
            authority = audit / "authority"
            authority.mkdir(parents=True)
            (audit / "frame_index.jsonl").touch()
            (authority / "quality.json").write_text(json.dumps({"valid": True}))
            (authority / "segments.jsonl").write_text(
                json.dumps(
                    {
                        "segment_id": "episode-a--authority-003",
                        "authority_class": "expert_correction",
                        "training_eligible": True,
                        "intervention_id": 1,
                        "started_bag_timestamp_ns": 10_010_000_000,
                        "ended_bag_timestamp_ns": 10_050_000_000,
                    }
                )
                + "\n"
            )
            policy = EqualEefPolicy(
                eef_distance_m=0.005,
                max_sample_interval_ns=100_000_000,
                action_horizon=1,
                min_motion_frames=1,
                trim_segment_end_frames=0,
            )
            for outcome in ("success", "fail"):
                (audit / "quality.json").write_text(
                    json.dumps({"outcome": outcome, "grade": "A"})
                )
                with (
                    patch(
                        "arx5_collection.dagger_dataset.selection.read_episode_scan",
                        return_value=scan,
                    ),
                    patch(
                        "arx5_collection.dagger_dataset.selection.load_frame_groups",
                        return_value=groups,
                    ),
                ):
                    result = select_equal_eef_dagger_dataset(
                        [episode],
                        root / "audit",
                        root / f"derived-{outcome}",
                        "task",
                        GripperCalibration(-1, 0),
                        GripperCalibration(-1, 0),
                        policy,
                    )
                self.assertEqual(len(result.episodes), 1)

            (audit / "quality.json").write_text(
                json.dumps({"outcome": "aborted", "grade": "A"})
            )
            aborted = select_equal_eef_dagger_dataset(
                [episode],
                root / "audit",
                root / "derived-aborted",
                "task",
                GripperCalibration(-1, 0),
                GripperCalibration(-1, 0),
                policy,
            )
            self.assertEqual(aborted.episodes, ())
            self.assertEqual(aborted.excluded_episodes[0]["reason"], "outcome_aborted")

            output = root / "derived-fail" / "selection"
            sources = read_jsonl(output / "source_manifest.jsonl")
            samples = read_jsonl(output / "sample_index.jsonl")

        self.assertEqual({row["collection_type"] for row in sources}, {"dagger"})
        self.assertEqual({row["intervention_id"] for row in sources}, {1})
        self.assertEqual({row["source_session_id"] for row in sources}, {"w3/2026-08-20/raw"})
        self.assertTrue(all(row["training_eligible"] for row in samples))
        self.assertEqual(len(samples), 4)


if __name__ == "__main__":
    unittest.main()
