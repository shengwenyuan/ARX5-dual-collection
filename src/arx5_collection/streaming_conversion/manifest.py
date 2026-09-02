from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from arx5_collection.atomic import staged_directory

from .config import BufferedRuntimeConfig
from .config import PrefetchRuntimeConfig
from .config import StreamingConversionConfig
from .models import DiscoveryResult
from .models import FileIdentity
from .models import JobEvent
from .models import JobSnapshot
from .models import JobState
from .models import RunDefinition
from .models import SelectionEntry


RUN_SCHEMA_VERSION = 5
_SUPPORTED_RUN_SCHEMA_VERSIONS = {2, 3, 4, RUN_SCHEMA_VERSION}
SELECTION_SCHEMA_VERSION = 2
JOB_EVENT_SCHEMA_VERSION = 1
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$")
_TERMINAL = {
    JobState.COMMITTED,
    JobState.EXCLUDED,
    JobState.DISCARDED,
    JobState.FAILED,
}
_SKIPPED_ON_RESUME = {
    JobState.COMMITTED,
    JobState.EXCLUDED,
    JobState.DISCARDED,
}
_ALLOWED = {
    JobState.DISCOVERED: {
        JobState.STAGING,
        JobState.EXCLUDED,
        JobState.DISCARDED,
        JobState.FAILED,
    },
    JobState.STAGING: {
        JobState.CONVERTING,
        JobState.EXCLUDED,
        JobState.DISCARDED,
        JobState.FAILED,
    },
    JobState.CONVERTING: {
        JobState.VALIDATING,
        JobState.EXCLUDED,
        JobState.DISCARDED,
        JobState.FAILED,
    },
    JobState.VALIDATING: {
        JobState.COMMITTED,
        JobState.EXCLUDED,
        JobState.DISCARDED,
        JobState.FAILED,
    },
}


