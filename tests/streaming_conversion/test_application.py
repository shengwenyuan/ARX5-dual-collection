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
from arx5_collection.streaming_conversion.discovery import discover_episodes
from arx5_collection.streaming_conversion.manifest import RunManifest
from arx5_collection.streaming_conversion.models import JobState


NOW = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
RECIPE = Path("config/conversion.pi05-equal-eef-v2.toml").resolve()


class _TTY(StringIO):
    def isatty(self) -> bool:
        return True


class StreamingApplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
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
name = "pi05-equal-eef-v2"
profile = "{RECIPE}"
task = "folding the cloth"
'''
        )


class _SuccessfulCoordinator:
    def __init__(self, manifest: RunManifest) -> None:
        self._manifest = manifest

    def run(self):
        for episode_id, job in self._manifest.jobs.items():
            if job.state is JobState.DISCOVERED:
                self._manifest.transition(episode_id, JobState.STAGING)
            self._manifest.transition(episode_id, JobState.CONVERTING)
            self._manifest.transition(episode_id, JobState.VALIDATING)
            self._manifest.transition(episode_id, JobState.COMMITTED)
        return self._manifest.jobs


def _successful_coordinator(manifest, *args, **kwargs):
    return _SuccessfulCoordinator(manifest)


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
