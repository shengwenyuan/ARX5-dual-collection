from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from arx5_collection.lerobot_recomposition.config import load_config
from arx5_collection.lerobot_recomposition.models import CompositionConfig
from arx5_collection.lerobot_recomposition.models import CompositionPlan
from arx5_collection.lerobot_recomposition.models import EpisodeDescriptor
from arx5_collection.lerobot_recomposition.models import OutputConfig
from arx5_collection.lerobot_recomposition.models import SelectedEpisode
from arx5_collection.lerobot_recomposition.models import SnapshotDescriptor
from arx5_collection.lerobot_recomposition.models import V3RuntimeConfig
from arx5_collection.lerobot_recomposition.planner import _validate_v3_shard_selection
from arx5_collection.lerobot_recomposition.planner import build_plan
from arx5_collection.lerobot_recomposition.v21 import build_v21
from arx5_collection.lerobot_recomposition.v21 import _rewrite_parquet
from arx5_collection.lerobot_recomposition.v3_worker import run as run_v3_worker
from arx5_collection.lerobot_recomposition.v3_worker import _ordered_whole_shard_groups
from arx5_collection.lerobot_recomposition.v3 import build_v3
from arx5_collection.lerobot_recomposition.task_compat import LEGACY_EIGHT_STREAM_DESCRIPTION


class CompositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manifest_order_is_frozen_and_v21_build_is_standalone(self) -> None:
        source_a = self._snapshot("source-a", ("segment-a0", "segment-a1"), "task A")
        source_b = self._snapshot("source-b", ("segment-b0",), "task B")
        selection = self.root / "selection.jsonl"
        _write_jsonl(
            selection,
            [
                {
                    "segment_id": "segment-a1",
                    "source_episode_id": "episode-segment-a1",
                    "source_session_id": "session-segment-a1",
                    "expected_lerobot_episode_index": 1,
                },
                {
                    "segment_id": "segment-a0",
                    "source_episode_id": "episode-segment-a0",
                    "source_session_id": "session-segment-a0",
                    "expected_lerobot_episode_index": 0,
                },
            ],
        )
        config_path = self._config(source_a, source_b, selection)
        plan = build_plan(load_config(config_path))
        self.assertEqual(
            [item.episode.provenance["segment_id"] for item in plan.selected],
            ["segment-a1", "segment-a0", "segment-b0"],
        )
        source_video = Path(plan.selected[0].episode.physical["videos"]["cam"])
        original_bytes = source_video.read_bytes()

        def rewrite(source, target, _local, _global, _offset, local_tasks, global_tasks):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            return [global_tasks[local_tasks[0]]] * 2

        with patch(
            "arx5_collection.lerobot_recomposition.v21._rewrite_parquet",
            side_effect=rewrite,
        ):
            result = build_v21(
                plan,
                validator=lambda *_args, **_kwargs: {"status": "ready"},
            )

        self.assertEqual(result.episode_count, 3)
        self.assertEqual(result.tasks, ("task A", "task B"))
        output_rows = _read_jsonl(result.output_path / "reports/source_manifest.jsonl")
        self.assertEqual([row["lerobot_episode_index"] for row in output_rows], [0, 1, 2])
        self.assertEqual(output_rows[0]["source_lerobot_episode_index"], 1)
        output_video = result.output_path / "videos/chunk-000/cam/episode_000000.mp4"
        self.assertEqual(output_video.stat().st_ino, source_video.stat().st_ino)
        self.assertEqual(source_video.read_bytes(), original_bytes)
        self.assertEqual(_read(result.output_path / "snapshot.json")["status"], "committed")

    def test_selection_identity_drift_is_rejected_before_output(self) -> None:
        source = self._snapshot("source-a", ("segment-a0",), "task")
        selection = self.root / "selection.jsonl"
        _write_jsonl(
            selection,
            [{
                "segment_id": "segment-a0",
                "source_episode_id": "wrong",
                "source_session_id": "session-segment-a0",
                "expected_lerobot_episode_index": 0,
            }],
        )
        output = self.root / "output"
        config = self.root / "composition.toml"
        config.write_text(
            _toml(output, [("one", source, f'selection_manifest = "{selection}"')])
        )
        with self.assertRaisesRegex(ValueError, "identity drift"):
            build_plan(load_config(config))
        self.assertFalse(output.exists())

    def test_one_snapshot_may_preserve_multiple_episode_task_descriptions(self) -> None:
        source = self._snapshot("multi-task", ("segment-a", "segment-b"), "task A")
        _write_jsonl(
            source / "meta/tasks.jsonl",
            [
                {"task_index": 0, "task": "task A"},
                {"task_index": 1, "task": "task B"},
            ],
        )
        episodes = _read_jsonl(source / "meta/episodes.jsonl")
        episodes[1]["tasks"] = ["task B"]
        _write_jsonl(source / "meta/episodes.jsonl", episodes)
        info = _read(source / "meta/info.json")
        info["total_tasks"] = 2
        _write(source / "meta/info.json", info)
        config = self.root / "composition.toml"
        config.write_text(_toml(self.root / "output", [("multi", source, "select_all = true")]))

        plan = build_plan(load_config(config))

        self.assertEqual(plan.tasks, ("task A", "task B"))
        self.assertEqual([item.episode.tasks for item in plan.selected], [("task A",), ("task B",)])

    def test_historical_generic_bos_task_is_temporarily_mapped_to_fold_cloth(self) -> None:
        source = self._snapshot(
            "legacy-fold",
            ("segment-a",),
            LEGACY_EIGHT_STREAM_DESCRIPTION,
        )
        config = self.root / "composition.toml"
        config.write_text(_toml(self.root / "output", [("legacy", source, "select_all = true")]))

        plan = build_plan(load_config(config))

        self.assertEqual(plan.tasks, ("folding the cloth",))
        self.assertEqual(plan.selected[0].source.tasks, {0: "folding the cloth"})
        self.assertEqual(
            plan.contract["temporary_task_aliases"],
            {LEGACY_EIGHT_STREAM_DESCRIPTION: "folding the cloth"},
        )

    def test_materialization_failure_preserves_diagnostic_staging_without_commit_marker(self) -> None:
        source = self._snapshot("source-a", ("segment-a0",), "task")
        config = self.root / "composition.toml"
        output = self.root / "output"
        config.write_text(_toml(output, [("one", source, "select_all = true")]))
        plan = build_plan(load_config(config))

        with patch(
            "arx5_collection.lerobot_recomposition.v21._rewrite_parquet",
            side_effect=RuntimeError("synthetic failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                build_v21(plan, validator=lambda *_args, **_kwargs: {})

        staging = list(self.root.glob(".output.composition.*"))
        self.assertEqual(len(staging), 1)
        self.assertTrue((staging[0] / "reports/failure.json").is_file())
        self.assertFalse((staging[0] / "snapshot.json").exists())
        self.assertFalse(output.exists())

    def test_v3_partial_shared_video_shard_is_rejected(self) -> None:
        source = SnapshotDescriptor(
            "v3",
            self.root / "v3",
            "local/v3",
            "lerobot-v3.0",
            "fingerprint",
            {"features": {"cam": {"dtype": "video"}}},
            {"recipe": {"name": "recipe"}},
            tuple(
                EpisodeDescriptor(
                    index,
                    2,
                    ("task",),
                    {
                        "segment_id": f"segment-{index}",
                        "source_episode_id": f"episode-{index}",
                        "source_session_id": f"session-{index}",
                    },
                    {"video_shards": {"cam": [0, 0]}},
                )
                for index in range(2)
            ),
            {0: "task"},
            {},
        )
        with self.assertRaisesRegex(ValueError, "cuts shared video shard"):
            _validate_v3_shard_selection((SelectedEpisode(source, source.episodes[0]),))
        with self.assertRaisesRegex(ValueError, "reorders Episodes"):
            _validate_v3_shard_selection(
                (
                    SelectedEpisode(source, source.episodes[1]),
                    SelectedEpisode(source, source.episodes[0]),
                )
            )

    def test_v3_worker_splits_complete_shard_components_in_recipe_order(self) -> None:
        class Metadata:
            total_episodes = 4
            video_keys = ["cam"]
            episodes = [
                {"videos/cam/chunk_index": 0, "videos/cam/file_index": index // 2}
                for index in range(4)
            ]

        self.assertEqual(_ordered_whole_shard_groups(Metadata(), [2, 3, 0, 1]), [[2, 3], [0, 1]])
        with self.assertRaisesRegex(ValueError, "cannot reorder"):
            _ordered_whole_shard_groups(Metadata(), [3, 2])

    def test_v3_worker_refuses_unpinned_runtime_before_any_dataset_write(self) -> None:
        with patch(
            "arx5_collection.lerobot_recomposition.v3_worker.version",
            return_value="0.7.0",
        ):
            with self.assertRaisesRegex(RuntimeError, "requires lerobot==0.6.1"):
                run_v3_worker({"operation": "compose"})

    def test_v3_main_process_atomically_publishes_worker_dataset_and_sidecars(self) -> None:
        source = SnapshotDescriptor(
            "v3",
            self.root / "source",
            "local/source",
            "lerobot-v3.0",
            "source-fingerprint",
            {"features": {}},
            {"recipe": {"name": "pi05"}},
            (),
            {0: "task"},
            {},
        )
        episode = EpisodeDescriptor(
            0,
            2,
            ("task",),
            {
                "segment_id": "segment",
                "source_episode_id": "episode",
                "source_session_id": "session",
            },
            {"video_shards": {}},
        )
        output = self.root / "v3-output"
        config = CompositionConfig(
            1,
            OutputConfig("lerobot-v3.0", "local/v3-output", output),
            (),
            V3RuntimeConfig(Path(sys.executable)),
            self.root / "config.toml",
        )
        plan = CompositionPlan(
            config,
            (SelectedEpisode(source, episode),),
            ("task",),
            2,
            0,
            {"info": {}, "recipe": {"name": "pi05"}},
            "plan-fingerprint",
        )

        def compose(request):
            worker_output = Path(request["output"])
            (worker_output / "meta").mkdir(parents=True)
            (worker_output / "meta/info.json").write_text('{"codebase_version":"v3.0"}\n')
            return {
                "codebase_version": "v3.0",
                "episodes": 1,
                "frames": 2,
                "tasks": ["task"],
                "video_files": 0,
                "operations": {"video_reencode": 0},
                "validation": {"dataset": "loaded"},
            }

        with (
            patch(
                "arx5_collection.lerobot_recomposition.v3._materialize_worker_sources",
                return_value=[],
            ),
            patch(
                "arx5_collection.lerobot_recomposition.v3.V3WorkerClient.compose",
                side_effect=compose,
            ),
        ):
            result = build_v3(plan)

        self.assertEqual(result.output_path, output)
        self.assertTrue((output / "meta/info.json").is_file())
        self.assertEqual(_read(output / "snapshot.json")["status"], "committed")
        self.assertEqual(
            _read_jsonl(output / "reports/source_manifest.jsonl")[0]["segment_id"],
            "segment",
        )

    @unittest.skipUnless(importlib.util.find_spec("pyarrow"), "pyarrow is not installed")
    def test_real_arrow_rewrite_preserves_schema_and_reindexes_only_contract_columns(self) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        source = self.root / "source.parquet"
        target = self.root / "output/reindexed.parquet"
        table = pa.table(
            {
                "episode_index": pa.array([4, 4], type=pa.int64()),
                "frame_index": pa.array([0, 1], type=pa.int64()),
                "index": pa.array([20, 21], type=pa.int64()),
                "task_index": pa.array([1, 1], type=pa.int64()),
                "observation.state": pa.array([[1.0], [2.0]], type=pa.list_(pa.float32())),
            }
        )
        pq.write_table(table, source)

        mapped = _rewrite_parquet(source, target, 4, 9, 100, {1: "task"}, {"task": 3})

        output = pq.read_table(target)
        self.assertEqual(mapped, [3, 3])
        self.assertEqual(output.schema, table.schema)
        self.assertEqual(output["episode_index"].to_pylist(), [9, 9])
        self.assertEqual(output["frame_index"].to_pylist(), [0, 1])
        self.assertEqual(output["index"].to_pylist(), [100, 101])
        self.assertEqual(output["task_index"].to_pylist(), [3, 3])
        self.assertEqual(output["observation.state"].to_pylist(), [[1.0], [2.0]])

    def _config(self, source_a: Path, source_b: Path, selection: Path) -> Path:
        output = self.root / "output"
        config = self.root / "composition.toml"
        config.write_text(
            _toml(
                output,
                [
                    ("one", source_a, f'selection_manifest = "{selection}"'),
                    ("two", source_b, "select_all = true"),
                ],
            )
        )
        return config

    def _snapshot(self, name: str, segments: tuple[str, ...], task: str) -> Path:
        root = self.root / name
        (root / "meta").mkdir(parents=True)
        (root / "reports").mkdir()
        features = {
            "observation.state": {"dtype": "float32", "shape": [1], "names": [["joint"]]},
            "action": {"dtype": "float32", "shape": [1], "names": [["joint"]]},
            "cam": {"dtype": "video", "shape": [3, 2, 2], "names": ["c", "h", "w"]},
        }
        info = {
            "codebase_version": "v2.1",
            "robot_type": "arx5_dual",
            "fps": 50,
            "chunks_size": 1000,
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": features,
            "total_episodes": len(segments),
            "total_frames": len(segments) * 2,
            "total_tasks": 1,
            "total_videos": len(segments),
            "total_chunks": 1,
            "splits": {"train": f"0:{len(segments)}"},
        }
        _write(root / "meta/info.json", info)
        _write(
            root / "snapshot.json",
            {
                "schema_version": 2,
                "status": "committed",
                "repo_id": f"local/{name}",
                "builder_backend": "lerobot-v2.1",
                "recipe": {"name": "pi05", "gripper_contract": "arx5-gripper-v1"},
            },
        )
        episodes = []
        stats = []
        provenance = []
        for index, segment in enumerate(segments):
            data = root / f"data/chunk-000/episode_{index:06d}.parquet"
            video = root / f"videos/chunk-000/cam/episode_{index:06d}.mp4"
            data.parent.mkdir(parents=True, exist_ok=True)
            video.parent.mkdir(parents=True, exist_ok=True)
            data.write_bytes(f"data-{segment}".encode())
            video.write_bytes(f"video-{segment}".encode())
            episodes.append({"episode_index": index, "tasks": [task], "length": 2})
            stats.append({
                "episode_index": index,
                "stats": {
                    "episode_index": _stats([index, index]),
                    "index": _stats([index * 2, index * 2 + 1]),
                    "task_index": _stats([0, 0]),
                },
            })
            provenance.append({
                "schema_version": 1,
                "segment_id": segment,
                "source_episode_id": f"episode-{segment}",
                "source_session_id": f"session-{segment}",
                "split_group": f"episode-{segment}",
                "collection_type": "demonstration",
                "training_class": "demonstration",
                "intervention_id": None,
                "authority_segment_id": None,
                "source_started_bag_timestamp_ns": None,
                "source_ended_bag_timestamp_ns": None,
                "sample_weight": 1.0,
                "lerobot_episode_index": index,
            })
        _write_jsonl(root / "meta/episodes.jsonl", episodes)
        _write_jsonl(root / "meta/episodes_stats.jsonl", stats)
        _write_jsonl(root / "meta/tasks.jsonl", [{"task_index": 0, "task": task}])
        _write_jsonl(root / "reports/source_manifest.jsonl", provenance)
        return root


def _toml(output: Path, sources: list[tuple[str, Path, str]]) -> str:
    blocks = [
        "schema_version = 1",
        "",
        "[output]",
        'backend = "lerobot-v2.1"',
        'repo_id = "local/output"',
        f'path = "{output}"',
    ]
    for name, path, selection in sources:
        blocks.extend(["", "[[sources]]", f'name = "{name}"', f'path = "{path}"', selection])
    return "\n".join(blocks) + "\n"


def _stats(values: list[int]) -> dict[str, list[int | float]]:
    return {
        "min": [min(values)],
        "max": [max(values)],
        "mean": [sum(values) / len(values)],
        "std": [0.0],
        "count": [len(values)],
    }


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value) + "\n")


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


if __name__ == "__main__":
    unittest.main()
