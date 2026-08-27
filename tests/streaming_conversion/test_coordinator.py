from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import patch

from arx5_collection.streaming_conversion.config import BufferedRuntimeConfig
from arx5_collection.streaming_conversion.config import OutputConfig
from arx5_collection.streaming_conversion.config import PrefetchRuntimeConfig
from arx5_collection.streaming_conversion.config import RecipeConfig
from arx5_collection.streaming_conversion.config import RuntimeConfig
from arx5_collection.streaming_conversion.config import SourceConfig
from arx5_collection.streaming_conversion.config import StreamingConversionConfig
from arx5_collection.streaming_conversion.coordinator import ConversionWork
from arx5_collection.streaming_conversion.coordinator import StageWork
from arx5_collection.streaming_conversion.coordinator import StreamingCoordinator
from arx5_collection.streaming_conversion.coordinator import StagingCapacityError
from arx5_collection.streaming_conversion.coordinator import spawn_executor
from arx5_collection.streaming_conversion.manifest import RunManifest
from arx5_collection.streaming_conversion.models import ConversionStatus
from arx5_collection.streaming_conversion.models import DiscoveryResult
from arx5_collection.streaming_conversion.models import EpisodeCandidate
from arx5_collection.streaming_conversion.models import EpisodeConversionResult
from arx5_collection.streaming_conversion.models import FileIdentity
from arx5_collection.streaming_conversion.models import JobState
from arx5_collection.streaming_conversion.models import StageReceipt
from arx5_collection.streaming_conversion.recipe import Pi05ConversionRecipe
from arx5_collection.streaming_conversion.source import SourceChangedError


RECIPE = Path("config/conversion.pi05-equal-eef-v3.toml")


class StreamingCoordinatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        self.recipe = Pi05ConversionRecipe.load(RECIPE)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_closes_jobs_with_bounded_work_and_releases_success_staging(self) -> None:
        manifest = self._manifest("episode-a", "episode-b", "episode-c")
        barrier = Barrier(2)
        lock = Lock()
        active = 0
        maximum = 0
        started: list[str] = []

        def stage(work: StageWork) -> StageReceipt:
            nonlocal active, maximum
            with lock:
                started.append(f"stage:{work.selection.episode_id}")
                active += 1
                maximum = max(maximum, active)
            if work.selection.episode_id in {"episode-a", "episode-b"}:
                barrier.wait(timeout=2)
            try:
                if work.selection.episode_id == "episode-c":
                    raise SourceChangedError("frozen source changed")
                work.target.mkdir(parents=True)
                return _receipt(work)
            finally:
                with lock:
                    active -= 1

        def convert(work: ConversionWork) -> EpisodeConversionResult:
            nonlocal active, maximum
            with lock:
                started.append(f"convert:{work.receipt.episode_id}")
                active += 1
                maximum = max(maximum, active)
            try:
                if work.receipt.episode_id == "episode-b":
                    return _excluded(work.receipt.episode_id)
                return _committed(work.receipt.episode_id, work.target)
            finally:
                with lock:
                    active -= 1

        jobs = self._coordinator(manifest, stage, convert, workers=2).run()

        self.assertEqual(jobs["episode-a"].state, JobState.COMMITTED)
        self.assertEqual(jobs["episode-b"].state, JobState.EXCLUDED)
        self.assertEqual(jobs["episode-c"].state, JobState.DISCARDED)
        self.assertEqual(
            jobs["episode-c"].reason_code,
            "discarded/source_changed_after_confirmation",
        )
        self.assertEqual(maximum, 2)
        self.assertLess(
            min(index for index, item in enumerate(started) if item.startswith("convert:")),
            started.index("stage:episode-c"),
        )
        self.assertFalse((manifest.run_dir / "staging" / "episode-a").exists())
        self.assertFalse((manifest.run_dir / "staging" / "episode-b").exists())

    def test_resume_restarts_interrupted_attempt_and_cleans_hidden_partials(self) -> None:
        manifest = self._manifest("episode-a")
        manifest.transition("episode-a", JobState.STAGING)
        manifest.transition("episode-a", JobState.CONVERTING)
        for parent in ("staging", "fragments"):
            partial = manifest.run_dir / parent / ".episode-a.interrupted"
            partial.mkdir(parents=True)

        jobs = self._coordinator(
            manifest,
            _fake_stage,
            lambda work: _committed(work.receipt.episode_id, work.target),
            workers=1,
        ).run()

        self.assertEqual(jobs["episode-a"].state, JobState.COMMITTED)
        self.assertEqual(jobs["episode-a"].attempt, 1)
        self.assertEqual(list((manifest.run_dir / "staging").glob(".*")), [])
        self.assertEqual(list((manifest.run_dir / "fragments").glob(".*")), [])

    def test_terminal_jobs_skip_workers_and_failed_job_requires_retry(self) -> None:
        manifest = self._manifest("episode-a", "episode-b")
        for state in (
            JobState.STAGING,
            JobState.CONVERTING,
            JobState.VALIDATING,
            JobState.COMMITTED,
        ):
            manifest.transition("episode-a", state)
        stale_stage = manifest.run_dir / "staging" / "episode-a"
        stale_stage.mkdir(parents=True)
        manifest.transition("episode-b", JobState.STAGING)
        manifest.transition(
            "episode-b",
            JobState.FAILED,
            reason_code="infrastructure/staging_io",
        )
        calls = 0

        def forbidden_stage(work: StageWork) -> StageReceipt:
            nonlocal calls
            calls += 1
            raise AssertionError(work)

        jobs = self._coordinator(
            manifest,
            forbidden_stage,
            lambda work: _committed(work.receipt.episode_id, work.target),
            workers=1,
        ).run()

        self.assertEqual(calls, 0)
        self.assertEqual(jobs["episode-a"].state, JobState.COMMITTED)
        self.assertEqual(jobs["episode-b"].state, JobState.FAILED)
        self.assertFalse(stale_stage.exists())

    def test_default_executor_uses_spawn_context(self) -> None:
        with patch(
            "arx5_collection.streaming_conversion.coordinator.ProcessPoolExecutor"
        ) as factory:
            executor = spawn_executor(1)

        self.assertIs(executor, factory.return_value)
        self.assertEqual(factory.call_args.kwargs["max_workers"], 1)
        self.assertEqual(
            factory.call_args.kwargs["mp_context"].get_start_method(),
            "spawn",
        )

    def test_bounded_prefetch_overlaps_independent_stage_and_conversion_pools(self) -> None:
        manifest = self._manifest(
            "episode-a", "episode-b", "episode-c", "episode-d"
        )
        first_stages = Barrier(2)
        third_stage_started = Event()
        lock = Lock()
        stage_active = 0
        stage_maximum = 0
        convert_active = 0
        convert_maximum = 0

        def stage(work: StageWork) -> StageReceipt:
            nonlocal stage_active, stage_maximum
            episode_id = work.selection.episode_id
            with lock:
                stage_active += 1
                stage_maximum = max(stage_maximum, stage_active)
            try:
                if episode_id in {"episode-a", "episode-b"}:
                    first_stages.wait(timeout=2)
                else:
                    third_stage_started.set()
                work.target.mkdir(parents=True)
                return _receipt(work)
            finally:
                with lock:
                    stage_active -= 1

        def convert(work: ConversionWork) -> EpisodeConversionResult:
            nonlocal convert_active, convert_maximum
            with lock:
                convert_active += 1
                convert_maximum = max(convert_maximum, convert_active)
            try:
                self.assertTrue(third_stage_started.wait(timeout=2))
                return _committed(work.receipt.episode_id, work.target)
            finally:
                with lock:
                    convert_active -= 1

        jobs = self._prefetch_coordinator(
            manifest,
            stage,
            convert,
            stage_workers=2,
            conversion_workers=2,
            target_bytes=90,
            max_bytes=120,
            max_episodes=4,
        ).run()

        self.assertEqual(
            {job.state for job in jobs.values()},
            {JobState.COMMITTED},
        )
        self.assertEqual(stage_maximum, 2)
        self.assertGreaterEqual(convert_maximum, 1)

    def test_prefetch_hard_episode_bound_blocks_new_stage_until_release(self) -> None:
        manifest = self._manifest("episode-a", "episode-b", "episode-c")
        first_stages = Barrier(2)
        lock = Lock()
        events: list[str] = []

        def stage(work: StageWork) -> StageReceipt:
            episode_id = work.selection.episode_id
            if episode_id in {"episode-a", "episode-b"}:
                first_stages.wait(timeout=2)
            with lock:
                events.append(f"stage:{episode_id}")
            work.target.mkdir(parents=True)
            return _receipt(work)

        def convert(work: ConversionWork) -> EpisodeConversionResult:
            episode_id = work.receipt.episode_id
            with lock:
                events.append(f"convert:{episode_id}")
            return _committed(episode_id, work.target)

        self._prefetch_coordinator(
            manifest,
            stage,
            convert,
            stage_workers=2,
            conversion_workers=1,
            target_bytes=60,
            max_bytes=60,
            max_episodes=2,
        ).run()

        first_conversion = min(
            index for index, item in enumerate(events) if item.startswith("convert:")
        )
        self.assertLess(first_conversion, events.index("stage:episode-c"))

    def test_retained_failed_stage_counts_against_hard_capacity(self) -> None:
        manifest = self._manifest("episode-a", "episode-b")
        retained = manifest.run_dir / "staging" / "episode-a"
        retained.mkdir(parents=True)
        manifest.transition("episode-a", JobState.STAGING)
        manifest.transition(
            "episode-a",
            JobState.FAILED,
            reason_code="infrastructure/staging_io",
        )

        coordinator = self._prefetch_coordinator(
            manifest,
            _fake_stage,
            lambda work: _committed(work.receipt.episode_id, work.target),
            stage_workers=1,
            conversion_workers=1,
            target_bytes=40,
            max_bytes=40,
            max_episodes=2,
        )

        with self.assertRaisesRegex(
            StagingCapacityError,
            "retained_terminal=\\['episode-a'\\]",
        ):
            coordinator.run()

        self.assertTrue(retained.exists())
        self.assertEqual(manifest.jobs["episode-b"].state, JobState.DISCOVERED)

    def test_reports_prefetch_queue_and_final_release(self) -> None:
        manifest = self._manifest("episode-a")
        progress = []

        self._prefetch_coordinator(
            manifest,
            _fake_stage,
            lambda work: _committed(work.receipt.episode_id, work.target),
            stage_workers=1,
            conversion_workers=1,
            target_bytes=60,
            max_bytes=60,
            max_episodes=1,
            progress_reporter=progress.append,
        ).run()

        self.assertGreaterEqual(len(progress), 2)
        self.assertEqual(progress[0].reserved_staging_bytes, 30)
        self.assertEqual(progress[-1].reserved_staging_bytes, 0)
        self.assertEqual(progress[-1].states, {"committed": 1})

    def test_prefetch_resume_reuses_valid_complete_stage_without_copy(self) -> None:
        manifest = self._manifest("episode-a")
        selection = manifest.selection[0]
        target = manifest.run_dir / "staging" / "episode-a"
        target.mkdir(parents=True)
        receipt = StageReceipt(
            episode_id="episode-a",
            source_session_id=selection.source_session_id,
            source_dir=selection.source_dir,
            stage_dir=target,
            mcap=selection.mcap,
            metadata=selection.metadata,
        )
        manifest.transition("episode-a", JobState.STAGING)

        def forbidden_stage(work: StageWork) -> StageReceipt:
            raise AssertionError(work)

        with patch(
            "arx5_collection.streaming_conversion.coordinator.validate_stage",
            return_value=receipt,
        ):
            jobs = self._prefetch_coordinator(
                manifest,
                forbidden_stage,
                lambda work: _committed(work.receipt.episode_id, work.target),
                stage_workers=1,
                conversion_workers=1,
                target_bytes=60,
                max_bytes=60,
                max_episodes=1,
            ).run()

        self.assertEqual(jobs["episode-a"].state, JobState.COMMITTED)
        self.assertEqual(jobs["episode-a"].attempt, 1)
        self.assertFalse(target.exists())

    def test_buffered_prefetch_retained_failure_does_not_act_as_soft_watermark(self) -> None:
        manifest = self._manifest("episode-a", "episode-b")
        retained = manifest.run_dir / "staging" / "episode-a"
        retained.mkdir(parents=True)
        manifest.transition("episode-a", JobState.STAGING)
        manifest.transition(
            "episode-a",
            JobState.FAILED,
            reason_code="infrastructure/staging_io",
        )

        jobs = self._buffered_coordinator(
            manifest,
            _fake_stage,
            lambda work: _committed(work.receipt.episode_id, work.target),
            low_bytes=20,
            high_bytes=40,
            hard_max_bytes=90,
        ).run()

        self.assertEqual(jobs["episode-a"].state, JobState.FAILED)
        self.assertEqual(jobs["episode-b"].state, JobState.COMMITTED)
        self.assertTrue(retained.exists())

    def test_buffered_prefetch_stops_before_minimum_pfs_free_space(self) -> None:
        manifest = self._manifest("episode-a")
        coordinator = self._buffered_coordinator(
            manifest,
            _fake_stage,
            lambda work: _committed(work.receipt.episode_id, work.target),
            low_bytes=20,
            high_bytes=40,
            hard_max_bytes=90,
            min_free_bytes=20,
            disk_free_reader=lambda path: 40,
        )

        with self.assertRaises(StagingCapacityError):
            coordinator.run()

        self.assertEqual(manifest.jobs["episode-a"].state, JobState.DISCOVERED)

    def test_buffered_resume_quarantines_invalid_stage_then_restages(self) -> None:
        manifest = self._manifest("episode-a")
        invalid = manifest.run_dir / "staging" / "episode-a"
        invalid.mkdir(parents=True)
        (invalid / "stage.json").write_text("not-json")
        manifest.transition("episode-a", JobState.STAGING)

        jobs = self._buffered_coordinator(
            manifest,
            _fake_stage,
            lambda work: _committed(work.receipt.episode_id, work.target),
            low_bytes=20,
            high_bytes=40,
            hard_max_bytes=90,
        ).run()

        self.assertEqual(jobs["episode-a"].state, JobState.COMMITTED)
        quarantined = list(
            (manifest.run_dir / "quarantine" / "staging").iterdir()
        )
        self.assertEqual(len(quarantined), 1)
        self.assertEqual((quarantined[0] / "stage.json").read_text(), "not-json")

    def _manifest(self, *episode_ids: str) -> RunManifest:
        config = StreamingConversionConfig(
            1,
            SourceConfig(self.source_root, (Path("task"),), ()),
            RuntimeConfig(self.root / "streaming", 2),
            OutputConfig(self.root / "lerobot", "fold", "local/fold"),
            RecipeConfig("pi05-equal-eef-v3", str(RECIPE), "folding the cloth"),
        )
        candidates = tuple(
            EpisodeCandidate(
                source_dir=self.source_root / "task" / episode_id,
                relative_dir=Path("task") / episode_id,
                include_path=Path("task"),
                episode_id=episode_id,
                source_session_id="w4/2026-08-25/task",
                collection_type="demonstration",
                outcome="success",
                task_id="task",
                task_description="folding the cloth",
                mcap=FileIdentity(10, index + 1),
                metadata=FileIdentity(20, index + 11),
            )
            for index, episode_id in enumerate(episode_ids)
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
            self.root / "lerobot" / f"output-{episode_ids[0]}",
            f"run-{episode_ids[0]}",
        )

    def _coordinator(
        self,
        manifest: RunManifest,
        stage_runner,
        conversion_runner,
        *,
        workers: int,
    ) -> StreamingCoordinator:
        return StreamingCoordinator(
            manifest,
            self.source_root,
            self.recipe,
            "folding the cloth",
            "local/fold",
            workers,
            stage_runner=stage_runner,
            conversion_runner=conversion_runner,
            executor_factory=lambda count: ThreadPoolExecutor(max_workers=count),
        )

    def _prefetch_coordinator(
        self,
        manifest: RunManifest,
        stage_runner,
        conversion_runner,
        *,
        stage_workers: int,
        conversion_workers: int,
        target_bytes: int,
        max_bytes: int,
        max_episodes: int,
        progress_reporter=None,
    ) -> StreamingCoordinator:
        runtime = PrefetchRuntimeConfig(
            pfs_root=self.root,
            streaming_root=self.root / "streaming",
            stage_workers=stage_workers,
            conversion_workers=conversion_workers,
            prefetch_target_bytes=target_bytes,
            prefetch_max_bytes=max_bytes,
            prefetch_max_episodes=max_episodes,
        )
        executor_factory = lambda count: ThreadPoolExecutor(max_workers=count)
        return StreamingCoordinator(
            manifest,
            self.source_root,
            self.recipe,
            "folding the cloth",
            "local/fold",
            runtime,
            stage_runner=stage_runner,
            conversion_runner=conversion_runner,
            stage_executor_factory=executor_factory,
            conversion_executor_factory=executor_factory,
            progress_reporter=progress_reporter,
        )

    def _buffered_coordinator(
        self,
        manifest: RunManifest,
        stage_runner,
        conversion_runner,
        *,
        low_bytes: int,
        high_bytes: int,
        hard_max_bytes: int,
        min_free_bytes: int = 0,
        disk_free_reader=None,
    ) -> StreamingCoordinator:
        runtime = BufferedRuntimeConfig(
            pfs_root=self.root,
            streaming_root=self.root / "streaming",
            stage_workers=2,
            conversion_workers=2,
            ready_low_bytes=low_bytes,
            ready_high_bytes=high_bytes,
            temporary_hard_max_bytes=hard_max_bytes,
            max_staged_episodes=4,
            min_free_bytes=min_free_bytes,
        )
        executor_factory = lambda count: ThreadPoolExecutor(max_workers=count)
        return StreamingCoordinator(
            manifest,
            self.source_root,
            self.recipe,
            "folding the cloth",
            "local/fold",
            runtime,
            stage_runner=stage_runner,
            conversion_runner=conversion_runner,
            stage_executor_factory=executor_factory,
            conversion_executor_factory=executor_factory,
            disk_free_reader=disk_free_reader,
        )


def _fake_stage(work: StageWork) -> StageReceipt:
    work.target.mkdir(parents=True, exist_ok=True)
    return _receipt(work)


def _receipt(work: StageWork) -> StageReceipt:
    return StageReceipt(
        episode_id=work.selection.episode_id,
        source_session_id=work.selection.source_session_id,
        source_dir=work.selection.source_dir,
        stage_dir=work.target,
        mcap=work.selection.mcap,
        metadata=work.selection.metadata,
    )


def _committed(episode_id: str, target: Path) -> EpisodeConversionResult:
    return EpisodeConversionResult(
        episode_id,
        ConversionStatus.COMMITTED,
        target,
        segment_count=1,
        frame_count=10,
    )


def _excluded(episode_id: str) -> EpisodeConversionResult:
    return EpisodeConversionResult(
        episode_id,
        ConversionStatus.EXCLUDED,
        None,
        segment_count=0,
        frame_count=0,
        reason_code="selection/no_valid_motion_segment",
    )


if __name__ == "__main__":
    unittest.main()
