from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


class EpisodeState(str, Enum):
    READY = "ready"
    RECORDING = "recording"
    FINALIZING = "finalizing"


class EpisodeOutcome(str, Enum):
    SUCCESS = "success"
    FAIL = "fail"
    ABORTED = "aborted"


class EpisodeBlocked(RuntimeError):
    """A recoverable pre-Episode failure with confirmed safe arm state."""

    def __init__(self, reason: str, safety: str) -> None:
        self.reason = reason
        self.safety = safety
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class RecordingStarted:
    episode_id: str
    monotonic_time_ns: int

    def __post_init__(self) -> None:
        if not self.episode_id:
            raise ValueError("episode_id must not be empty")
        if self.monotonic_time_ns < 0:
            raise ValueError("monotonic_time_ns must not be negative")


@dataclass(frozen=True, slots=True)
class RecordingStopping:
    outcome: EpisodeOutcome
    monotonic_time_ns: int

    def __post_init__(self) -> None:
        if self.monotonic_time_ns < 0:
            raise ValueError("monotonic_time_ns must not be negative")


@dataclass(frozen=True, slots=True)
class StreamSpec:
    id: str
    topic: str
    required: bool
    expected_hz: float

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("stream id must not be empty")
        if not self.topic:
            raise ValueError("stream topic must not be empty")
        if self.expected_hz <= 0:
            raise ValueError("expected_hz must be greater than zero")


@dataclass(frozen=True, slots=True)
class EpisodeRequest:
    task_id: str
    task_description: str
    output_root: Path
    station_config: Path
    streams: tuple[StreamSpec, ...]

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id must not be empty")
        if not self.task_description:
            raise ValueError("task_description must not be empty")

        stream_ids = [stream.id for stream in self.streams]
        if len(stream_ids) != len(set(stream_ids)):
            raise ValueError("stream ids must be unique")

        topics = [stream.topic for stream in self.streams]
        if len(topics) != len(set(topics)):
            raise ValueError("stream topics must be unique")


@dataclass(frozen=True, slots=True)
class StreamMetrics:
    id: str
    count: int
    duration_s: float
    observed_hz: float
    max_gap_ms: float
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("stream metric id must not be empty")


@dataclass(frozen=True, slots=True)
class EpisodeResult:
    episode_id: str
    outcome: EpisodeOutcome
    started_at: datetime
    ended_at: datetime
    duration_s: float
    committed: bool
    mcap_path: Path
    metadata_path: Path
    stream_metrics: tuple[StreamMetrics, ...] = ()
    errors: tuple[str, ...] = ()
    session_blocked: bool = False

    def __post_init__(self) -> None:
        if not self.episode_id:
            raise ValueError("episode_id must not be empty")
