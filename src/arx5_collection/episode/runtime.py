from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from arx5_collection.collection_metadata import MetadataContext

from .metadata import build_metadata, load_station, write_metadata
from .models import (
    EpisodeOutcome,
    EpisodeRequest,
    EpisodeResult,
    EpisodeState,
    RecordingStarted,
    RecordingStopping,
    StreamMetrics,
)
from .ports import RecordTrigger, RecordingBackend, StreamMonitor, TriggerEvent
from .store import EpisodeStore


class EpisodeRuntime:
    def __init__(
        self,
        store: EpisodeStore,
        trigger: RecordTrigger,
        backend: RecordingBackend,
        monitor: StreamMonitor,
        software_version: str,
        episode_id_factory: Callable[[], str] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        pre_episode_check: Callable[[], None] | None = None,
        runtime_check: Callable[[], None] | None = None,
        state_sink: Callable[[EpisodeState], None] | None = None,
        metadata_context_provider: Callable[[], MetadataContext] | None = None,
        recording_started_hook: Callable[[RecordingStarted], None] | None = None,
        recording_stopping_hook: Callable[[RecordingStopping], None] | None = None,
        poll_interval_s: float = 0.1,
    ) -> None:
        self.store = store
        self.trigger = trigger
        self.backend = backend
        self.monitor = monitor
        self.software_version = software_version
        self.episode_id_factory = episode_id_factory or default_episode_id
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))

        if monotonic_clock is None:
            from time import monotonic

            monotonic_clock = monotonic
        self.monotonic_clock = monotonic_clock
        self.pre_episode_check = pre_episode_check
        self.runtime_check = runtime_check
        self.state_sink = state_sink
        self.metadata_context_provider = metadata_context_provider
        self.recording_started_hook = recording_started_hook
        self.recording_stopping_hook = recording_stopping_hook
        self.poll_interval_s = poll_interval_s
        self.state = EpisodeState.READY

    def run_once(self, request: EpisodeRequest) -> EpisodeResult:
        if self.state is not EpisodeState.READY:
            raise RuntimeError("an episode is already active")

        self._wait_for_start()
        if self.pre_episode_check is not None:
            self.pre_episode_check()
        pending = self.store.prepare(self.episode_id_factory())
        started_at = self.wall_clock()
        started_monotonic_ns = round(self.monotonic_clock() * 1e9)

        try:
            self.backend.start(pending.mcap_path, request.streams)
            try:
                self.monitor.start(request.streams)
            except BaseException:
                self.backend.stop()
                raise

            self.state = EpisodeState.RECORDING
            if self.state_sink is not None:
                self.state_sink(self.state)
            if self.recording_started_hook is not None:
                try:
                    self.recording_started_hook(
                        RecordingStarted(pending.episode_id, started_monotonic_ns)
                    )
                except BaseException:
                    self._stop_components()
                    raise
            outcome, errors, stopping_monotonic_ns, session_blocked = (
                self._wait_for_end()
            )
            ended_at = self.wall_clock()
            duration_s = max(
                0.0,
                (stopping_monotonic_ns - started_monotonic_ns) / 1e9,
            )
            self.state = EpisodeState.FINALIZING
            if self.state_sink is not None:
                self.state_sink(self.state)

            hook_error: BaseException | None = None
            if self.recording_stopping_hook is not None:
                try:
                    self.recording_stopping_hook(
                        RecordingStopping(outcome, stopping_monotonic_ns)
                    )
                except BaseException as error:
                    hook_error = error
            try:
                stream_metrics = self._stop_components()
            except BaseException as stop_error:
                if hook_error is not None:
                    raise hook_error from stop_error
                raise
            if hook_error is not None:
                raise hook_error
            pending_result = EpisodeResult(
                episode_id=pending.episode_id,
                outcome=outcome,
                started_at=started_at,
                ended_at=ended_at,
                duration_s=duration_s,
                committed=False,
                mcap_path=pending.mcap_path,
                metadata_path=pending.metadata_path,
                stream_metrics=stream_metrics,
                errors=errors,
                session_blocked=session_blocked,
            )
            metadata = build_metadata(
                request,
                pending_result,
                load_station(request.station_config),
                self.software_version,
                metadata_context=(
                    self.metadata_context_provider()
                    if self.metadata_context_provider is not None
                    else None
                ),
            )
            write_metadata(pending.metadata_path, metadata)
            stored = self.store.commit(
                pending,
                outcome=outcome,
            )
            return replace(
                pending_result,
                committed=True,
                mcap_path=stored.mcap_path,
                metadata_path=stored.metadata_path,
            )
        finally:
            self.state = EpisodeState.READY

    def _wait_for_start(self) -> None:
        while True:
            signal = self.trigger.wait(self.poll_interval_s)
            if signal is not None and signal.event is TriggerEvent.ACTIVATE:
                return

    def _wait_for_end(
        self,
    ) -> tuple[EpisodeOutcome, tuple[str, ...], int, bool]:
        try:
            while True:
                if self.runtime_check is not None:
                    self.runtime_check()
                failure = self.monitor.required_failure()
                if failure is not None:
                    return EpisodeOutcome.FAIL, (failure,), self._monotonic_ns(), True
                signal = self.trigger.wait(self.poll_interval_s)
                if signal is None:
                    continue
                if signal.event is TriggerEvent.ABORT:
                    return (
                        EpisodeOutcome.ABORTED,
                        ("operator requested abort",),
                        signal.monotonic_time_ns,
                        False,
                    )
                if signal.event is TriggerEvent.ACTIVATE:
                    return (
                        EpisodeOutcome.SUCCESS,
                        (),
                        signal.monotonic_time_ns,
                        False,
                    )
                if signal.event is TriggerEvent.FAIL:
                    assert signal.detail is not None
                    return (
                        EpisodeOutcome.FAIL,
                        (signal.detail,),
                        signal.monotonic_time_ns,
                        False,
                    )
        except KeyboardInterrupt:
            return (
                EpisodeOutcome.ABORTED,
                ("recording interrupted",),
                self._monotonic_ns(),
                False,
            )
        except Exception as error:
            return EpisodeOutcome.FAIL, (str(error),), self._monotonic_ns(), True

    def _monotonic_ns(self) -> int:
        return round(self.monotonic_clock() * 1e9)

    def _stop_components(self) -> tuple[StreamMetrics, ...]:
        backend_error: Exception | None = None
        try:
            self.backend.stop()
        except Exception as error:
            backend_error = error

        try:
            metrics = self.monitor.stop()
        except Exception as monitor_error:
            if backend_error is not None:
                raise backend_error from monitor_error
            raise

        if backend_error is not None:
            raise backend_error
        return metrics


def default_episode_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{uuid4().hex[:8]}"
