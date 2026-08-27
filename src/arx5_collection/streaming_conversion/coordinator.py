from __future__ import annotations

from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor
from concurrent.futures import FIRST_COMPLETED
from concurrent.futures import Future
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
from dataclasses import dataclass
from enum import Enum
import multiprocessing
from pathlib import Path
import shutil
import time

from .config import PrefetchRuntimeConfig
from .config import RuntimeConfig
from .config import RuntimeSettings
from .manifest import RunManifest
from .models import ConversionStatus
from .models import EpisodeCandidate
from .models import EpisodeConversionResult
from .models import JobSnapshot
from .models import JobState
from .models import SelectionEntry
from .models import StageReceipt
from .recipe import Pi05ConversionRecipe
from .source import MountedEpisodeSource
from .source import SourceChangedError
from .source import StageValidationError
from .source import validate_stage
from .worker import convert_episode_fragment
from .worker import load_committed_fragment


@dataclass(frozen=True, slots=True)
class StageWork:
    source_root: Path
    selection: SelectionEntry
    target: Path


@dataclass(frozen=True, slots=True)
class ConversionWork:
    receipt: StageReceipt
    target: Path
    recipe: Pi05ConversionRecipe
    task: str
    repo_id: str


class WorkPhase(str, Enum):
    STAGE = "stage"
    CONVERT = "convert"


@dataclass(frozen=True, slots=True)
class ActiveWork:
    episode_id: str
    phase: WorkPhase


@dataclass(frozen=True, slots=True)
class CoordinatorProgress:
    stage_active: int
    stage_ready: int
    convert_active: int
    convert_ready: int
    reserved_staging_bytes: int
    reserved_staging_episodes: int
    states: dict[str, int]


StageRunner = Callable[[StageWork], StageReceipt]
ConversionRunner = Callable[[ConversionWork], EpisodeConversionResult]
ExecutorFactory = Callable[[int], Executor]
ProgressReporter = Callable[[CoordinatorProgress], None]


class StagingCapacityError(RuntimeError):
    pass


