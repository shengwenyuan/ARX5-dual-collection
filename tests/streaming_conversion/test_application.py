from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from arx5_collection.streaming_conversion.alignment import AlignmentCancelled
from arx5_collection.streaming_conversion.application import StreamingRunRequest
from arx5_collection.streaming_conversion.application import execute_streaming_conversion
from arx5_collection.streaming_conversion.builder import SnapshotBuildResult
from arx5_collection.streaming_conversion.config import StreamingConversionConfig
from arx5_collection.streaming_conversion.coordinator import CoordinatorMetric
from arx5_collection.streaming_conversion.coordinator import CoordinatorProgress
from arx5_collection.streaming_conversion.discovery import discover_episodes
from arx5_collection.streaming_conversion.manifest import RunManifest
from arx5_collection.streaming_conversion.models import JobState


NOW = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
RECIPE = Path("config/conversion.pi05-equal-eef-v3.toml")


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


class StreamingApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "tmp" / "source"
        episode = self.source / "task" / "episode-a"
        episode.mkdir(parents=True)
        (episode / "episode.mcap").write_bytes(b"mcap")
        (episode / "metadata.json").write_text(
            json.dumps(
                {
                    "episode_id": "episode-a",
                    "outcome": "success",
                    "task": {"id": "legacy", "description": "record"},
                    "station": {"id": "w4"},
                    "timing": {"started_at": "2026-08-26T04:00:00Z"},
                }
            )
        )
        self.config_path = self.root / "streaming.toml"
        self._write_config(workers=2)
        self.output = self.root / "lerobot" / "snapshot"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_new_run_aligns_then_executes_frozen_pipeline(self) -> None:
        output = _TTY()
        with patch(
            "arx5_collection.streaming_conversion.application.StreamingCoordinator",
            side_effect=_successful_coordinator,
        ):
            result = execute_streaming_conversion(
                StreamingRunRequest(
                    self.config_path,
                    output_override=self.output,
                    run_id="run-new",
                ),
                _TTY("\n"),
                output,
                builder=_fake_builder,
                clock=lambda: NOW,
            )

        self.assertEqual(result.run_id, "run-new")
        self.assertEqual(result.committed, 1)
        self.assertEqual(result.snapshot.repo_id, "local/snapshot")
        self.assertIn("ARX5 streaming conversion alignment", output.getvalue())
        self.assertIn("按 ENTER", output.getvalue())
        run = RunManifest.open(self.root / "streaming" / "run-new")
        self.assertEqual(run.jobs["episode-a"].state, JobState.COMMITTED)
        self.assertEqual(run.definition.output_path, self.output)
        self.assertEqual(run.definition.repo_id, "local/snapshot")
        metrics = [
            json.loads(line)
            for line in (run.run_dir / "metrics.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            {record["kind"] for record in metrics},
            {"progress", "work_completed"},
        )

    def test_cancelled_alignment_has_no_run_side_effect(self) -> None:
        with self.assertRaises(AlignmentCancelled):
            execute_streaming_conversion(
                StreamingRunRequest(self.config_path, run_id="cancelled-run"),
                _TTY("no\n"),
                _TTY(),
                builder=_fake_builder,
                clock=lambda: NOW,
            )

        self.assertFalse((self.root / "streaming" / "cancelled-run").exists())

    def test_resume_skips_discovery_and_explicitly_retries_failed_job(self) -> None:
        manifest = self._create_manifest("resume-run")
        manifest.transition("episode-a", JobState.STAGING)
        manifest.transition(
            "episode-a",
            JobState.FAILED,
            reason_code="infrastructure/staging_io",
        )
        output = _TTY()

        def forbidden_discovery(config):
            raise AssertionError(config)

        with patch(
            "arx5_collection.streaming_conversion.application.StreamingCoordinator",
            side_effect=_successful_coordinator,
        ):
            result = execute_streaming_conversion(
                StreamingRunRequest(
                    self.config_path,
                    resume_run_id="resume-run",
                    retry_failed=True,
                ),
                _TTY(),
                output,
                discover=forbidden_discovery,
                builder=_fake_builder,
                clock=lambda: NOW,
            )

        self.assertEqual(result.committed, 1)
        resumed = RunManifest.open(manifest.run_dir)
        self.assertEqual(resumed.jobs["episode-a"].attempt, 1)
        self.assertNotIn("ARX5 streaming conversion alignment", output.getvalue())

    def test_resume_rejects_changed_frozen_config(self) -> None:
        self._create_manifest("resume-run")
        self._write_config(workers=3)

        with self.assertRaisesRegex(ValueError, "workers"):
            execute_streaming_conversion(
                StreamingRunRequest(
                    self.config_path,
                    resume_run_id="resume-run",
                ),
                _TTY(),
                _TTY(),
                builder=_fake_builder,
            )

    def test_prefetch_resume_rejects_changed_frozen_capacity(self) -> None:
        self._write_prefetch_config(conversion_workers=64)
        self._create_manifest("resume-prefetch")
        self._write_prefetch_config(conversion_workers=80)

        with self.assertRaisesRegex(ValueError, "conversion_workers"):
            execute_streaming_conversion(
                StreamingRunRequest(
                    self.config_path,
                    resume_run_id="resume-prefetch",
                ),
                _TTY(),
                _TTY(),
                builder=_fake_builder,
            )

    def test_resume_rejects_changed_source_materialization(self) -> None:
        self._write_prefetch_config(conversion_workers=64, materialization="direct")
        self._create_manifest("resume-direct")
        self._write_prefetch_config(conversion_workers=64)

        with self.assertRaisesRegex(ValueError, "source_materialization"):
            execute_streaming_conversion(
                StreamingRunRequest(
                    self.config_path,
                    resume_run_id="resume-direct",
                ),
                _TTY(),
                _TTY(),
                builder=_fake_builder,
            )

    def test_direct_source_is_deleted_only_after_successful_snapshot(self) -> None:
        self._write_prefetch_config(conversion_workers=64, materialization="direct")
        output = _TTY()

        with patch(
            "arx5_collection.streaming_conversion.application.StreamingCoordinator",
            side_effect=_successful_coordinator,
        ):
            execute_streaming_conversion(
                StreamingRunRequest(
                    self.config_path,
                    output_override=self.output,
                    run_id="direct-success",
                ),
                _TTY("\n"),
                output,
                builder=_fake_builder,
                clock=lambda: NOW,
            )

        self.assertFalse(self.source.exists())
        self.assertIn('"status": "deleted"', output.getvalue())

    def test_direct_source_is_preserved_when_snapshot_build_fails(self) -> None:
        self._write_prefetch_config(conversion_workers=64, materialization="direct")

        def failed_builder(*args, **kwargs):
            raise RuntimeError("injected snapshot failure")

        with (
            patch(
                "arx5_collection.streaming_conversion.application.StreamingCoordinator",
                side_effect=_successful_coordinator,
            ),
            self.assertRaisesRegex(RuntimeError, "injected snapshot failure"),
        ):
            execute_streaming_conversion(
                StreamingRunRequest(
                    self.config_path,
                    output_override=self.output,
                    run_id="direct-failure",
                ),
                _TTY("\n"),
                _TTY(),
                builder=failed_builder,
                clock=lambda: NOW,
            )

        self.assertTrue(self.source.is_dir())

    def test_request_rejects_ambiguous_run_controls(self) -> None:
        for request, reason in (
            (
                StreamingRunRequest(
                    self.config_path,
                    run_id="new",
                    resume_run_id="old",
                ),
                "mutually exclusive",
            ),
            (
                StreamingRunRequest(self.config_path, retry_failed=True),
                "requires --resume",
            ),
            (
                StreamingRunRequest(
                    self.config_path,
                    output_override=self.output,
                    resume_run_id="old",
                ),
                "cannot change",
            ),
            (
                StreamingRunRequest(self.config_path, resume_run_id="../escape"),
                "one path component",
            ),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    execute_streaming_conversion(request, _TTY(), _TTY())

    def _create_manifest(self, run_id: str) -> RunManifest:
        config = StreamingConversionConfig.load(self.config_path)
        return RunManifest.create(
            config,
            discover_episodes(config.source),
            self.output,
            run_id,
        )

    def _write_config(self, *, workers: int) -> None:
        self.config_path.write_text(
            f'''schema_version = 1

[source]
root = "{self.source}"
include_paths = ["task"]
block = []

[runtime]
streaming_root = "{self.root / 'streaming'}"
workers = {workers}

[output]
lerobot_root = "{self.root / 'lerobot'}"
dataset_name = "fold"
repo_id = "local/fold"

[recipe]
name = "pi05-equal-eef-v3"
profile = "{RECIPE}"
task_source = "metadata.task.description"
'''
        )

    def _write_prefetch_config(
        self, *, conversion_workers: int, materialization: str = "copy"
    ) -> None:
        materialization_line = (
            f'materialization = "{materialization}"\n'
            if materialization != "copy"
            else ""
        )
        self.config_path.write_text(
            f'''schema_version = 2

[source]
root = "{self.source}"
{materialization_line}include_paths = ["task"]
block = []

[runtime]
pfs_root = "{self.root}"
streaming_root = "{self.root / 'streaming'}"
stage_workers = 2
conversion_workers = {conversion_workers}
prefetch_target_bytes = 1000
prefetch_max_bytes = 2000
prefetch_max_episodes = 4

[output]
lerobot_root = "{self.root / 'lerobot'}"
dataset_name = "fold"
repo_id = "local/fold"

[recipe]
name = "pi05-equal-eef-v3"
profile = "{RECIPE}"
task_source = "metadata.task.description"
'''
        )


class _SuccessfulCoordinator:
    def __init__(
        self,
        manifest: RunManifest,
        progress_reporter=None,
        metric_reporter=None,
    ) -> None:
        self._manifest = manifest
        self._progress_reporter = progress_reporter
        self._metric_reporter = metric_reporter

    def run(self):
        for episode_id, job in self._manifest.jobs.items():
            if job.state is JobState.DISCOVERED:
                self._manifest.transition(episode_id, JobState.STAGING)
            self._manifest.transition(episode_id, JobState.CONVERTING)
            self._manifest.transition(episode_id, JobState.VALIDATING)
            self._manifest.transition(episode_id, JobState.COMMITTED)
            if self._metric_reporter is not None:
                self._metric_reporter(
                    CoordinatorMetric(
                        phase="convert",
                        episode_id=episode_id,
                        elapsed_seconds=1.0,
                        source_bytes=4,
                        status="committed",
                        frame_count=10,
                    )
                )
        if self._progress_reporter is not None:
            self._progress_reporter(
                CoordinatorProgress(
                    stage_active=0,
                    stage_ready=0,
                    convert_active=0,
                    convert_ready=0,
                    reserved_staging_bytes=0,
                    reserved_staging_episodes=0,
                    ready_staging_bytes=0,
                    temporary_bytes=0,
                    pfs_free_bytes=None,
                    elapsed_seconds=1.0,
                    staged_bytes=4,
                    converted_frames=10,
                    stage_gb_s=0.000000004,
                    conversion_frames_s=10.0,
                    states={"committed": 1},
                )
            )
        return self._manifest.jobs


def _successful_coordinator(manifest, *args, **kwargs):
    return _SuccessfulCoordinator(
        manifest,
        progress_reporter=kwargs.get("progress_reporter"),
        metric_reporter=kwargs.get("metric_reporter"),
    )


def _fake_builder(manifest, output_path, recipe, repo_id):
    return SnapshotBuildResult(
        output_path=output_path,
        repo_id=repo_id,
        source_episode_count=1,
        fragment_count=1,
        episode_count=1,
        frame_count=10,
        tasks=("folding the cloth",),
    )


if __name__ == "__main__":
    unittest.main()
