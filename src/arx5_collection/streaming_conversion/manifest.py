from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from arx5_collection.atomic import staged_directory

from .config import StreamingConversionConfig
from .models import DiscoveryResult
from .models import FileIdentity
from .models import JobEvent
from .models import JobSnapshot
from .models import JobState
from .models import SelectionEntry


RUN_SCHEMA_VERSION = 1
SELECTION_SCHEMA_VERSION = 1
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
        selection: tuple[SelectionEntry, ...],
        events: list[JobEvent],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
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
                source_dir=item.source_dir,
                relative_dir=item.relative_dir,
                collection_type=item.collection_type,
                outcome=item.outcome,
                metadata_task_id=item.task_id,
                metadata_task_description=item.task_description,
                training_task=config.recipe.task,
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
            "streaming_root": str(config.runtime.streaming_root),
            "output_path": str(output_path),
            "workers": config.runtime.workers,
            "recipe": {
                "name": config.recipe.name,
                "profile": config.recipe.profile,
                "task": config.recipe.task,
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
        _exact_keys(
            run_value,
            {
                "schema_version",
                "run_id",
                "created_at",
                "source_root",
                "streaming_root",
                "output_path",
                "workers",
                "recipe",
            },
            "run manifest",
        )
        if run_value["schema_version"] != RUN_SCHEMA_VERSION:
            raise ValueError("unsupported run manifest schema_version")
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
        return cls(run_dir, run_id, selection, events, clock)

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


def _selection_value(item: SelectionEntry) -> dict[str, object]:
    return {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "episode_id": item.episode_id,
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
