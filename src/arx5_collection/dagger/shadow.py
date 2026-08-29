from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, time_ns
from uuid import uuid4

from arx5_collection.collection_metadata import (
    DaggerMetadata,
    MetadataContext,
    ShadowMetadata,
    ShadowQuality,
)
from arx5_collection.episode.models import RecordingStarted, RecordingStopping
from arx5_collection.episode.ports import TriggerEvent, TriggerSignal

from .models import DaggerTriggerEvent, InferenceTiming, ShadowFailureCode
from .observation import ObservationUnavailableError
from .ports import AsyncPolicyClient, DaggerTrigger


def _failure_code(error: BaseException) -> str:
    if isinstance(error, ObservationUnavailableError):
        return error.code.value
    if isinstance(error, TimeoutError):
        return ShadowFailureCode.POLICY_TIMEOUT.value
    identity = f"{type(error).__module__}.{type(error).__name__} {error}".lower()
    if isinstance(error, (ConnectionError, OSError)) or any(
        marker in identity
        for marker in ("websocket", "connection closed", "connection refused")
    ):
        return ShadowFailureCode.POLICY_TRANSPORT_ERROR.value
    return ShadowFailureCode.POLICY_ERROR.value


@dataclass(frozen=True, slots=True)
class ShadowAttempt:
    episode_id: str
    inference_id: str
    control_epoch: int
    status: str
    started_at_ns: int
    completed_at_ns: int
    detail: str = ""
    observed_ns: int | None = None
    limit_ns: int | None = None
    snapshot_ms: float | None = None
    encode_ms: float | None = None
    policy_round_trip_ms: float | None = None
    server_inference_ms: float | None = None
    client_total_ms: float | None = None


class JsonlShadowLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream = None
        self._lock = Lock()

    def __enter__(self) -> JsonlShadowLog:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8", buffering=1)
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __call__(self, attempt: ShadowAttempt) -> None:
        if self._stream is None:
            raise RuntimeError("Shadow JSONL log is not open")
        with self._lock:
            self._stream.write(
                json.dumps(asdict(attempt), ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None


class ShadowInferenceLoop:
    """Schedule one asynchronous inference at a time without control authority."""

    def __init__(
        self,
        policy: AsyncPolicyClient,
        period_s: float,
        monotonic_clock: Callable[[], float] = monotonic,
        wall_clock_ns: Callable[[], int] = time_ns,
        inference_id_factory: Callable[[], str] | None = None,
        attempt_sink: Callable[[ShadowAttempt], None] | None = None,
        status_sink: Callable[[str], None] | None = None,
    ) -> None:
        if period_s <= 0:
            raise ValueError("shadow inference period must be positive")
        self.policy = policy
        self.period_s = period_s
        self.monotonic_clock = monotonic_clock
        self.wall_clock_ns = wall_clock_ns
        self.inference_id_factory = inference_id_factory or (lambda: uuid4().hex)
        self.attempt_sink = attempt_sink or (lambda attempt: None)
        self.status_sink = status_sink or (lambda message: None)
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._episode_id: str | None = None
        self._attempt_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._recovery_count = 0
        self._degraded = False

    @property
    def summary(self) -> ShadowMetadata:
        with self._lock:
            if self._success_count == 0:
                quality = ShadowQuality.INVALID
            elif self._failure_count:
                quality = ShadowQuality.DEGRADED
            else:
                quality = ShadowQuality.HEALTHY
            return ShadowMetadata(
                quality=quality,
                inference_attempt_count=self._attempt_count,
                inference_success_count=self._success_count,
                inference_failure_count=self._failure_count,
                recovery_count=self._recovery_count,
            )

    def start(self, episode_id: str) -> None:
        if self._thread is not None:
            raise RuntimeError("shadow inference loop is already active")
        if not episode_id:
            raise ValueError("episode_id must not be empty")
        self.policy.begin_epoch(0)
        self._episode_id = episode_id
        with self._lock:
            self._attempt_count = 0
            self._success_count = 0
            self._failure_count = 0
            self._recovery_count = 0
            self._degraded = False
        self._stop.clear()
        self._thread = Thread(target=self._run, name="dagger-shadow-scheduler")
        self._thread.start()

    def stop(self, timeout_s: float = 35.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout_s)
        if thread.is_alive():
            raise TimeoutError("shadow inference loop did not stop")
        self._thread = None
        self._episode_id = None

    def _run(self) -> None:
        assert self._episode_id is not None
        while not self._stop.is_set():
            started_s = self.monotonic_clock()
            started_at_ns = self.wall_clock_ns()
            inference_id = self.inference_id_factory()
            future = self.policy.submit(self._episode_id, 0, inference_id)
            while not future.done():
                self._stop.wait(0.02)
            completed_at_ns = self.wall_clock_ns()
            try:
                ticket = future.result()
            except BaseException as error:
                code = _failure_code(error)
                self._record_failure(
                    ShadowAttempt(
                        episode_id=self._episode_id,
                        inference_id=inference_id,
                        control_epoch=0,
                        status=code,
                        started_at_ns=started_at_ns,
                        completed_at_ns=completed_at_ns,
                        detail=str(error),
                        observed_ns=(
                            error.observed_ns
                            if isinstance(error, ObservationUnavailableError)
                            else None
                        ),
                        limit_ns=(
                            error.limit_ns
                            if isinstance(error, ObservationUnavailableError)
                            else None
                        ),
                    )
                )
            else:
                self._record_success(
                    self._episode_id,
                    inference_id,
                    started_at_ns,
                    completed_at_ns,
                    ticket.timing,
                )
            remaining_s = self.period_s - (self.monotonic_clock() - started_s)
            self._stop.wait(max(0.0, remaining_s))

    def _record_failure(self, attempt: ShadowAttempt) -> None:
        with self._lock:
            first_failure = not self._degraded
            self._attempt_count += 1
            self._failure_count += 1
            self._degraded = True
        self._write_attempt(attempt)
        if first_failure:
            self.status_sink(
                f"DAgger Shadow DEGRADED: {attempt.status}: {attempt.detail}"
            )

    def _record_success(
        self,
        episode_id: str,
        inference_id: str,
        started_at_ns: int,
        completed_at_ns: int,
        timing: InferenceTiming | None,
    ) -> None:
        with self._lock:
            recovered = self._degraded
            self._attempt_count += 1
            self._success_count += 1
            if recovered:
                self._recovery_count += 1
            self._degraded = False
        self._write_attempt(
            ShadowAttempt(
                episode_id=episode_id,
                inference_id=inference_id,
                control_epoch=0,
                status="recovered" if recovered else "success",
                started_at_ns=started_at_ns,
                completed_at_ns=completed_at_ns,
                snapshot_ms=None if timing is None else timing.snapshot_ms,
                encode_ms=None if timing is None else timing.encode_ms,
                policy_round_trip_ms=(
                    None if timing is None else timing.policy_round_trip_ms
                ),
                server_inference_ms=(
                    None if timing is None else timing.server_inference_ms
                ),
                client_total_ms=None if timing is None else timing.total_ms,
            )
        )
        if recovered:
            self.status_sink("DAgger Shadow RECOVERED")

    def _write_attempt(self, attempt: ShadowAttempt) -> None:
        try:
            self.attempt_sink(attempt)
        except BaseException as error:
            self.status_sink(f"DAgger Shadow JSONL write failed: {error}")


class ShadowRecordTrigger:
    def __init__(
        self,
        trigger: DaggerTrigger,
        status_sink: Callable[[str], None] | None = None,
    ) -> None:
        self.trigger = trigger
        self.status_sink = status_sink or (lambda message: None)

    def arm(self) -> None:
        self.trigger.arm()

    def disarm(self) -> None:
        self.trigger.disarm()

    def wait(self, timeout_s: float) -> TriggerSignal | None:
        signal = self.trigger.wait(timeout_s)
        if signal is None:
            return None
        if signal.event is DaggerTriggerEvent.RECORD_TOGGLE:
            return TriggerSignal(TriggerEvent.ACTIVATE, signal.monotonic_time_ns)
        if signal.event is DaggerTriggerEvent.ABORT:
            return TriggerSignal(TriggerEvent.ABORT, signal.monotonic_time_ns)
        if signal.event is DaggerTriggerEvent.OWNERSHIP_TOGGLE:
            self.status_sink("DAgger Shadow has no control ownership; event ignored")
        return None


class ShadowEpisodeHooks:
    def __init__(self, shadow: ShadowInferenceLoop, checkpoint_sha256: str) -> None:
        self.shadow = shadow
        self.checkpoint_sha256 = checkpoint_sha256

    def recording_started(self, started: RecordingStarted) -> None:
        self.shadow.start(started.episode_id)

    def recording_stopping(self, stopping: RecordingStopping) -> None:
        del stopping
        self.shadow.stop()

    def metadata_context(self) -> MetadataContext:
        return MetadataContext.for_dagger(
            DaggerMetadata(
                checkpoint_sha256=self.checkpoint_sha256,
                intervention_count=0,
                control_segments=(),
                shadow=self.shadow.summary,
            )
        )
