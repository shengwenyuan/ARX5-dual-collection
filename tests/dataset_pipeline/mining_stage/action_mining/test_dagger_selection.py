from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from arx5_collection.dataset_pipeline.persistence.artifacts import read_jsonl
from arx5_collection.dataset_pipeline.source.models import ArmSample
from arx5_collection.dataset_pipeline.source.models import CleaningResult
from arx5_collection.dataset_pipeline.source.models import EpisodeScan
from arx5_collection.dataset_pipeline.source.models import FrameGroup
from arx5_collection.dataset_pipeline.source.models import ImagePair
from arx5_collection.dataset_pipeline.source.models import MessageRef
from arx5_collection.dataset_pipeline.mining_stage.action_mining.episode_filter import (
    run as episode_filter,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.equal_eef_action_sampler import (
    run as equal_eef_action_sampler,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.motion_segmenter import (
    run as motion_segmenter,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.training_interval import (
    run as training_interval,
)
from arx5_collection.dataset_pipeline.mining_stage.action_mining.trajectory_labeler import (
    run as trajectory_labeler,
)
from arx5_collection.dataset_pipeline.execution.models import FileIdentity
from arx5_collection.dataset_pipeline.execution.models import StageReceipt
from arx5_collection.dataset_pipeline.configuration.recipe import DatasetPipelineRecipe
from arx5_collection.dataset_pipeline.configuration.recipe import UnitSpec
from arx5_collection.dataset_pipeline.execution.unit_runtime import (
    EpisodePipelineContext,
)


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
    def test_units_mine_only_complete_correction(self) -> None:
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
            recipe_path = root / "recipe.toml"
            recipe_path.write_text(
                (
                    Path(__file__)
                    .parents[4]
                    .joinpath("config/specs/recipes/pi05-equal-eef-v3.toml")
                    .read_text()
                    .replace("action_horizon = 50", "action_horizon = 1")
                    .replace("min_motion_frames = 54", "min_motion_frames = 1")
                    .replace(
                        "trim_segment_end_frames = 34",
                        "trim_segment_end_frames = 0",
                    )
                )
            )
            recipe = DatasetPipelineRecipe.load(recipe_path)
            correction = SimpleNamespace(
                intervention_id=1,
                segment_id="episode-a--authority-003",
                started_bag_timestamp_ns=10_010_000_000,
                ended_bag_timestamp_ns=10_050_000_000,
            )
            for outcome in ("success", "fail"):
                output_root = root / f"derived-{outcome}"
                audit = output_root / "audit" / "episode-a"
                audit.mkdir(parents=True)
                (audit / "frame_index.jsonl").touch()
                context = EpisodePipelineContext(
                    StageReceipt(
                        "episode-a",
                        "frozen/session",
                        episode,
                        episode,
                        FileIdentity(0, 0),
                        FileIdentity(0, 0),
                    ),
                    output_root,
                    "task",
                    "local/test",
                    recipe,
                    metadata={
                        "episode_id": "episode-a",
                        "collection_type": "dagger",
                    },
                    scan=scan,
                    cleaning=CleaningResult(
                        {"outcome": outcome, "grade": "A"},
                        groups,
                        audit,
                    ),
                    authority=SimpleNamespace(
                        valid=True,
                        expert_segments=(correction,),
                    ),
                )
                timed = lambda name, operation: operation()
                with patch(
                    "arx5_collection.dataset_pipeline.mining_stage.action_mining.training_interval.load_frame_groups",
                    return_value=groups,
                ):
                    training_interval(
                        context,
                        UnitSpec(
                            "training_interval",
                            {"max_episode_duration_s": 180.0},
                        ),
                        timed,
                    )
                equal_eef_action_sampler(
                    context,
                    UnitSpec("equal_eef_action_sampler", {}),
                    timed,
                )
                motion_segmenter(
                    context,
                    UnitSpec("motion_segmenter", {}),
                    timed,
                )
                trajectory_labeler(
                    context,
                    UnitSpec("trajectory_labeler", {}),
                    timed,
                )
                self.assertEqual(len(context.selection.episodes), 1)

            aborted = EpisodePipelineContext(
                StageReceipt(
                    "episode-a",
                    "frozen/session",
                    episode,
                    episode,
                    FileIdentity(0, 0),
                    FileIdentity(0, 0),
                ),
                root / "aborted",
                "task",
                "local/test",
                recipe,
                metadata={
                    "episode_id": "episode-a",
                    "collection_type": "dagger",
                },
                cleaning=CleaningResult(
                    {"outcome": "aborted", "grade": "A"},
                    (),
                ),
                authority=SimpleNamespace(
                    valid=True,
                    expert_segments=(correction,),
                ),
            )
            episode_filter(
                aborted,
                UnitSpec("episode_filter", {}),
                lambda name, operation: operation(),
            )
            self.assertEqual(aborted.selection.episodes, ())
            self.assertEqual(
                aborted.selection.excluded_episodes[0]["reason"],
                "outcome_aborted",
            )

            sources = read_jsonl(
                root / "derived-fail" / "selection" / "source_manifest.jsonl"
            )
            samples = read_jsonl(
                root / "derived-fail" / "selection" / "sample_index.jsonl"
            )

        self.assertEqual({row["collection_type"] for row in sources}, {"dagger"})
        self.assertEqual({row["intervention_id"] for row in sources}, {1})
        self.assertEqual(
            {row["source_session_id"] for row in sources},
            {"frozen/session"},
        )
        self.assertTrue(all(row["training_eligible"] for row in samples))
        self.assertEqual(len(samples), 4)


if __name__ == "__main__":
    unittest.main()