class RunManifest:
    def __init__(
        self,
        run_dir: Path,
        run_id: str,
        definition: RunDefinition,
        selection: tuple[SelectionEntry, ...],
        events: list[JobEvent],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.definition = definition
        self.selection = selection
        self._events = events
        self._clock = clock or _utc_now
        self._jobs = _replay_events(selection, events)

    @classmethod
    def create(
        cls,
        config: StreamingConversionConfig,
        discovery: DiscoveryResult,
        output_path: Path,
        run_id: str,
        repo_id: str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> RunManifest:
        run_id = _path_component(run_id, "run_id")
        if not discovery.candidates:
            raise ValueError("cannot create a streaming run with no Episodes")
        if not output_path.is_absolute():
            raise ValueError("streaming output path must be absolute")
        if output_path.exists():
            raise FileExistsError(output_path)
        target = config.runtime.streaming_root / run_id
        now = (clock or _utc_now)()
        recorded_at = _format_utc(now)
        selection = tuple(
            SelectionEntry(
                episode_id=item.episode_id,
                source_session_id=item.source_session_id,
                source_dir=item.source_dir,
                relative_dir=item.relative_dir,
                collection_type=item.collection_type,
                outcome=item.outcome,
                metadata_task_id=item.task_id,
                metadata_task_description=item.task_description,
                training_task=config.recipe.training_task(item.task_description),
                mcap=item.mcap,
                metadata=item.metadata,
            )
            for item in discovery.candidates
        )
        events = [
            JobEvent(
                event_index=index,
                episode_id=item.episode_id,
                previous_state=None,
                state=JobState.DISCOVERED,
                attempt=0,
                recorded_at=recorded_at,
            )
            for index, item in enumerate(selection)
        ]
        run_value = {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": recorded_at,
            "source_root": str(discovery.source_root),
            "source_materialization": config.source.materialization,
            "streaming_root": str(config.runtime.streaming_root),
            "output_path": str(output_path),
            "repo_id": repo_id or config.output.repo_id_for(output_path),
            "runtime": _runtime_value(config.runtime),
            "recipe": {
                "name": config.recipe.name,
                "profile": config.recipe.profile,
                "task": config.recipe.task_identity,
            },
        }
        with staged_directory(target) as temporary:
            _write_json(temporary / "run.json", run_value)
            _write_jsonl(
                temporary / "selection_manifest.jsonl",
                [_selection_value(item) for item in selection],
            )
            _write_jsonl(
                temporary / "jobs.jsonl",
                [_event_value(item) for item in events],
            )
        return cls.open(target, clock=clock)

    @classmethod
    def open(
        cls,
        run_dir: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> RunManifest:
        run_value = _read_json(run_dir / "run.json")
        schema_version = run_value.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in _SUPPORTED_RUN_SCHEMA_VERSIONS
        ):
            raise ValueError("unsupported run manifest schema_version")
        common_keys = {
            "schema_version",
            "run_id",
            "created_at",
            "source_root",
            "streaming_root",
            "output_path",
            "repo_id",
            "recipe",
        }
        if schema_version == RUN_SCHEMA_VERSION:
            common_keys.add("source_materialization")
        _exact_keys(
            run_value,
            common_keys | ({"workers"} if schema_version == 2 else {"runtime"}),
            "run manifest",
        )
        run_id = _path_component(run_value["run_id"], "run_id")
        if run_dir.name != run_id:
            raise ValueError("run directory name does not match run_id")
        selection = tuple(
            _selection_entry(value)
            for value in _read_jsonl(run_dir / "selection_manifest.jsonl")
        )
        if not selection:
            raise ValueError("selection manifest must not be empty")
        events = [_job_event(value) for value in _read_jsonl(run_dir / "jobs.jsonl")]
        return cls(
            run_dir,
            run_id,
            _run_definition(run_value, schema_version),
            selection,
            events,
            clock,
        )

    @property
    def jobs(self) -> dict[str, JobSnapshot]:
        return dict(self._jobs)

    def skipped_episode_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                episode_id
                for episode_id, job in self._jobs.items()
                if job.state in _SKIPPED_ON_RESUME
            )
        )

    def retryable_episode_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                episode_id
                for episode_id, job in self._jobs.items()
                if job.state is JobState.FAILED
            )
        )

    def transition(
        self,
        episode_id: str,
        state: JobState,
        *,
        reason_code: str | None = None,
        detail: str | None = None,
    ) -> JobSnapshot:
        current = self._job(episode_id)
        if current.state in _TERMINAL:
            raise ValueError(f"job {episode_id} is terminal in state={current.state.value}")
        if state not in _ALLOWED[current.state]:
            raise ValueError(
                f"invalid job transition {current.state.value} -> {state.value}"
            )
        _validate_reason(state, reason_code)
        event = JobEvent(
            event_index=len(self._events),
            episode_id=episode_id,
            previous_state=current.state,
            state=state,
            attempt=current.attempt,
            recorded_at=_format_utc(self._clock()),
            reason_code=reason_code,
            detail=detail,
        )
        self._append(event)
        return self._jobs[episode_id]

    def retry_failed(self, episode_id: str, *, detail: str | None = None) -> JobSnapshot:
        current = self._job(episode_id)
        if current.state is not JobState.FAILED:
            raise ValueError(f"job {episode_id} is not failed")
        event = JobEvent(
            event_index=len(self._events),
            episode_id=episode_id,
            previous_state=JobState.FAILED,
            state=JobState.STAGING,
            attempt=current.attempt + 1,
            recorded_at=_format_utc(self._clock()),
            detail=detail,
        )
        self._append(event)
        return self._jobs[episode_id]

    def resume_interrupted(
        self,
        episode_id: str,
        *,
        detail: str | None = None,
    ) -> JobSnapshot:
        current = self._job(episode_id)
        if current.state not in {
            JobState.STAGING,
            JobState.CONVERTING,
            JobState.VALIDATING,
        }:
            raise ValueError(f"job {episode_id} is not interrupted")
        event = JobEvent(
            event_index=len(self._events),
            episode_id=episode_id,
            previous_state=current.state,
            state=JobState.STAGING,
            attempt=current.attempt + 1,
            recorded_at=_format_utc(self._clock()),
            detail=detail,
        )
        self._append(event)
        return self._jobs[episode_id]

    def _job(self, episode_id: str) -> JobSnapshot:
        try:
            return self._jobs[episode_id]
        except KeyError as error:
            raise KeyError(f"unknown Episode job: {episode_id}") from error

    def _append(self, event: JobEvent) -> None:
        path = self.run_dir / "jobs.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps(_event_value(event), sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._events.append(event)
        self._jobs = _replay_events(self.selection, self._events)