class StreamingCoordinator:
    def __init__(
        self,
        manifest: RunManifest,
        source_root: Path,
        recipe: Pi05ConversionRecipe,
        task: str,
        repo_id: str,
        runtime: RuntimeSettings | int,
        *,
        stage_runner: StageRunner | None = None,
        conversion_runner: ConversionRunner | None = None,
        executor_factory: ExecutorFactory | None = None,
        stage_executor_factory: ExecutorFactory | None = None,
        conversion_executor_factory: ExecutorFactory | None = None,
        progress_reporter: ProgressReporter | None = None,
        progress_interval_seconds: float = 10.0,
    ) -> None:
        if not task.strip():
            raise ValueError("Coordinator task must not be empty")
        self._manifest = manifest
        self._source_root = source_root
        self._recipe = recipe
        self._task = task
        self._repo_id = repo_id
        if isinstance(runtime, bool) or not isinstance(
            runtime, (int, RuntimeConfig, PrefetchRuntimeConfig)
        ):
            raise TypeError("Coordinator runtime is invalid")
        if isinstance(runtime, int):
            if runtime < 1:
                raise ValueError("Coordinator workers must be positive")
            runtime = RuntimeConfig(manifest.definition.streaming_root, runtime)
        self._runtime = runtime
        self._stage_runner = stage_runner or stage_episode
        self._conversion_runner = conversion_runner or convert_staged_episode
        self._legacy_executor_factory = executor_factory or spawn_executor
        self._stage_executor_factory = stage_executor_factory or stage_thread_executor
        self._conversion_executor_factory = (
            conversion_executor_factory or spawn_executor
        )
        if progress_interval_seconds <= 0:
            raise ValueError("progress interval must be positive")
        self._progress_reporter = progress_reporter
        self._progress_interval_seconds = progress_interval_seconds
        self._selection = {item.episode_id: item for item in manifest.selection}

    def run(self) -> dict[str, JobSnapshot]:
        self._recover_interrupted()
        if isinstance(self._runtime, RuntimeConfig):
            return self._run_legacy(self._runtime.workers)
        return self._run_bounded_prefetch(self._runtime)

    def _run_legacy(self, workers: int) -> dict[str, JobSnapshot]:
        ready_stage = deque(
            sorted(
                episode_id
                for episode_id, job in self._manifest.jobs.items()
                if job.state in {JobState.DISCOVERED, JobState.STAGING}
            )
        )
        ready_convert: deque[StageReceipt] = deque()
        active: dict[Future[object], ActiveWork] = {}

        with self._legacy_executor_factory(workers) as executor:
            self._fill_legacy(
                executor,
                workers,
                active,
                ready_stage,
                ready_convert,
            )
            while active:
                completed, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in completed:
                    work = active.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:
                        self._record_error(work, error)
                        continue
                    if work.phase is WorkPhase.STAGE:
                        if not isinstance(result, StageReceipt):
                            self._record_error(
                                work,
                                TypeError("stage runner returned an invalid result"),
                            )
                            continue
                        if result.episode_id != work.episode_id:
                            self._record_error(
                                work,
                                ValueError("stage runner returned another Episode"),
                            )
                            continue
                        self._manifest.transition(work.episode_id, JobState.CONVERTING)
                        ready_convert.append(result)
                    else:
                        try:
                            self._record_conversion(work, result)
                        except Exception as error:
                            self._record_error(work, error)
                self._fill_legacy(
                    executor,
                    workers,
                    active,
                    ready_stage,
                    ready_convert,
                )
        return self._manifest.jobs

    def _fill_legacy(
        self,
        executor: Executor,
        workers: int,
        active: dict[Future[object], ActiveWork],
        ready_stage: deque[str],
        ready_convert: deque[StageReceipt],
    ) -> None:
        while len(active) < workers and (ready_convert or ready_stage):
            if ready_convert:
                receipt = ready_convert.popleft()
                work = ConversionWork(
                    receipt=receipt,
                    target=self._manifest.run_dir / "fragments" / receipt.episode_id,
                    recipe=self._recipe,
                    task=self._task,
                    repo_id=_fragment_repo_id(self._repo_id, receipt.episode_id),
                )
                future = executor.submit(self._conversion_runner, work)
                active[future] = ActiveWork(receipt.episode_id, WorkPhase.CONVERT)
                continue

            episode_id = ready_stage.popleft()
            job = self._manifest.jobs[episode_id]
            if job.state is JobState.DISCOVERED:
                self._manifest.transition(episode_id, JobState.STAGING)
            work = StageWork(
                source_root=self._source_root,
                selection=self._selection[episode_id],
                target=self._manifest.run_dir / "staging" / episode_id,
            )
            future = executor.submit(self._stage_runner, work)
            active[future] = ActiveWork(episode_id, WorkPhase.STAGE)

    def _run_bounded_prefetch(
        self,
        runtime: PrefetchRuntimeConfig,
    ) -> dict[str, JobSnapshot]:
        ready_stage, ready_convert, reserved = self._recover_prefetch_queues()
        active: dict[Future[object], ActiveWork] = {}
        next_progress = time.monotonic()

        with self._stage_executor_factory(runtime.stage_workers) as stage_executor:
            with self._conversion_executor_factory(
                runtime.conversion_workers
            ) as conversion_executor:
                while ready_stage or ready_convert or active:
                    self._fill_conversions(
                        conversion_executor,
                        runtime.conversion_workers,
                        active,
                        ready_convert,
                    )
                    self._fill_staging(
                        stage_executor,
                        runtime,
                        active,
                        ready_stage,
                        reserved,
                    )
                    now = time.monotonic()
                    if now >= next_progress:
                        self._report_progress(
                            active,
                            ready_stage,
                            ready_convert,
                            reserved,
                        )
                        next_progress = now + self._progress_interval_seconds
                    if not active:
                        if ready_stage:
                            raise StagingCapacityError(
                                self._capacity_error(runtime, ready_stage, reserved)
                            )
                        if ready_convert:
                            raise RuntimeError("conversion queue could not be scheduled")
                        break

                    timeout = max(0.0, next_progress - time.monotonic())
                    completed, _ = wait(
                        tuple(active),
                        timeout=timeout,
                        return_when=FIRST_COMPLETED,
                    )
                    if not completed:
                        continue
                    for future in completed:
                        work = active.pop(future)
                        try:
                            result = future.result()
                        except Exception as error:
                            self._record_error(work, error)
                            self._release_missing_stage(work.episode_id, reserved)
                            continue
                        if work.phase is WorkPhase.STAGE:
                            if not isinstance(result, StageReceipt):
                                self._record_error(
                                    work,
                                    TypeError("stage runner returned an invalid result"),
                                )
                                self._release_missing_stage(work.episode_id, reserved)
                                continue
                            if result.episode_id != work.episode_id:
                                self._record_error(
                                    work,
                                    ValueError("stage runner returned another Episode"),
                                )
                                self._release_missing_stage(work.episode_id, reserved)
                                continue
                            self._manifest.transition(
                                work.episode_id, JobState.CONVERTING
                            )
                            ready_convert.append(result)
                            continue
                        try:
                            self._record_conversion(work, result)
                        except Exception as error:
                            self._record_error(work, error)
                        self._release_missing_stage(work.episode_id, reserved)
                self._report_progress(
                    active,
                    ready_stage,
                    ready_convert,
                    reserved,
                )
        return self._manifest.jobs

    def _recover_prefetch_queues(
        self,
    ) -> tuple[deque[str], deque[StageReceipt], set[str]]:
        ready_stage: deque[str] = deque()
        ready_convert: deque[StageReceipt] = deque()
        reserved = {
            selection.episode_id
            for selection in self._manifest.selection
            if (self._manifest.run_dir / "staging" / selection.episode_id).exists()
        }
        for selection in self._manifest.selection:
            episode_id = selection.episode_id
            job = self._manifest.jobs[episode_id]
            if job.state not in {JobState.DISCOVERED, JobState.STAGING}:
                continue
            target = self._manifest.run_dir / "staging" / episode_id
            if not target.exists():
                ready_stage.append(episode_id)
                continue
            try:
                receipt = validate_stage(target)
                _validate_reused_stage(
                    receipt,
                    StageWork(self._source_root, selection, target),
                )
            except (OSError, ValueError):
                ready_stage.append(episode_id)
                continue
            if job.state is JobState.DISCOVERED:
                self._manifest.transition(episode_id, JobState.STAGING)
            self._manifest.transition(episode_id, JobState.CONVERTING)
            ready_convert.append(receipt)
        return ready_stage, ready_convert, reserved

    def _fill_conversions(
        self,
        executor: Executor,
        workers: int,
        active: dict[Future[object], ActiveWork],
        ready_convert: deque[StageReceipt],
    ) -> None:
        active_count = sum(
            work.phase is WorkPhase.CONVERT for work in active.values()
        )
        while active_count < workers and ready_convert:
            receipt = ready_convert.popleft()
            work = ConversionWork(
                receipt=receipt,
                target=self._manifest.run_dir / "fragments" / receipt.episode_id,
                recipe=self._recipe,
                task=self._task,
                repo_id=_fragment_repo_id(self._repo_id, receipt.episode_id),
            )
            future = executor.submit(self._conversion_runner, work)
            active[future] = ActiveWork(receipt.episode_id, WorkPhase.CONVERT)
            active_count += 1

    def _fill_staging(
        self,
        executor: Executor,
        runtime: PrefetchRuntimeConfig,
        active: dict[Future[object], ActiveWork],
        ready_stage: deque[str],
        reserved: set[str],
    ) -> None:
        active_count = sum(work.phase is WorkPhase.STAGE for work in active.values())
        while active_count < runtime.stage_workers and ready_stage:
            episode_id = ready_stage[0]
            episode_bytes = self._episode_bytes(episode_id)
            if episode_bytes > runtime.prefetch_max_bytes:
                raise StagingCapacityError(
                    f"Episode {episode_id} requires {episode_bytes} staging bytes, "
                    f"above hard limit {runtime.prefetch_max_bytes}"
                )
            reserved_bytes = self._reserved_bytes(reserved)
            if (
                reserved_bytes >= runtime.prefetch_target_bytes
                or len(reserved) >= runtime.prefetch_max_episodes
                or reserved_bytes + episode_bytes > runtime.prefetch_max_bytes
            ):
                return
            ready_stage.popleft()
            job = self._manifest.jobs[episode_id]
            if job.state is JobState.DISCOVERED:
                self._manifest.transition(episode_id, JobState.STAGING)
            work = StageWork(
                source_root=self._source_root,
                selection=self._selection[episode_id],
                target=self._manifest.run_dir / "staging" / episode_id,
            )
            reserved.add(episode_id)
            try:
                future = executor.submit(self._stage_runner, work)
            except Exception:
                reserved.remove(episode_id)
                raise
            active[future] = ActiveWork(episode_id, WorkPhase.STAGE)
            active_count += 1

    def _episode_bytes(self, episode_id: str) -> int:
        selection = self._selection[episode_id]
        return selection.mcap.size + selection.metadata.size

    def _reserved_bytes(self, reserved: set[str]) -> int:
        return sum(self._episode_bytes(episode_id) for episode_id in reserved)

    def _release_missing_stage(self, episode_id: str, reserved: set[str]) -> None:
        if not (self._manifest.run_dir / "staging" / episode_id).exists():
            reserved.discard(episode_id)

    def _capacity_error(
        self,
        runtime: PrefetchRuntimeConfig,
        ready_stage: deque[str],
        reserved: set[str],
    ) -> str:
        retained = sorted(
            episode_id
            for episode_id in reserved
            if self._manifest.jobs[episode_id].state
            in {JobState.FAILED, JobState.DISCARDED}
        )
        return (
            "staging capacity cannot make progress: "
            f"reserved_bytes={self._reserved_bytes(reserved)}, "
            f"reserved_episodes={len(reserved)}, "
            f"prefetch_max_bytes={runtime.prefetch_max_bytes}, "
            f"prefetch_max_episodes={runtime.prefetch_max_episodes}, "
            f"next_episode={ready_stage[0]}, retained_terminal={retained}"
        )

    def _report_progress(
        self,
        active: dict[Future[object], ActiveWork],
        ready_stage: deque[str],
        ready_convert: deque[StageReceipt],
        reserved: set[str],
    ) -> None:
        if self._progress_reporter is None:
            return
        states: dict[str, int] = {}
        for job in self._manifest.jobs.values():
            states[job.state.value] = states.get(job.state.value, 0) + 1
        self._progress_reporter(
            CoordinatorProgress(
                stage_active=sum(
                    work.phase is WorkPhase.STAGE for work in active.values()
                ),
                stage_ready=len(ready_stage),
                convert_active=sum(
                    work.phase is WorkPhase.CONVERT for work in active.values()
                ),
                convert_ready=len(ready_convert),
                reserved_staging_bytes=self._reserved_bytes(reserved),
                reserved_staging_episodes=len(reserved),
                states=dict(sorted(states.items())),
            )
        )

    def _recover_interrupted(self) -> None:
        for episode_id, job in sorted(self._manifest.jobs.items()):
            if job.state in {JobState.COMMITTED, JobState.EXCLUDED}:
                _remove_stage(self._manifest.run_dir, episode_id)
                continue
            if job.state not in {
                JobState.STAGING,
                JobState.CONVERTING,
                JobState.VALIDATING,
            }:
                continue
            _clean_hidden_partials(self._manifest.run_dir / "staging", episode_id)
            _clean_hidden_partials(self._manifest.run_dir / "fragments", episode_id)
            self._manifest.resume_interrupted(
                episode_id,
                detail="Coordinator resumed an interrupted attempt",
            )

    def _record_conversion(self, work: ActiveWork, value: object) -> None:
        if not isinstance(value, EpisodeConversionResult):
            self._record_error(
                work,
                TypeError("conversion runner returned an invalid result"),
            )
            return
        if value.episode_id != work.episode_id:
            self._record_error(
                work,
                ValueError("conversion runner returned another Episode"),
            )
            return
        if value.status is ConversionStatus.EXCLUDED:
            _remove_stage(self._manifest.run_dir, work.episode_id)
            self._manifest.transition(
                work.episode_id,
                JobState.EXCLUDED,
                reason_code=value.reason_code,
            )
            return
        if value.status is not ConversionStatus.COMMITTED:
            self._record_error(work, ValueError("unsupported conversion status"))
            return
        self._manifest.transition(work.episode_id, JobState.VALIDATING)
        _remove_stage(self._manifest.run_dir, work.episode_id)
        self._manifest.transition(work.episode_id, JobState.COMMITTED)

    def _record_error(self, work: ActiveWork, error: Exception) -> None:
        state, reason = _classify_error(work.phase, error)
        self._manifest.transition(
            work.episode_id,
            state,
            reason_code=reason,
            detail=f"{type(error).__name__}: {error}",
        )


