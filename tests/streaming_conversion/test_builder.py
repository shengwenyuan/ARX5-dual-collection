from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from arx5_collection.streaming_conversion.builder import build_lerobot_v21_snapshot
from arx5_collection.streaming_conversion.config import OutputConfig
from arx5_collection.streaming_conversion.config import RecipeConfig
from arx5_collection.streaming_conversion.config import RuntimeConfig
from arx5_collection.streaming_conversion.config import SourceConfig
from arx5_collection.streaming_conversion.config import StreamingConversionConfig
from arx5_collection.streaming_conversion.manifest import RunManifest
from arx5_collection.streaming_conversion.models import DiscoveryResult
from arx5_collection.streaming_conversion.models import EpisodeCandidate
from arx5_collection.streaming_conversion.models import FileIdentity
from arx5_collection.streaming_conversion.models import JobState
from arx5_collection.streaming_conversion.recipe import Pi05ConversionRecipe


RECIPE = Path("config/conversion.pi05-equal-eef-v2.toml")


class LeRobotV21BuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.recipe = Pi05ConversionRecipe.load(RECIPE)
        self.output = self.root / "lerobot" / "snapshot"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builds_deterministic_multitask_snapshot_and_releases_run_cache(self) -> None:
        manifest = self._manifest(
            (("episode-a", "session-a"), ("episode-b", "session-b"))
        )
        self._commit(manifest, "episode-a", "session-a", "task A", "w3")
        self._commit(manifest, "episode-b", "session-b", "task B", "w4")
        (manifest.run_dir / "staging" / "episode-a").mkdir(parents=True)

        with self._patched_backend():
            result = build_lerobot_v21_snapshot(
                manifest,
                self.output,
                self.recipe,
                "local/fold",
            )

        self.assertEqual(result.tasks, ("task A", "task B"))
        self.assertEqual(result.episode_count, 2)
        self.assertEqual(result.frame_count, 4)
        info = _read(self.output / "meta" / "info.json")
        self.assertEqual(info["total_episodes"], 2)
        self.assertEqual(info["total_frames"], 4)
        self.assertEqual(info["total_tasks"], 2)
        self.assertEqual(info["total_videos"], 2)
        self.assertEqual(
            _read_jsonl(self.output / "meta" / "tasks.jsonl"),
            [
                {"task_index": 0, "task": "task A"},
                {"task_index": 1, "task": "task B"},
            ],
        )
        sources = _read_jsonl(self.output / "reports" / "source_manifest.jsonl")
        self.assertEqual(
            [row["lerobot_episode_index"] for row in sources],
            [0, 1],
        )
        snapshot = _read(self.output / "snapshot.json")
        self.assertEqual(snapshot["recipe"]["calibration_profiles"], ["w3", "w4"])
        validation = _read(self.output / "reports" / "validation.json")
        self.assertEqual(validation["dataset_root"], str(self.output))
        self.assertNotIn(f".{self.output.name}.", json.dumps(validation))
        self.assertFalse((manifest.run_dir / "staging").exists())
        self.assertFalse((manifest.run_dir / "fragments").exists())
        self.assertTrue((manifest.run_dir / "jobs.jsonl").is_file())
        self.assertEqual(
            (self.output / "videos/chunk-000/cam/episode_000000.mp4").read_bytes(),
            b"video-episode-a",
        )

    def test_rejects_task_mismatch_within_source_session(self) -> None:
        manifest = self._manifest(
            (("episode-a", "shared-session"), ("episode-b", "shared-session"))
        )
        self._commit(manifest, "episode-a", "shared-session", "task A", "w3")
        self._commit(manifest, "episode-b", "shared-session", "task B", "w4")

        with self.assertRaisesRegex(ValueError, "source Session"):
            build_lerobot_v21_snapshot(
                manifest,
                self.output,
                self.recipe,
                "local/fold",
            )

        self.assertFalse(self.output.exists())
        self.assertTrue((manifest.run_dir / "fragments").is_dir())

    def test_rejects_training_contract_drift(self) -> None:
        manifest = self._manifest(
            (("episode-a", "session-a"), ("episode-b", "session-b"))
        )
        self._commit(manifest, "episode-a", "session-a", "task", "w3")
        self._commit(manifest, "episode-b", "session-b", "task", "w4")
        fragment_path = manifest.run_dir / "fragments/episode-b/fragment.json"
        fragment = _read(fragment_path)
        fragment["contracts"]["filter_version"] = "drift"
        _write(fragment_path, fragment)

        with self.assertRaisesRegex(ValueError, "training contract drift"):
            build_lerobot_v21_snapshot(
                manifest,
                self.output,
                self.recipe,
                "local/fold",
            )

        self.assertFalse(self.output.exists())

    def test_failed_job_blocks_builder(self) -> None:
        manifest = self._manifest((("episode-a", "session-a"),))
        manifest.transition("episode-a", JobState.STAGING)
        manifest.transition(
            "episode-a",
            JobState.FAILED,
            reason_code="infrastructure/staging_io",
        )

        with self.assertRaisesRegex(RuntimeError, "failed Episodes"):
            build_lerobot_v21_snapshot(
                manifest,
                self.output,
                self.recipe,
                "local/fold",
            )

        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_overwritten(self) -> None:
        manifest = self._manifest((("episode-a", "session-a"),))
        self.output.mkdir(parents=True)

        with self.assertRaises(FileExistsError):
            build_lerobot_v21_snapshot(
                manifest,
                self.output,
                self.recipe,
                "local/fold",
            )

    def _manifest(self, entries: tuple[tuple[str, str], ...]) -> RunManifest:
        config = StreamingConversionConfig(
            1,
            SourceConfig(self.source_root, (Path("task"),), ()),
            RuntimeConfig(self.root / "streaming", 2),
            OutputConfig(self.root / "lerobot", "fold", "local/fold"),
            RecipeConfig("pi05-equal-eef-v2", str(RECIPE), "folding the cloth"),
        )
        candidates = tuple(
            EpisodeCandidate(
                source_dir=self.source_root / episode_id,
                relative_dir=Path(episode_id),
                include_path=Path("task"),
                episode_id=episode_id,
                source_session_id=session_id,
                collection_type="demonstration",
                outcome="success",
                task_id="task",
                task_description="record",
                mcap=FileIdentity(10, index + 1),
                metadata=FileIdentity(20, index + 11),
            )
            for index, (episode_id, session_id) in enumerate(entries)
        )
        discovery = DiscoveryResult(
            self.source_root,
            (self.source_root / "task",),
            candidates,
            (),
        )
        return RunManifest.create(
            config,
            discovery,
            self.output,
            "builder-test",
        )

    def _commit(
        self,
        manifest: RunManifest,
        episode_id: str,
        session_id: str,
        task: str,
        station: str,
    ) -> None:
        for state in (
            JobState.STAGING,
            JobState.CONVERTING,
            JobState.VALIDATING,
            JobState.COMMITTED,
        ):
            manifest.transition(episode_id, state)
        root = manifest.run_dir / "fragments" / episode_id
        dataset = root / "lerobot"
        data = dataset / "data/chunk-000/episode_000000.parquet"
        video = dataset / "videos/chunk-000/cam/episode_000000.mp4"
        data.parent.mkdir(parents=True)
        video.parent.mkdir(parents=True)
        data.write_bytes(b"parquet-placeholder")
        video.write_bytes(f"video-{episode_id}".encode())
        contracts = {
            "openpi_commit": "openpi",
            "lerobot_commit": "lerobot",
            "fps": 50,
            "mode": "video",
            "image_size": [640, 360],
            "image_color": "RGB",
            "state_action_order": ["joint"],
            "state_action_version": "state-v1",
            "filter_version": "filter-v1",
            "gripper_calibration": {"station": station},
            "sampling_contract": {"action_horizon": 50},
        }
        info = {
            "codebase_version": "v2.1",
            "robot_type": "arx5_dual",
            "total_episodes": 1,
            "total_frames": 2,
            "total_tasks": 1,
            "total_videos": 1,
            "total_chunks": 1,
            "chunks_size": 1000,
            "fps": 50,
            "splits": {"train": "0:1"},
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": (
                "videos/chunk-{episode_chunk:03d}/{video_key}/"
                "episode_{episode_index:06d}.mp4"
            ),
            "features": {
                "observation.state": {
                    "dtype": "float32",
                    "shape": [1],
                    "names": [["joint"]],
                },
                "action": {
                    "dtype": "float32",
                    "shape": [1],
                    "names": [["joint"]],
                },
                "cam": {
                    "dtype": "video",
                    "shape": [3, 360, 640],
                    "names": ["channels", "height", "width"],
                },
            },
        }
        meta = dataset / "meta"
        meta.mkdir()
        _write(meta / "info.json", info)
        _write_jsonl(
            meta / "episodes.jsonl",
            [{"episode_index": 0, "tasks": [task], "length": 2}],
        )
        _write_jsonl(
            meta / "episodes_stats.jsonl",
            [
                {
                    "episode_index": 0,
                    "stats": {
                        "episode_index": _stats([0, 0]),
                        "index": _stats([0, 1]),
                        "task_index": _stats([0, 0]),
                    },
                }
            ],
        )
        _write_jsonl(meta / "tasks.jsonl", [{"task_index": 0, "task": task}])
        reports = root / "reports"
        reports.mkdir()
        _write_jsonl(
            reports / "source_manifest.jsonl",
            [
                {
                    "schema_version": 1,
                    "segment_id": f"{episode_id}--000",
                    "source_episode_id": episode_id,
                    "source_session_id": session_id,
                    "split_group": episode_id,
                    "collection_type": "demonstration",
                    "training_class": "demonstration",
                    "intervention_id": None,
                    "authority_segment_id": None,
                    "source_started_bag_timestamp_ns": None,
                    "source_ended_bag_timestamp_ns": None,
                    "sample_weight": 1.0,
                    "lerobot_episode_index": 0,
                }
            ],
        )
        fragment = {
            "schema_version": 1,
            "status": "committed",
            "episode_id": episode_id,
            "source_dir": str(self.source_root / episode_id),
            "source_identity": {"mcap": {}, "metadata": {}},
            "collection_type": "demonstration",
            "outcome": "success",
            "metadata_task": {},
            "training_task": task,
            "source_session_ids": [session_id],
            "recipe": {
                "schema_version": 1,
                "name": "pi05-equal-eef-v2",
                "builder_backend": "lerobot-v2.1",
                "gripper_normalization": "linear_open_closed_0_1",
                "calibration_profile": station,
            },
            "repo_id": f"local/fold__{episode_id}",
            "segment_count": 1,
            "frame_count": 2,
            "contracts": contracts,
            "paths": {
                "dataset": "lerobot",
                "selection": "selection",
                "conversion_report": "reports/conversion.json",
                "source_manifest": "reports/source_manifest.jsonl",
            },
            "committed_at": "2026-08-26T00:00:00Z",
        }
        _write(root / "fragment.json", fragment)
        _write(
            root / "COMMITTED.json",
            {
                "schema_version": 1,
                "episode_id": episode_id,
                "fragment_status": "committed",
                "committed_at": "2026-08-26T00:00:00Z",
            },
        )

    @staticmethod
    def _patched_backend():
        def rewrite(
            source,
            target,
            local_episode,
            global_episode,
            frame_offset,
            local_tasks,
            global_tasks,
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            return [global_tasks[local_tasks[0]]] * 2

        return _PatchGroup(rewrite)


class _PatchGroup:
    def __init__(self, rewrite) -> None:
        self._rewrite = patch(
            "arx5_collection.streaming_conversion.builder._rewrite_episode_parquet",
            side_effect=rewrite,
        )
        self._validate = patch(
            "arx5_collection.streaming_conversion.builder.validate_lerobot",
            return_value={"status": "ready", "dataset_root": "temporary"},
        )

    def __enter__(self):
        self._rewrite.__enter__()
        self._validate.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._validate.__exit__(exc_type, exc_value, traceback)
        return self._rewrite.__exit__(exc_type, exc_value, traceback)


def _stats(values: list[int]) -> dict[str, list[int | float]]:
    mean = sum(values) / len(values)
    return {
        "min": [min(values)],
        "max": [max(values)],
        "mean": [mean],
        "std": [0.0],
        "count": [len(values)],
    }


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value) + "\n")


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


if __name__ == "__main__":
    unittest.main()
