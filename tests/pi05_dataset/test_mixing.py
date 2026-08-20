from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from arx5_collection.artifacts import read_json
from arx5_collection.artifacts import read_jsonl
from arx5_collection.pi05_dataset.mixing import mix_selections


def make_selection(
    root: Path,
    episode: str,
    collection_type: str,
    *,
    task: str = "Stacking paper cups",
) -> Path:
    root.mkdir(parents=True)
    segment_id = f"{episode}--000"
    contract = {
        "filter_version": "pi05-arx-filter-v2-equal-eef-distance",
        "state_action_version": "arx5-measured-position-proxy-v1",
        "gripper_calibration": {"left": {}, "right": {}},
        "sampling_contract": {"mode": "equal_eef_distance"},
        "excluded_episodes": [],
    }
    (root / "selection.json").write_text(json.dumps(contract))
    (root / "sample_index.jsonl").write_text(
        json.dumps(
            {
                "source_episode_id": episode,
                "source_sample_index": 0,
                "training_eligible": True,
                "segment_id": segment_id,
            }
        )
        + "\n"
    )
    (root / "segments.jsonl").write_text(
        json.dumps(
            {
                "segment_id": segment_id,
                "source_episode_id": episode,
                "task": task,
            }
        )
        + "\n"
    )
    (root / "source_manifest.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "segment_id": segment_id,
                "source_episode_id": episode,
                "split_group": episode,
                "collection_type": collection_type,
                "training_class": (
                    "demonstration"
                    if collection_type == "demonstration"
                    else "expert_correction"
                ),
                "sample_weight": 1.0,
            }
        )
        + "\n"
    )
    return root


class SelectionMixingTest(unittest.TestCase):
    def test_merges_without_duplication_and_records_future_weights(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            demo = make_selection(root / "demo", "demo-a", "demonstration")
            dagger = make_selection(root / "dagger", "dagger-a", "dagger")

            output = mix_selections(
                {"demonstration": demo, "dagger": dagger},
                root / "mixed",
                {"demonstration": 1.0, "dagger": 2.0},
            )
            samples = read_jsonl(output / "sample_index.jsonl")
            sources = read_jsonl(output / "source_manifest.jsonl")
            report = read_json(output / "selection.json")

        self.assertEqual(len(samples), 2)
        self.assertEqual(
            {row["mixture_source"]: row["sample_weight"] for row in sources},
            {"demonstration": 1.0, "dagger": 2.0},
        )
        self.assertFalse(report["weighting_applied"])
        self.assertEqual(report["source_composition"], {"dagger": 1, "demonstration": 1})

    def test_rejects_incompatible_selection_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            demo = make_selection(root / "demo", "demo-a", "demonstration")
            dagger = make_selection(root / "dagger", "dagger-a", "dagger")
            report = read_json(dagger / "selection.json")
            report["filter_version"] = "different"
            (dagger / "selection.json").write_text(json.dumps(report))

            with self.assertRaisesRegex(ValueError, "contract mismatch"):
                mix_selections(
                    {"demonstration": demo, "dagger": dagger},
                    root / "mixed",
                )

    def test_rejects_task_prompt_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            demo = make_selection(root / "demo", "demo-a", "demonstration")
            dagger = make_selection(
                root / "dagger",
                "dagger-a",
                "dagger",
                task="stacking five paper cups",
            )

            with self.assertRaisesRegex(ValueError, "contract mismatch.*task"):
                mix_selections(
                    {"demonstration": demo, "dagger": dagger},
                    root / "mixed",
                )


if __name__ == "__main__":
    unittest.main()