def _replay_events(
    selection: tuple[SelectionEntry, ...],
    events: list[JobEvent],
) -> dict[str, JobSnapshot]:
    selected_ids = {item.episode_id for item in selection}
    if len(selected_ids) != len(selection):
        raise ValueError("selection manifest contains duplicate episode_id")
    jobs: dict[str, JobSnapshot] = {}
    for index, event in enumerate(events):
        if event.event_index != index:
            raise ValueError("job event_index must be contiguous")
        if event.episode_id not in selected_ids:
            raise ValueError(f"job event references unselected Episode: {event.episode_id}")
        current = jobs.get(event.episode_id)
        if current is None:
            if (
                event.previous_state is not None
                or event.state is not JobState.DISCOVERED
                or event.attempt != 0
            ):
                raise ValueError("first job event must initialize discovered attempt 0")
        elif event.previous_state is not current.state:
            raise ValueError("job event previous_state does not match replay state")
        elif current.state is JobState.FAILED:
            if event.state is not JobState.STAGING or event.attempt != current.attempt + 1:
                raise ValueError("failed job may only retry into a new staging attempt")
        elif (
            current.state
            in {JobState.STAGING, JobState.CONVERTING, JobState.VALIDATING}
            and event.state is JobState.STAGING
            and event.attempt == current.attempt + 1
        ):
            pass
        else:
            if current.state in _TERMINAL or event.state not in _ALLOWED[current.state]:
                raise ValueError(
                    f"invalid replay transition {current.state.value} -> {event.state.value}"
                )
            if event.attempt != current.attempt:
                raise ValueError("job attempt changed without an explicit retry")
        _validate_reason(event.state, event.reason_code)
        jobs[event.episode_id] = JobSnapshot(
            episode_id=event.episode_id,
            state=event.state,
            attempt=event.attempt,
            event_index=event.event_index,
            reason_code=event.reason_code,
            detail=event.detail,
        )
    if set(jobs) != selected_ids:
        raise ValueError("jobs manifest does not initialize every selected Episode")
    return jobs


def _validate_reason(state: JobState, reason_code: str | None) -> None:
    requires_reason = state in {JobState.EXCLUDED, JobState.DISCARDED, JobState.FAILED}
    if requires_reason:
        if not isinstance(reason_code, str) or not _REASON_CODE.fullmatch(reason_code):
            raise ValueError(f"state={state.value} requires a stable reason_code")
    elif reason_code is not None:
        raise ValueError(f"state={state.value} must not contain reason_code")


def _runtime_value(value: object) -> dict[str, object]:
    if isinstance(value, BufferedRuntimeConfig):
        return {
            "mode": "buffered_prefetch",
            "pfs_root": str(value.pfs_root),
            "stage_workers": value.stage_workers,
            "conversion_workers": value.conversion_workers,
            "ready_low_bytes": value.ready_low_bytes,
            "ready_high_bytes": value.ready_high_bytes,
            "temporary_hard_max_bytes": value.temporary_hard_max_bytes,
            "max_staged_episodes": value.max_staged_episodes,
            "min_free_bytes": value.min_free_bytes,
        }
    if isinstance(value, PrefetchRuntimeConfig):
        return {
            "mode": "bounded_prefetch",
            "pfs_root": str(value.pfs_root),
            "stage_workers": value.stage_workers,
            "conversion_workers": value.conversion_workers,
            "prefetch_target_bytes": value.prefetch_target_bytes,
            "prefetch_max_bytes": value.prefetch_max_bytes,
            "prefetch_max_episodes": value.prefetch_max_episodes,
        }
    workers = getattr(value, "workers", None)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("legacy runtime workers must be positive")
    return {"mode": "legacy_shared_pool", "workers": workers}


