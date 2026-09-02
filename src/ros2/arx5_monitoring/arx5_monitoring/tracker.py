from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StreamSnapshot:
    total_count: int
    window_count: int
    window_duration_s: float
    observed_hz: float
    max_gap_ms: float
    last_message_stamp_ns: int | None
    silence_s: float
    non_monotonic_count: int


class StreamTracker:
    def __init__(self) -> None:
        self.total_count = 0
        self.non_monotonic_count = 0
        self.last_message_stamp_ns: int | None = None
        self.last_arrival_s: float | None = None
        self.window_count = 0
        self.window_gap_count = 0
        self.window_duration_ns = 0
        self.window_max_gap_ns = 0

    def observe(self, message_stamp_ns: int, arrival_s: float) -> None:
        if message_stamp_ns < 0:
            raise ValueError("message_stamp_ns must not be negative")
        if self.last_arrival_s is not None and arrival_s < self.last_arrival_s:
            raise ValueError("arrival_s must be monotonic")

        if self.last_message_stamp_ns is not None:
            gap_ns = message_stamp_ns - self.last_message_stamp_ns
            if gap_ns <= 0:
                self.non_monotonic_count += 1
            else:
                self.window_gap_count += 1
                self.window_duration_ns += gap_ns
                self.window_max_gap_ns = max(self.window_max_gap_ns, gap_ns)

        self.total_count += 1
        self.window_count += 1
        self.last_message_stamp_ns = message_stamp_ns
        self.last_arrival_s = arrival_s

    def snapshot(self, now_s: float, reset_window: bool = True) -> StreamSnapshot:
        if self.last_arrival_s is not None and now_s < self.last_arrival_s:
            raise ValueError("now_s must not precede the last arrival")

        duration_s = self.window_duration_ns / 1e9
        observed_hz = (
            self.window_gap_count / duration_s
            if self.window_gap_count > 0 and duration_s > 0
            else 0.0
        )
        silence_s = (
            now_s - self.last_arrival_s if self.last_arrival_s is not None else 0.0
        )
        result = StreamSnapshot(
            total_count=self.total_count,
            window_count=self.window_count,
            window_duration_s=duration_s,
            observed_hz=observed_hz,
            max_gap_ms=self.window_max_gap_ns / 1e6,
            last_message_stamp_ns=self.last_message_stamp_ns,
            silence_s=silence_s,
            non_monotonic_count=self.non_monotonic_count,
        )
        if reset_window:
            self.window_count = 0
            self.window_gap_count = 0
            self.window_duration_ns = 0
            self.window_max_gap_ns = 0
        return result
