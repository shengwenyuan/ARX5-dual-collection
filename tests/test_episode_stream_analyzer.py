from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest


TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

from analyze_episode_streams import (  # noqa: E402
    ARM_STREAMS,
    CAMERA_STREAMS,
    TelemetryStats,
    TimingStats,
    rgbd_pairing,
    serialized_header_stamp_ns,
)


def test_profiles_freeze_eight_logical_streams() -> None:
    streams = CAMERA_STREAMS + ARM_STREAMS
    assert len(streams) == 8
    assert len({stream.id for stream in streams}) == 8
    assert len({stream.topic for stream in streams}) == 8


def test_serialized_header_stamp_supports_cdr_endianness() -> None:
    little = b"\x00\x01\x00\x00" + struct.pack("<iI", 123, 456)
    big = b"\x00\x00\x00\x00" + struct.pack(">iI", 123, 456)
    assert serialized_header_stamp_ns(little) == 123_000_000_456
    assert serialized_header_stamp_ns(big) == 123_000_000_456


def test_timing_stats_warns_without_failing_low_frequency() -> None:
    stats = TimingStats()
    stats.add(1_000_000_000)
    stats.add(1_100_000_000)
    stats.add(1_200_000_000)

    summary = stats.summary(expected_hz=30.0, warning_ratio=0.9)

    assert summary["count"] == 3
    assert summary["observed_hz"] == pytest.approx(10.0)
    assert summary["max_gap_ms"] == 100.0
    assert len(summary["warnings"]) == 1


def test_timing_stats_reports_non_monotonic_headers() -> None:
    stats = TimingStats()
    stats.add(2_000_000_000)
    stats.add(1_000_000_000)
    summary = stats.summary(expected_hz=1.0, warning_ratio=0.9)
    assert summary["count"] == 2
    assert summary["non_monotonic_count"] == 1
    assert any("non-monotonic" in warning for warning in summary["warnings"])


def test_telemetry_total_count_is_monotonic() -> None:
    stats = TelemetryStats("/embodiments/left_arm/state")
    stats.add("/embodiments/left_arm/state", 100, 100, 1000.0, 1.1, 0.0, 0)
    stats.add("/embodiments/left_arm/state", 1100, 1000, 1000.0, 1.2, 0.0, 0)
    summary = stats.summary()
    assert summary["updates"] == 2
    assert summary["last_total_count"] == 1100
    assert summary["min_observed_hz"] == 1000.0
    with pytest.raises(RuntimeError, match="decreased"):
        stats.add("/embodiments/left_arm/state", 10, 1, 1.0, 1.0, 0.0, 0)


def test_rgbd_pairing_reports_boundary_orphans_without_fabrication() -> None:
    result = rgbd_pairing(
        {
            "camera_left_color": {1, 2},
            "camera_left_aligned_depth": {1, 2, 3},
            "camera_right_color": {4},
            "camera_right_aligned_depth": {4},
        }
    )
    assert result["left"] == {
        "paired_count": 2,
        "color_only_count": 0,
        "depth_only_count": 1,
    }
    assert result["right"]["paired_count"] == 1
    assert result["overview"]["paired_count"] == 0
