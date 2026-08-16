from __future__ import annotations

import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).parents[1] / "ros2_ws/src/arx5_monitoring"
sys.path.insert(0, str(PACKAGE_ROOT))

from arx5_monitoring.tracker import StreamTracker  # noqa: E402


def test_empty_snapshot() -> None:
    snapshot = StreamTracker().snapshot(10.0)
    assert snapshot.total_count == 0
    assert snapshot.window_count == 0
    assert snapshot.observed_hz == 0.0
    assert snapshot.last_message_stamp_ns is None


def test_frequency_and_max_gap_use_message_headers() -> None:
    tracker = StreamTracker()
    tracker.observe(1_000_000_000, 1.0)
    tracker.observe(1_033_000_000, 1.033)
    tracker.observe(1_067_000_000, 1.067)

    snapshot = tracker.snapshot(1.1)

    assert snapshot.total_count == 3
    assert snapshot.window_count == 3
    assert snapshot.window_duration_s == pytest.approx(0.067)
    assert snapshot.observed_hz == pytest.approx(2 / 0.067)
    assert snapshot.max_gap_ms == 34.0
    assert snapshot.silence_s == pytest.approx(0.033)


def test_window_resets_but_total_count_persists() -> None:
    tracker = StreamTracker()
    tracker.observe(1_000_000_000, 1.0)
    tracker.observe(1_010_000_000, 1.01)
    tracker.snapshot(1.02)
    tracker.observe(1_020_000_000, 1.02)

    snapshot = tracker.snapshot(1.03)

    assert snapshot.total_count == 3
    assert snapshot.window_count == 1
    assert snapshot.observed_hz == 100.0
    assert snapshot.max_gap_ms == 10.0


def test_non_monotonic_header_is_reported_without_dropping_count() -> None:
    tracker = StreamTracker()
    tracker.observe(2_000_000_000, 1.0)
    tracker.observe(1_000_000_000, 1.1)

    snapshot = tracker.snapshot(1.2)

    assert snapshot.total_count == 2
    assert snapshot.non_monotonic_count == 1
    assert snapshot.observed_hz == 0.0


def test_arrival_clock_must_be_monotonic() -> None:
    tracker = StreamTracker()
    tracker.observe(1, 2.0)
    with pytest.raises(ValueError, match="arrival_s"):
        tracker.observe(2, 1.0)