def stage_episode(work: StageWork) -> StageReceipt:
    if work.target.exists():
        receipt = validate_stage(work.target)
        _validate_reused_stage(receipt, work)
        return receipt
    candidate = EpisodeCandidate(
        source_dir=work.selection.source_dir,
        relative_dir=work.selection.relative_dir,
        include_path=work.selection.relative_dir.parent,
        episode_id=work.selection.episode_id,
        source_session_id=work.selection.source_session_id,
        collection_type=work.selection.collection_type,
        outcome=work.selection.outcome,
        task_id=work.selection.metadata_task_id,
        task_description=work.selection.metadata_task_description,
        mcap=work.selection.mcap,
        metadata=work.selection.metadata,
    )
    return MountedEpisodeSource(work.source_root).stage(candidate, work.target)


def convert_staged_episode(work: ConversionWork) -> EpisodeConversionResult:
    if work.target.exists():
        result = load_committed_fragment(work.target)
        if result.episode_id != work.receipt.episode_id:
            raise ValueError("committed Fragment belongs to another Episode")
        return result
    return convert_episode_fragment(
        work.receipt,
        work.target,
        work.recipe,
        work.task,
        work.repo_id,
    )


def spawn_executor(workers: int) -> Executor:
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
    )


def stage_thread_executor(workers: int) -> Executor:
    return ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="arx5-stage",
    )


