from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from arx5_collection.collection.capture import CaptureProfile
from arx5_collection.dataset_pipeline.source.models import EpisodeScan
from arx5_collection.dataset_pipeline.source.models import CleaningResult
from arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.models import (
    CameraPairingStats,
)
from arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.models import (
    PairingResult,
)
from arx5_collection.dataset_pipeline.execution.models import ConversionStatus
from arx5_collection.dataset_pipeline.execution.models import EpisodeCandidate
from arx5_collection.dataset_pipeline.execution.models import FileIdentity
from arx5_collection.dataset_pipeline.configuration.recipe import DatasetPipelineRecipe
from arx5_collection.dataset_pipeline.source.staging import MountedEpisodeSource
from arx5_collection.dataset_pipeline.source.staging import SourceChangedError
from arx5_collection.dataset_pipeline.execution.worker import convert_episode_fragment


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
RECIPE = "builtin:pi05-equal-eef-v3"


class ConvertEpisodeFragmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source_root = self.root / "source"
        self.recipe = DatasetPipelineRecipe.load(RECIPE)

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
        self.assertEqual(
            {name for name, _ in result.phase_seconds},
            {
                "stage_validate",
                "stage_revalidate",
                "metadata_check",
                "mcap_check",
                "timeline_check",
                "arm_signal_check",
                "frame_alignment",
                "alignment_report",
                "episode_filter",
                "training_interval",
                "equal_eef_action_sampler",
                "motion_segmenter",
                "trajectory_labeler",
                "lerobot_fragment_generator",
                "lerobot_fragment_generator.decode_rgb",
                "lerobot_fragment_generator.video_encode",
                "lerobot_fragment_validator",
                "finalize",
            },
        )
        self.assertTrue(all(seconds >= 0 for _, seconds in result.phase_seconds))
        self.assertTrue((target / "COMMITTED.json").is_file())
        fragment = json.loads((target / "fragment.json").read_text())
        self.assertEqual(fragment["training_task"], "folding the cloth")
        self.assertEqual(fragment["recipe"]["builder_backend"], "lerobot-v2.1")
        self.assertEqual(fragment["recipe"]["gripper_contract"], "arx5-gripper-v1")
        self.assertEqual(fragment["source_station_id"], "w3")
        report = json.loads((target / "reports" / "conversion.json").read_text())
        self.assertEqual(report["source_selection_dir"], str(target / "selection"))
        self.assertEqual(report["output_root"], str(target / "lerobot"))
        self.assertNotIn(".episode-a.", (target / "fragment.json").read_text())

    def test_excluded_episode_does_not_publish_empty_fragment(self) -> None:
        receipt = self._stage("demonstration", "aborted", "aborted")
        target = self.root / "fragments" / receipt.episode_id
        with (
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.mcap_check.read_episode_scan",
                side_effect=self._fake_scan,
            ),
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.frame_alignment.build_frame_groups",
                return_value=self._fake_pairing(),
            ),
            patch.dict(
                "arx5_collection.dataset_pipeline.execution.episode_pipeline.EPISODE_UNIT_RUNNERS",
                self._action_runners(receipt.episode_id, "outcome_aborted"),
            ),
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_generator.export_lerobot"
            ) as export,
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
        with (
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.mcap_check.read_episode_scan",
                side_effect=self._fake_scan,
            ),
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.frame_alignment.build_frame_groups",
                return_value=self._fake_pairing(),
            ),
            patch.dict(
                "arx5_collection.dataset_pipeline.execution.episode_pipeline.EPISODE_UNIT_RUNNERS",
                self._action_runners(receipt.episode_id, "quality_grade_C"),
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
                "arx5_collection.dataset_pipeline.mining_stage.action_mining.dagger_authority.classify_dagger_episode",
                return_value=(SimpleNamespace(valid=True, expert_segments=()), target),
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

    def test_accepts_new_station_with_device_gripper_contract(self) -> None:
        receipt = self._stage("demonstration", "success", "success", station_id="w5")
        target = self.root / "fragments" / receipt.episode_id

        with self._successful_pipeline(receipt.episode_id, target):
            result = convert_episode_fragment(
                receipt,
                target,
                self.recipe,
                "folding the cloth",
                "local/fragment-test",
            )

        self.assertEqual(result.status, ConversionStatus.COMMITTED)
        fragment = json.loads((target / "fragment.json").read_text())
        self.assertEqual(fragment["source_station_id"], "w5")
        self.assertEqual(fragment["recipe"]["gripper_contract"], "arx5-gripper-v1")

    def test_records_explicit_video_policy_in_fragment(self) -> None:
        receipt = self._stage("demonstration", "success", "success")
        target = self.root / "fragments" / receipt.episode_id
        recipe = DatasetPipelineRecipe.load("builtin:pi05-equal-eef-v3-svt-p8")

        with self._successful_pipeline(receipt.episode_id, target):
            convert_episode_fragment(
                receipt,
                target,
                recipe,
                "folding the cloth",
                "local/fragment-test",
            )

        fragment = json.loads((target / "fragment.json").read_text())
        self.assertEqual(fragment["recipe"]["video"]["preset"], 8)

    def test_pipeline_failure_cleans_partial_fragment(self) -> None:
        receipt = self._stage("demonstration", "success", "success")
        target = self.root / "fragments" / receipt.episode_id

        with (
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.mcap_check.read_episode_scan",
                side_effect=self._fake_scan,
            ),
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.frame_alignment.build_frame_groups",
                return_value=self._fake_pairing(),
            ),
            patch.dict(
                "arx5_collection.dataset_pipeline.execution.episode_pipeline.EPISODE_UNIT_RUNNERS",
                self._action_runners(receipt.episode_id),
            ),
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_generator.export_lerobot",
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

    def test_direct_source_change_during_conversion_prevents_commit(self) -> None:
        receipt = self._stage(
            "demonstration", "success", "success", materialization="direct"
        )
        target = self.root / "fragments" / receipt.episode_id

        def export_and_change(*args, **kwargs):
            dataset = self._fake_export(*args, **kwargs)
            (receipt.source_dir / "episode.mcap").write_bytes(b"changed")
            return dataset

        with (
            self._successful_pipeline(receipt.episode_id, target),
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_generator.export_lerobot",
                side_effect=export_and_change,
            ),
            self.assertRaisesRegex(SourceChangedError, "source changed"),
        ):
            convert_episode_fragment(
                receipt,
                target,
                self.recipe,
                "folding the cloth",
                "local/fragment-test",
            )

        self.assertFalse(target.exists())

    def _stage(
        self,
        collection_type: str,
        outcome: str,
        bucket: str,
        *,
        station_id: str = "w3",
        materialization: str = "copy",
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
        self.source_session_id = candidate.source_session_id
        stage = self.root / "staging" / bucket / "episode-a"
        return MountedEpisodeSource(self.source_root, materialization).stage(
            candidate, stage
        )

    def _successful_pipeline(self, episode_id: str, target: Path, dagger: bool = False):
        stack = ExitStack()
        stack.enter_context(
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.mcap_check.read_episode_scan",
                side_effect=self._fake_scan,
            )
        )
        stack.enter_context(
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.episode_sanitycheck.frame_alignment.build_frame_groups",
                return_value=self._fake_pairing(),
            )
        )
        stack.enter_context(
            patch.dict(
                "arx5_collection.dataset_pipeline.execution.episode_pipeline.EPISODE_UNIT_RUNNERS",
                self._action_runners(episode_id),
            )
        )
        stack.enter_context(
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_generator.export_lerobot",
                side_effect=self._fake_export,
            )
        )
        stack.enter_context(
            patch(
                "arx5_collection.dataset_pipeline.mining_stage.dataset_generator.lerobot_fragment_validator.validate_lerobot",
                return_value={"episodes": 2, "frames": 120},
            )
        )
        return stack

    def _action_runners(
        self,
        episode_id: str,
        exclusion_reason: str | None = None,
    ):
        def episode_filter(context, unit, timed):
            def operation():
                if exclusion_reason is None:
                    return
                context.selection = SimpleNamespace(
                    episodes=(),
                    excluded_episodes=(
                        {"episode_id": episode_id, "reason": exclusion_reason},
                    ),
                    output_dir=None,
                )
                context.exclusion_reason = exclusion_reason

            timed(unit.type, operation)

        def training_interval(context, unit, timed):
            context.mining_intervals = timed(unit.type, lambda: ())

        def action_sampler(context, unit, timed):
            context.interval_samples = timed(unit.type, lambda: ())

        def motion_segmenter(context, unit, timed):
            def operation():
                context.mined_samples = ()
                context.mined_segments = ()

            timed(unit.type, operation)

        def trajectory_labeler(context, unit, timed):
            context.selection = timed(
                unit.type,
                lambda: self._selected(
                    context.output_root,
                    episode_id,
                    {episode_id: context.receipt.source_session_id},
                ),
            )

        return {
            "episode_filter": episode_filter,
            "training_interval": training_interval,
            "equal_eef_action_sampler": action_sampler,
            "motion_segmenter": motion_segmenter,
            "trajectory_labeler": trajectory_labeler,
        }

    @staticmethod
    def _fake_scan(episode_dir: Path) -> EpisodeScan:
        from arx5_collection.dataset_pipeline.source.models import required_topics

        return EpisodeScan(
            episode_dir=episode_dir,
            refs_by_topic={topic: () for topic in required_topics(CaptureProfile.RGBD)},
            left_arm=(),
            right_arm=(),
            topic_types={},
        )

    @staticmethod
    def _fake_pairing() -> PairingResult:
        stats = CameraPairingStats(0, 0, 0)
        return PairingResult(
            frame_groups=(),
            camera_stats={"left": stats, "right": stats, "overview": stats},
            common_start_ns=None,
            common_end_ns=None,
            eligible_overview_pairs=1,
            rejected_cross_camera=0,
            rejected_arm_age=0,
        )

    @staticmethod
    def _fake_clean(episode_dir: Path, audit_root: Path, policy) -> CleaningResult:
        output = audit_root / episode_dir.name
        output.mkdir(parents=True)
        quality = {
            "episode_id": episode_dir.name,
            "outcome": json.loads((episode_dir / "metadata.json").read_text())[
                "outcome"
            ],
            "grade": "A",
            "task": {"id": "legacy", "description": "legacy task"},
        }
        (output / "quality.json").write_text(json.dumps(quality))
        (output / "frame_index.jsonl").touch()
        return CleaningResult(quality, (), output)

    def _selected(
        self,
        output_root: Path,
        episode_id: str,
        source_session_ids: dict[str, str],
    ):
        if source_session_ids != {episode_id: self.source_session_id}:
            raise AssertionError(
                "Worker did not preserve frozen source Session identity"
            )
        output = output_root / "selection"
        output.mkdir()
        return SimpleNamespace(
            episodes=(object(),),
            excluded_episodes=(),
            output_dir=output,
        )

    def _fake_export(
        self,
        source_root: Path,
        selection_dir: Path,
        output_root: Path,
        repo_id: str,
        *,
        dataset_root: Path,
        video=None,
        phase_reporter=None,
    ) -> Path:
        if phase_reporter is not None:
            phase_reporter("decode_rgb", 1.0)
            phase_reporter("video_encode", 2.0)
        dataset_root.mkdir()
        reports = output_root / "reports"
        reports.mkdir()
        (reports / "source_manifest.jsonl").write_text(
            json.dumps(
                {
                    "segment_id": "episode-a--000",
                    "source_episode_id": "episode-a",
                    "source_session_id": self.source_session_id,
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
                    "gripper_calibration": {
                        "contract_id": "arx5-gripper-v1",
                        "left": {
                            "open_value": -3.4,
                            "closed_value": 0.0,
                            "open_tolerance": 0.05,
                            "closed_tolerance": 0.10,
                        },
                        "right": {
                            "open_value": -3.4,
                            "closed_value": 0.0,
                            "open_tolerance": 0.05,
                            "closed_tolerance": 0.10,
                        },
                    },
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
