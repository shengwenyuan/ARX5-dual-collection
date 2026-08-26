from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

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


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


class RunManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = StreamingConversionConfig(
            1,
            SourceConfig(self.root / "source", (Path("task"),), ()),
            RuntimeConfig(self.root / "streaming", 25),
            OutputConfig(self.root / "lerobot", "fold", "local/fold"),
            RecipeConfig("pi05-equal-eef-v2", "recipe.toml", "folding the cloth"),
        )
        self.discovery = _discovery(self.root / "source")
        self.output = self.root / "lerobot" / "fold_2026-08-25"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_atomically_freezes_selection_and_initial_jobs(self) -> None:
        run = self._create()

        self.assertEqual(run.run_id, "run-001")
        self.assertEqual(set(run.jobs), {"episode-a", "episode-b"})
        self.assertEqual(
            {job.state for job in run.jobs.values()},
            {JobState.DISCOVERED},
        )
        selection = [
            json.loads(line)
            for line in (run.run_dir / "selection_manifest.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [row["episode_id"] for row in selection],
            ["episode-a", "episode-b"],
        )
        self.assertEqual(
            {row["training_task"] for row in selection},
            {"folding the cloth"},
        )
        self.assertFalse(self.output.exists())

    def test_rejects_existing_run_and_output(self) -> None:
        self._create()
        with self.assertRaises(FileExistsError):
            self._create()

        other_output = self.root / "already-exists"
        other_output.mkdir()
        with self.assertRaises(FileExistsError):
            RunManifest.create(
                self.config,
                self.discovery,
                other_output,
                "run-002",
                clock=lambda: NOW,
            )

    def test_replays_valid_transitions_and_resume_skip_states(self) -> None:
        run = self._create()
        for state in (
            JobState.STAGING,
            JobState.CONVERTING,
            JobState.VALIDATING,
            JobState.COMMITTED,
        ):
            run.transition("episode-a", state)
        run.transition(
            "episode-b",
            JobState.EXCLUDED,
            reason_code="selection/no_valid_segment",
        )

        resumed = RunManifest.open(run.run_dir, clock=lambda: NOW)

        self.assertEqual(resumed.jobs["episode-a"].state, JobState.COMMITTED)
        self.assertEqual(resumed.jobs["episode-b"].state, JobState.EXCLUDED)
        self.assertEqual(
            resumed.skipped_episode_ids(),
            ("episode-a", "episode-b"),
        )

    def test_failed_job_requires_explicit_retry_and_increments_attempt(self) -> None:
        run = self._create()
        run.transition("episode-a", JobState.STAGING)
        run.transition(
            "episode-a",
            JobState.FAILED,
            reason_code="infrastructure/pfs_write",
            detail="write failed",
        )
        with self.assertRaisesRegex(ValueError, "terminal"):
            run.transition("episode-a", JobState.STAGING)

        retried = run.retry_failed("episode-a", detail="operator retry")

        self.assertEqual(retried.state, JobState.STAGING)
        self.assertEqual(retried.attempt, 1)
        self.assertEqual(run.retryable_episode_ids(), ())
        resumed = RunManifest.open(run.run_dir)
        self.assertEqual(resumed.jobs["episode-a"].attempt, 1)

    def test_rejects_invalid_transition_and_unstable_reason(self) -> None:
        run = self._create()
        with self.assertRaisesRegex(ValueError, "invalid job transition"):
            run.transition("episode-a", JobState.COMMITTED)
        with self.assertRaisesRegex(ValueError, "stable reason_code"):
            run.transition(
                "episode-a",
                JobState.DISCARDED,
                reason_code="Bad Reason",
            )

    def test_interrupted_job_resumes_as_new_staging_attempt(self) -> None:
        run = self._create()
        run.transition("episode-a", JobState.STAGING)
        run.transition("episode-a", JobState.CONVERTING)

        resumed = run.resume_interrupted("episode-a", detail="coordinator restart")

        self.assertEqual(resumed.state, JobState.STAGING)
        self.assertEqual(resumed.attempt, 1)
        replayed = RunManifest.open(run.run_dir)
        self.assertEqual(replayed.jobs["episode-a"], resumed)
        with self.assertRaisesRegex(ValueError, "not interrupted"):
            run.resume_interrupted("episode-b")

    def test_rejects_tampered_event_history(self) -> None:
        run = self._create()
        path = run.run_dir / "jobs.jsonl"
        value = json.loads(path.read_text().splitlines()[-1])
        value["event_index"] = 99
        with path.open("a") as stream:
            stream.write(json.dumps(value) + "\n")

        with self.assertRaisesRegex(ValueError, "event_index must be contiguous"):
            RunManifest.open(run.run_dir)

    def _create(self) -> RunManifest:
        return RunManifest.create(
            self.config,
            self.discovery,
            self.output,
            "run-001",
            clock=lambda: NOW,
        )


def _discovery(source_root: Path) -> DiscoveryResult:
    source_root.mkdir(parents=True)
    candidates = tuple(
        EpisodeCandidate(
            source_dir=source_root / "task" / episode_id,
            relative_dir=Path("task") / episode_id,
            include_path=Path("task"),
            episode_id=episode_id,
            source_session_id="w4/2026-08-25/task",
            collection_type="demonstration",
            outcome="success",
            task_id="eight-stream-collection",
            task_description="Record synchronized streams",
            mcap=FileIdentity(size, 100 + index),
            metadata=FileIdentity(1000 + index, 200 + index),
        )
        for index, (episode_id, size) in enumerate(
            (("episode-a", 10), ("episode-b", 20))
        )
    )
    return DiscoveryResult(
        source_root,
        (source_root / "task",),
        candidates,
        (),
    )


if __name__ == "__main__":
    unittest.main()
