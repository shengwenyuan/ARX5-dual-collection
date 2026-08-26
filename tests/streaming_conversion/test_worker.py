from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from arx5_collection.cleaning.pipeline import CleaningResult
from arx5_collection.streaming_conversion.models import ConversionStatus
from arx5_collection.streaming_conversion.models import EpisodeCandidate
from arx5_collection.streaming_conversion.models import FileIdentity
from arx5_collection.streaming_conversion.recipe import Pi05ConversionRecipe
from arx5_collection.streaming_conversion.source import MountedEpisodeSource
from arx5_collection.streaming_conversion.worker import convert_episode_fragment


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
RECIPE = Path("config/conversion.pi05-equal-eef-v2.toml")


class ConvertEpisodeFragmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source_root = self.root / "source"
        self.recipe = Pi05ConversionRecipe.load(RECIPE)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_commits_valid_demonstration_fragment_atomically(self) -> None:
        receipt = self._stage("demonstration", "success", "success")
        target = self.root / "fragments" / receipt.episode_id

        with self._successful_pipeline(receipt.episode_id, target):
            result = convert_episode_fragment(
                receipt,
                target,
                self.recipe,
                "folding the cloth",
                "local/fragment-test",
                clock=lambda: NOW,
            )

        self.assertEqual(result.status, ConversionStatus.COMMITTED)
        self.assertEqual(result.segment_count, 2)
        self.assertEqual(result.frame_count, 120)
        self.assertTrue((target / "COMMITTED.json").is_file())
        fragment = json.loads((target / "fragment.json").read_text())
        self.assertEqual(fragment["training_task"], "folding the cloth")
        self.assertEqual(fragment["recipe"]["builder_backend"], "lerobot-v2.1")
        self.assertEqual(fragment["recipe"]["calibration_profile"], "w3")
        report = json.loads((target / "reports" / "conversion.json").read_text())
        self.assertEqual(report["source_selection_dir"], str(target / "selection"))
        self.assertEqual(report["output_root"], str(target / "lerobot"))
        self.assertNotIn(".episode-a.", (target / "fragment.json").read_text())

    def test_excluded_episode_does_not_publish_empty_fragment(self) -> None:
        receipt = self._stage("demonstration", "aborted", "aborted")
        target = self.root / "fragments" / receipt.episode_id
        excluded = SimpleNamespace(
            episodes=(),
            excluded_episodes=(
                {"episode_id": receipt.episode_id, "reason": "outcome_aborted"},
            ),
            output_dir=target.parent / "unused",
        )

        with (
            patch(
                "arx5_collection.streaming_conversion.worker.clean_episode",
                side_effect=self._fake_clean,
            ),
            patch(
                "arx5_collection.streaming_conversion.worker.select_equal_eef_dataset",
                return_value=excluded,
            ),
            patch("arx5_collection.streaming_conversion.worker.export_lerobot") as export,
        ):
            result = convert_episode_fragment(
                receipt,
                target,
                self.recipe,
                "folding the cloth",
                "local/fragment-test",
            )

        self.assertEqual(result.status, ConversionStatus.EXCLUDED)
        self.assertEqual(result.reason_code, "selection/outcome_aborted")
        self.assertFalse(target.exists())
        export.assert_not_called()

    def test_rejects_unstable_selector_exclusion_reason(self) -> None:
        receipt = self._stage("demonstration", "success", "success")
        target = self.root / "fragments" / receipt.episode_id
        excluded = SimpleNamespace(
            episodes=(),
            excluded_episodes=(
                {"episode_id": receipt.episode_id, "reason": "quality_grade_C"},
            ),
            output_dir=target.parent / "unused",
        )

        with (
            patch(
                "arx5_collection.streaming_conversion.worker.clean_episode",
                side_effect=self._fake_clean,
            ),
            patch(
                "arx5_collection.streaming_conversion.worker.select_equal_eef_dataset",
                return_value=excluded,
            ),
            self.assertRaisesRegex(RuntimeError, "stable reason code"),
        ):
            convert_episode_fragment(
                receipt,
                target,
                self.recipe,
                "folding the cloth",
                "local/fragment-test",
            )

        self.assertFalse(target.exists())

    def test_dagger_fail_routes_through_authority_pipeline(self) -> None:
        receipt = self._stage("dagger", "fail", "dagger_fail")
        target = self.root / "fragments" / receipt.episode_id

        with (
            self._successful_pipeline(receipt.episode_id, target, dagger=True),
            patch(
                "arx5_collection.streaming_conversion.worker.classify_dagger_episode"
            ) as classify,
        ):
            result = convert_episode_fragment(
                receipt,
                target,
                self.recipe,
                "folding the cloth",
                "local/fragment-test",
            )

        self.assertEqual(result.status, ConversionStatus.COMMITTED)
        classify.assert_called_once()

    def test_rejects_unmarked_dagger_fail(self) -> None:
        receipt = self._stage("dagger", "fail", "fail")
        target = self.root / "fragments" / receipt.episode_id

        with self.assertRaisesRegex(ValueError, "dagger_fail"):
            convert_episode_fragment(
                receipt,
                target,
                self.recipe,
                "folding the cloth",
                "local/fragment-test",
            )

        self.assertFalse(target.exists())

    def test_rejects_station_without_frozen_calibration(self) -> None:
        receipt = self._stage("demonstration", "success", "success", station_id="w5")
        target = self.root / "fragments" / receipt.episode_id

        with self.assertRaisesRegex(ValueError, "no gripper calibration"):
            convert_episode_fragment(
                receipt,
                target,
                self.recipe,
                "folding the cloth",
                "local/fragment-test",
            )

        self.assertFalse(target.exists())

    def test_pipeline_failure_cleans_partial_fragment(self) -> None:
        receipt = self._stage("demonstration", "success", "success")
        target = self.root / "fragments" / receipt.episode_id

        with (
            patch(
                "arx5_collection.streaming_conversion.worker.clean_episode",
                side_effect=self._fake_clean,
            ),
            patch(
                "arx5_collection.streaming_conversion.worker.select_equal_eef_dataset",
                side_effect=lambda *args, **kwargs: self._selected(
                    args[2],
                    receipt.episode_id,
                    kwargs["source_session_ids"],
                ),
            ),
            patch(
                "arx5_collection.streaming_conversion.worker.export_lerobot",
                side_effect=OSError("injected export failure"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "injected export failure"):
                convert_episode_fragment(
                    receipt,
                    target,
                    self.recipe,
                    "folding the cloth",
                    "local/fragment-test",
                )

        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob(f".{target.name}.*")), [])

    def _stage(
        self,
        collection_type: str,
        outcome: str,
        bucket: str,
        *,
        station_id: str = "w3",
    ):
        source_dir = self.source_root / bucket / "episode-a"
        source_dir.mkdir(parents=True)
        (source_dir / "episode.mcap").write_bytes(b"mcap")
        (source_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "episode_id": "episode-a",
                    "collection_type": collection_type,
                    "outcome": outcome,
                    "station": {"id": station_id},
                    "task": {
                        "id": "eight-stream-collection",
                        "description": "Record synchronized streams",
                    },
                }
            )
        )
        candidate = EpisodeCandidate(
            source_dir=source_dir,
            relative_dir=source_dir.relative_to(self.source_root),
            include_path=Path(bucket),
            episode_id="episode-a",
            source_session_id=f"{station_id}/2026-08-25/task",
            collection_type=collection_type,
            outcome=outcome,
            task_id="eight-stream-collection",
            task_description="Record synchronized streams",
            mcap=_identity(source_dir / "episode.mcap"),
            metadata=_identity(source_dir / "metadata.json"),
        )
        stage = self.root / "staging" / bucket / "episode-a"
        return MountedEpisodeSource(self.source_root).stage(candidate, stage)

    def _successful_pipeline(self, episode_id: str, target: Path, dagger: bool = False):
        selector_name = (
            "select_equal_eef_dagger_dataset"
            if dagger
            else "select_equal_eef_dataset"
        )
        stack = ExitStack()
        stack.enter_context(
            patch(
                "arx5_collection.streaming_conversion.worker.clean_episode",
                side_effect=self._fake_clean,
            )
        )
        stack.enter_context(
            patch(
                f"arx5_collection.streaming_conversion.worker.{selector_name}",
                side_effect=lambda *args, **kwargs: self._selected(
                    args[2], episode_id, kwargs["source_session_ids"]
                ),
            )
        )
        stack.enter_context(
            patch(
                "arx5_collection.streaming_conversion.worker.export_lerobot",
                side_effect=self._fake_export,
            )
        )
        stack.enter_context(
            patch(
                "arx5_collection.streaming_conversion.worker.validate_lerobot",
                return_value={"episodes": 2, "frames": 120},
            )
        )
        return stack

    @staticmethod
    def _fake_clean(episode_dir: Path, audit_root: Path, policy) -> CleaningResult:
        output = audit_root / episode_dir.name
        output.mkdir(parents=True)
        quality = {
            "episode_id": episode_dir.name,
            "outcome": json.loads((episode_dir / "metadata.json").read_text())["outcome"],
            "grade": "A",
            "task": {"id": "legacy", "description": "legacy task"},
        }
        (output / "quality.json").write_text(json.dumps(quality))
        (output / "frame_index.jsonl").touch()
        return CleaningResult(quality, (), output)

    @staticmethod
    def _selected(
        output_root: Path,
        episode_id: str,
        source_session_ids: dict[str, str],
    ):
        if source_session_ids != {episode_id: "w3/2026-08-25/task"}:
            raise AssertionError("Worker did not preserve frozen source Session identity")
        output = output_root / "selection"
        output.mkdir()
        return SimpleNamespace(
            episodes=(object(),),
            excluded_episodes=(),
            output_dir=output,
        )

    @staticmethod
    def _fake_export(
        source_root: Path,
        selection_dir: Path,
        output_root: Path,
        repo_id: str,
        *,
        dataset_root: Path,
    ) -> Path:
        dataset_root.mkdir()
        reports = output_root / "reports"
        reports.mkdir()
        (reports / "source_manifest.jsonl").write_text(
            json.dumps(
                {
                    "segment_id": "episode-a--000",
                    "source_episode_id": "episode-a",
                    "source_session_id": "w3/2026-08-25/task",
                }
            )
            + "\n"
        )
        (reports / "conversion.json").write_text(
            json.dumps(
                {
                    "source_selection_dir": str(selection_dir.resolve()),
                    "output_root": str(dataset_root.resolve()),
                    "source_manifest": str(
                        (reports / "source_manifest.jsonl").resolve()
                    ),
                    "openpi_commit": "openpi",
                    "lerobot_commit": "lerobot",
                    "fps": 50,
                    "mode": "video",
                    "image_size": [640, 360],
                    "image_color": "RGB",
                    "state_action_order": ["joint"],
                    "state_action_version": "state-v1",
                    "filter_version": "filter-v2",
                    "gripper_calibration": {},
                    "sampling_contract": {},
                }
            )
        )
        return dataset_root


def _identity(path: Path) -> FileIdentity:
    stat = path.stat()
    return FileIdentity(stat.st_size, stat.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
