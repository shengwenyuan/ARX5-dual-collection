from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from .metadata import build_metadata, load_station, write_metadata
from .models import (
    EpisodeOutcome,
    EpisodeRequest,
    EpisodeResult,
    EpisodeState,
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
        state_sink: Callable[[EpisodeState], None] | None = None,
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
        self.state_sink = state_sink
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
        started_monotonic = self.monotonic_clock()

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
            outcome, errors = self._wait_for_end()
            ended_at = self.wall_clock()
            duration_s = self.monotonic_clock() - started_monotonic
            self.state = EpisodeState.FINALIZING
            if self.state_sink is not None:
                self.state_sink(self.state)

            stream_metrics = self._stop_components()
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
            )
            metadata = build_metadata(
                request,
                pending_result,
                load_station(request.station_config),
                self.software_version,
            )
            write_metadata(pending.metadata_path, metadata)
            stored = self.store.commit(
                pending,
                aborted=outcome is EpisodeOutcome.ABORTED,
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
        while self.trigger.wait(self.poll_interval_s) is not TriggerEvent.ACTIVATE:
            continue

    def _wait_for_end(self) -> tuple[EpisodeOutcome, tuple[str, ...]]:
        try:
            while True:
                failure = self.monitor.required_failure()
                if failure is not None:
                    return EpisodeOutcome.ABORTED, (failure,)
                event = self.trigger.wait(self.poll_interval_s)
                if event is TriggerEvent.ABORT:
                    return EpisodeOutcome.ABORTED, ("operator requested abort",)
                if event is TriggerEvent.ACTIVATE:
                    return EpisodeOutcome.SUCCESS, ()
        except KeyboardInterrupt:
            return EpisodeOutcome.ABORTED, ("recording interrupted",)
        except Exception as error:
            return EpisodeOutcome.ABORTED, (str(error),)

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
