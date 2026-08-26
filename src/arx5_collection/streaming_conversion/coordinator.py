from __future__ import annotations

from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor
from concurrent.futures import FIRST_COMPLETED
from concurrent.futures import Future
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import wait
from dataclasses import dataclass
from enum import Enum
import multiprocessing
from pathlib import Path
import shutil

from .manifest import RunManifest
from .models import ConversionStatus
from .models import EpisodeCandidate
from .models import EpisodeConversionResult
from .models import JobSnapshot
from .models import JobState
from .models import SelectionEntry
from .models import StageReceipt
from .recipe import Pi05ConversionRecipe
from .recipe import UnknownStationCalibrationError
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


StageRunner = Callable[[StageWork], StageReceipt]
ConversionRunner = Callable[[ConversionWork], EpisodeConversionResult]
ExecutorFactory = Callable[[int], Executor]


class StreamingCoordinator:
    def __init__(
        self,
        manifest: RunManifest,
        source_root: Path,
        recipe: Pi05ConversionRecipe,
        task: str,
        repo_id: str,
        workers: int,
        *,
        stage_runner: StageRunner | None = None,
        conversion_runner: ConversionRunner | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError("Coordinator workers must be positive")
        if not task.strip():
            raise ValueError("Coordinator task must not be empty")
        self._manifest = manifest
        self._source_root = source_root
        self._recipe = recipe
        self._task = task
        self._repo_id = repo_id
        self._workers = workers
        self._stage_runner = stage_runner or stage_episode
        self._conversion_runner = conversion_runner or convert_staged_episode
        self._executor_factory = executor_factory or spawn_executor
        self._selection = {item.episode_id: item for item in manifest.selection}

    def run(self) -> dict[str, JobSnapshot]:
        self._recover_interrupted()
        ready_stage = deque(
            sorted(
                episode_id
                for episode_id, job in self._manifest.jobs.items()
                if job.state in {JobState.DISCOVERED, JobState.STAGING}
            )
        )
        ready_convert: deque[StageReceipt] = deque()
        active: dict[Future[object], ActiveWork] = {}

        with self._executor_factory(self._workers) as executor:
            self._fill(executor, active, ready_stage, ready_convert)
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
                self._fill(executor, active, ready_stage, ready_convert)
        return self._manifest.jobs

    def _fill(
        self,
        executor: Executor,
        active: dict[Future[object], ActiveWork],
        ready_stage: deque[str],
        ready_convert: deque[StageReceipt],
    ) -> None:
        while len(active) < self._workers and (ready_convert or ready_stage):
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
    if isinstance(error, UnknownStationCalibrationError):
        return JobState.FAILED, "configuration/unknown_station_calibration"
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
