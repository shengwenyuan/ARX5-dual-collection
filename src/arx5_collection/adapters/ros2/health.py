from __future__ import annotations

from dataclasses import dataclass

from arx5_collection.collection.episode.models import StreamSpec
from arx5_collection.collection.environment import ENVIRONMENT


@dataclass(frozen=True, slots=True)
class StatusSample:
    stream_id: str
    topic: str
    total_count: int
    window_count: int
    observed_hz: float
    max_gap_ms: float
    silence_s: float
    non_monotonic_count: int


@dataclass
class _ObservedStream:
    baseline_count: int
    latest: StatusSample
    last_status_s: float


class StreamHealthTracker:
    def __init__(
        self,
        streams: tuple[StreamSpec, ...],
        started_s: float,
        startup_grace_s: float = ENVIRONMENT.monitor.startup_grace_s,
        heartbeat_timeout_s: float = ENVIRONMENT.monitor.heartbeat_timeout_s,
        data_silence_timeout_s: float = ENVIRONMENT.monitor.data_silence_timeout_s,
        warning_ratio: float = ENVIRONMENT.monitor.warning_ratio,
    ) -> None:
        if (
            min(
                startup_grace_s,
                heartbeat_timeout_s,
                data_silence_timeout_s,
            )
            <= 0
        ):
            raise ValueError("stream health timeouts must be positive")
        if not 0 < warning_ratio <= 1:
            raise ValueError("warning_ratio must be in (0, 1]")
        self.streams = streams
        self.by_id = {stream.id: stream for stream in streams}
        self.started_s = started_s
        self.startup_grace_s = startup_grace_s
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.data_silence_timeout_s = data_silence_timeout_s
        self.warning_ratio = warning_ratio
        self.observed: dict[str, _ObservedStream] = {}
        self.warnings: dict[str, list[str]] = {stream.id: [] for stream in streams}
        self.contract_failures: dict[str, str] = {}

    def observe(self, sample: StatusSample, arrival_s: float) -> None:
        stream = self.by_id.get(sample.stream_id)
        if stream is None:
            return
        if sample.topic != stream.topic:
            self.contract_failures[stream.id] = (
                f"stream {stream.id} telemetry topic mismatch: {sample.topic}"
            )
            return

        previous = self.observed.get(stream.id)
        if previous is not None and sample.total_count < previous.latest.total_count:
            self.contract_failures[stream.id] = (
                f"stream {stream.id} telemetry count decreased"
            )
            return
        baseline = sample.total_count if previous is None else previous.baseline_count
        self.observed[stream.id] = _ObservedStream(baseline, sample, arrival_s)

        if (
            sample.window_count > 1
            and sample.observed_hz < stream.expected_hz * self.warning_ratio
        ):
            self._warn(
                stream.id,
                f"live frequency {sample.observed_hz:.3f} Hz is below "
                f"{self.warning_ratio:.0%} of expected {stream.expected_hz:.3f} Hz",
            )
        if sample.non_monotonic_count:
            self._warn(
                stream.id,
                f"live telemetry reports {sample.non_monotonic_count} "
                "non-monotonic Header intervals",
            )

    def required_failure(self, now_s: float) -> str | None:
        for stream in self.streams:
            contract_failure = self.contract_failures.get(stream.id)
            if contract_failure is not None:
                if stream.required:
                    return contract_failure
                self._warn(stream.id, contract_failure)
                continue
            observed = self.observed.get(stream.id)
            if observed is None:
                if now_s - self.started_s >= self.startup_grace_s:
                    reason = f"stream {stream.id} produced no telemetry"
                    self._warn(stream.id, reason)
                continue
            if now_s - observed.last_status_s >= self.heartbeat_timeout_s:
                reason = f"stream {stream.id} telemetry stopped"
                self._warn(stream.id, reason)
                continue
            if observed.latest.silence_s >= self.data_silence_timeout_s:
                reason = f"stream {stream.id} data stopped"
                if stream.required:
                    return f"required {reason}"
                self._warn(stream.id, reason)
        return None

    def display(self) -> str:
        parts = []
        for stream in self.streams:
            observed = self.observed.get(stream.id)
            if observed is None:
                parts.append(f"{stream.id}=waiting")
                continue
            count = observed.latest.total_count - observed.baseline_count
            parts.append(
                f"{stream.id}={count} "
                f"{observed.latest.observed_hz:.1f}Hz "
                f"gap={observed.latest.max_gap_ms:.1f}ms"
            )
        return " | ".join(parts)

    def warnings_for(self, stream_id: str) -> tuple[str, ...]:
        return tuple(self.warnings[stream_id])

    def _warn(self, stream_id: str, warning: str) -> None:
        if warning not in self.warnings[stream_id]:
            self.warnings[stream_id].append(warning)