def _validate_reused_stage(receipt: StageReceipt, work: StageWork) -> None:
    selection = work.selection
    if (
        receipt.episode_id != selection.episode_id
        or receipt.source_session_id != selection.source_session_id
        or receipt.source_dir != selection.source_dir
        or receipt.mcap != selection.mcap
        or receipt.metadata != selection.metadata
    ):
        raise StageValidationError("committed staging does not match frozen selection")


def _classify_error(
    phase: WorkPhase,
    error: Exception,
) -> tuple[JobState, str]:
    if isinstance(error, SourceChangedError):
        return JobState.DISCARDED, "discarded/source_changed_after_confirmation"
    if phase is WorkPhase.STAGE:
        if isinstance(error, StageValidationError):
            return JobState.FAILED, "infrastructure/staging_validation"
        if isinstance(error, OSError):
            return JobState.FAILED, "infrastructure/staging_io"
        return JobState.FAILED, "worker/staging_exception"
    if isinstance(error, OSError):
        return JobState.FAILED, "infrastructure/conversion_io"
    if isinstance(error, ValueError):
        return JobState.DISCARDED, "discarded/episode_data_contract"
    if isinstance(error, RuntimeError):
        return JobState.FAILED, "worker/conversion_runtime"
    return JobState.FAILED, "worker/conversion_exception"


def _fragment_repo_id(repo_id: str, episode_id: str) -> str:
    owner, separator, name = repo_id.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("repo_id must use the '<owner>/<dataset>' form")
    return f"{owner}/{name}__{episode_id}"


def _clean_hidden_partials(parent: Path, episode_id: str) -> None:
    if not parent.is_dir():
        return
    prefix = f".{episode_id}."
    for path in parent.iterdir():
        if not path.name.startswith(prefix):
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def _remove_stage(run_dir: Path, episode_id: str) -> None:
    stage_dir = run_dir / "staging" / episode_id
    if not stage_dir.exists():
        return
    if stage_dir.is_symlink() or not stage_dir.is_dir():
        raise OSError(f"staging path is not a directory: {stage_dir}")
    shutil.rmtree(stage_dir)