def _run_definition(value: dict[str, Any], schema_version: int) -> RunDefinition:
    recipe = value["recipe"]
    _exact_keys(recipe, {"name", "profile", "task"}, "run recipe")
    source_root = Path(_string(value["source_root"], "run source_root"))
    streaming_root = Path(_string(value["streaming_root"], "run streaming_root"))
    output_path = Path(_string(value["output_path"], "run output_path"))
    if not all(path.is_absolute() for path in (source_root, streaming_root, output_path)):
        raise ValueError("run paths must be absolute")
    if schema_version == 2:
        workers = _positive_run_int(value["workers"], "run workers")
        runtime_values: dict[str, object] = {
            "workers": workers,
            "pfs_root": None,
            "stage_workers": None,
            "conversion_workers": None,
            "prefetch_target_bytes": None,
            "prefetch_max_bytes": None,
            "prefetch_max_episodes": None,
            "ready_low_bytes": None,
            "ready_high_bytes": None,
            "temporary_hard_max_bytes": None,
            "max_staged_episodes": None,
            "min_free_bytes": None,
        }
    else:
        runtime_values = _run_runtime(value["runtime"], streaming_root, output_path)
    return RunDefinition(
        run_id=_path_component(value["run_id"], "run_id"),
        source_root=source_root,
        streaming_root=streaming_root,
        output_path=output_path,
        repo_id=_string(value["repo_id"], "run repo_id"),
        workers=runtime_values["workers"],
        pfs_root=runtime_values["pfs_root"],
        stage_workers=runtime_values["stage_workers"],
        conversion_workers=runtime_values["conversion_workers"],
        prefetch_target_bytes=runtime_values["prefetch_target_bytes"],
        prefetch_max_bytes=runtime_values["prefetch_max_bytes"],
        prefetch_max_episodes=runtime_values["prefetch_max_episodes"],
        ready_low_bytes=runtime_values["ready_low_bytes"],
        ready_high_bytes=runtime_values["ready_high_bytes"],
        temporary_hard_max_bytes=runtime_values["temporary_hard_max_bytes"],
        max_staged_episodes=runtime_values["max_staged_episodes"],
        min_free_bytes=runtime_values["min_free_bytes"],
        recipe_name=_string(recipe["name"], "run recipe name"),
        recipe_profile=_string(recipe["profile"], "run recipe profile"),
        recipe_task=_string(recipe["task"], "run recipe task"),
        source_materialization=(
            _run_source_materialization(value["source_materialization"])
            if schema_version == RUN_SCHEMA_VERSION
            else "copy"
        ),
    )


def _run_source_materialization(value: object) -> str:
    materialization = _string(value, "run source_materialization")
    if materialization not in {"copy", "direct"}:
        raise ValueError("run source_materialization must be 'copy' or 'direct'")
    return materialization


def _run_runtime(
    value: object,
    streaming_root: Path,
    output_path: Path,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("run runtime must be an object")
    mode = value.get("mode")
    if mode == "legacy_shared_pool":
        _exact_keys(value, {"mode", "workers"}, "run runtime")
        return {
            "workers": _positive_run_int(value["workers"], "run runtime workers"),
            "pfs_root": None,
            "stage_workers": None,
            "conversion_workers": None,
            "prefetch_target_bytes": None,
            "prefetch_max_bytes": None,
            "prefetch_max_episodes": None,
            "ready_low_bytes": None,
            "ready_high_bytes": None,
            "temporary_hard_max_bytes": None,
            "max_staged_episodes": None,
            "min_free_bytes": None,
        }
    if mode == "buffered_prefetch":
        _exact_keys(
            value,
            {
                "mode",
                "pfs_root",
                "stage_workers",
                "conversion_workers",
                "ready_low_bytes",
                "ready_high_bytes",
                "temporary_hard_max_bytes",
                "max_staged_episodes",
                "min_free_bytes",
            },
            "run runtime",
        )
        common = _run_pfs_runtime(value, streaming_root, output_path)
        low_bytes = _positive_run_int(
            value["ready_low_bytes"], "run runtime ready_low_bytes"
        )
        high_bytes = _positive_run_int(
            value["ready_high_bytes"], "run runtime ready_high_bytes"
        )
        hard_max_bytes = _positive_run_int(
            value["temporary_hard_max_bytes"],
            "run runtime temporary_hard_max_bytes",
        )
        if low_bytes > high_bytes or high_bytes > hard_max_bytes:
            raise ValueError("run buffered prefetch watermarks are invalid")
        return {
            **common,
            "prefetch_target_bytes": None,
            "prefetch_max_bytes": None,
            "prefetch_max_episodes": None,
            "ready_low_bytes": low_bytes,
            "ready_high_bytes": high_bytes,
            "temporary_hard_max_bytes": hard_max_bytes,
            "max_staged_episodes": _positive_run_int(
                value["max_staged_episodes"], "run runtime max_staged_episodes"
            ),
            "min_free_bytes": _non_negative_int(
                value["min_free_bytes"], "run runtime min_free_bytes"
            ),
        }
    if mode != "bounded_prefetch":
        raise ValueError("run runtime mode is unsupported")
    _exact_keys(
        value,
        {
            "mode",
            "pfs_root",
            "stage_workers",
            "conversion_workers",
            "prefetch_target_bytes",
            "prefetch_max_bytes",
            "prefetch_max_episodes",
        },
        "run runtime",
    )
    common = _run_pfs_runtime(value, streaming_root, output_path)
    target_bytes = _positive_run_int(
        value["prefetch_target_bytes"], "run runtime prefetch_target_bytes"
    )
    max_bytes = _positive_run_int(
        value["prefetch_max_bytes"], "run runtime prefetch_max_bytes"
    )
    if target_bytes > max_bytes:
        raise ValueError("run prefetch target must not exceed maximum")
    return {
        **common,
        "prefetch_target_bytes": target_bytes,
        "prefetch_max_bytes": max_bytes,
        "prefetch_max_episodes": _positive_run_int(
            value["prefetch_max_episodes"], "run runtime prefetch_max_episodes"
        ),
        "ready_low_bytes": None,
        "ready_high_bytes": None,
        "temporary_hard_max_bytes": None,
        "max_staged_episodes": None,
        "min_free_bytes": None,
    }


def _run_pfs_runtime(
    value: dict[str, object], streaming_root: Path, output_path: Path
) -> dict[str, object]:
    pfs_root = Path(_string(value["pfs_root"], "run runtime pfs_root"))
    if not pfs_root.is_absolute():
        raise ValueError("run runtime pfs_root must be absolute")
    normalized_pfs = pfs_root.resolve(strict=False)
    if (
        normalized_pfs not in streaming_root.resolve(strict=False).parents
        or normalized_pfs not in output_path.resolve(strict=False).parents
    ):
        raise ValueError("run streaming and output paths must be below pfs_root")
    return {
        "workers": None,
        "pfs_root": pfs_root,
        "stage_workers": _positive_run_int(
            value["stage_workers"], "run runtime stage_workers"
        ),
        "conversion_workers": _positive_run_int(
            value["conversion_workers"], "run runtime conversion_workers"
        ),
    }


def _positive_run_int(value: object, label: str) -> int:
    number = _non_negative_int(value, label)
    if number == 0:
        raise ValueError(f"{label} must be positive")
    return number


def _selection_value(item: SelectionEntry) -> dict[str, object]:
    return {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "episode_id": item.episode_id,
        "source_session_id": item.source_session_id,
        "source_dir": str(item.source_dir),
        "relative_dir": str(item.relative_dir),
        "collection_type": item.collection_type,
        "outcome": item.outcome,
        "metadata_task": {
            "id": item.metadata_task_id,
            "description": item.metadata_task_description,
        },
        "training_task": item.training_task,
        "mcap": {"size": item.mcap.size, "mtime_ns": item.mcap.mtime_ns},
        "metadata": {
            "size": item.metadata.size,
            "mtime_ns": item.metadata.mtime_ns,
        },
    }


def _selection_entry(value: dict[str, Any]) -> SelectionEntry:
    _exact_keys(
        value,
        {
            "schema_version",
            "episode_id",
            "source_session_id",
            "source_dir",
            "relative_dir",
            "collection_type",
            "outcome",
            "metadata_task",
            "training_task",
            "mcap",
            "metadata",
        },
        "selection row",
    )
    if value["schema_version"] != SELECTION_SCHEMA_VERSION:
        raise ValueError("unsupported selection schema_version")
    task = value["metadata_task"]
    _exact_keys(task, {"id", "description"}, "selection metadata_task")
    mcap = _file_identity(value["mcap"], "selection mcap")
    metadata = _file_identity(value["metadata"], "selection metadata")
    return SelectionEntry(
        episode_id=_string(value["episode_id"], "selection episode_id"),
        source_session_id=_string(
            value["source_session_id"], "selection source_session_id"
        ),
        source_dir=Path(_string(value["source_dir"], "selection source_dir")),
        relative_dir=Path(_string(value["relative_dir"], "selection relative_dir")),
        collection_type=_string(value["collection_type"], "selection collection_type"),
        outcome=_string(value["outcome"], "selection outcome"),
        metadata_task_id=_string(task["id"], "selection metadata task id"),
        metadata_task_description=_string(
            task["description"], "selection metadata task description"
        ),
        training_task=_string(value["training_task"], "selection training_task"),
        mcap=mcap,
        metadata=metadata,
    )


def _event_value(event: JobEvent) -> dict[str, object]:
    return {
        "schema_version": JOB_EVENT_SCHEMA_VERSION,
        "event_index": event.event_index,
        "episode_id": event.episode_id,
        "previous_state": (
            event.previous_state.value if event.previous_state is not None else None
        ),
        "state": event.state.value,
        "attempt": event.attempt,
        "recorded_at": event.recorded_at,
        "reason_code": event.reason_code,
        "detail": event.detail,
    }


def _job_event(value: dict[str, Any]) -> JobEvent:
    _exact_keys(
        value,
        {
            "schema_version",
            "event_index",
            "episode_id",
            "previous_state",
            "state",
            "attempt",
            "recorded_at",
            "reason_code",
            "detail",
        },
        "job event",
    )
    if value["schema_version"] != JOB_EVENT_SCHEMA_VERSION:
        raise ValueError("unsupported job event schema_version")
    previous = value["previous_state"]
    reason_code = value["reason_code"]
    detail = value["detail"]
    if reason_code is not None and not isinstance(reason_code, str):
        raise ValueError("job reason_code must be a string or null")
    if detail is not None and not isinstance(detail, str):
        raise ValueError("job detail must be a string or null")
    return JobEvent(
        event_index=_non_negative_int(value["event_index"], "event_index"),
        episode_id=_string(value["episode_id"], "job episode_id"),
        previous_state=JobState(previous) if previous is not None else None,
        state=JobState(value["state"]),
        attempt=_non_negative_int(value["attempt"], "attempt"),
        recorded_at=_string(value["recorded_at"], "recorded_at"),
        reason_code=reason_code,
        detail=detail,
    )


def _file_identity(value: object, label: str) -> FileIdentity:
    _exact_keys(value, {"size", "mtime_ns"}, label)
    assert isinstance(value, dict)
    return FileIdentity(
        _non_negative_int(value["size"], f"{label} size"),
        _non_negative_int(value["mtime_ns"], f"{label} mtime_ns"),
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON must contain an object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values = []
    line_number = 0
    try:
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("row must be an object")
            values.append(value)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"cannot read JSONL {path}:{line_number}: {error}") from error
    return values


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(value, sort_keys=True) + "\n" for value in values))


def _exact_keys(value: object, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} keys must be exactly {sorted(expected)}")


def _path_component(value: object, label: str) -> str:
    text = _string(value, label)
    if text in {".", ".."} or Path(text).name != text:
        raise ValueError(f"{label} must be one path component")
    return text


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("manifest clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
