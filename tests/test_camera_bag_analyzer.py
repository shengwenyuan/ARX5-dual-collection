from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path


SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts" / "collection"
sys.path.insert(0, str(SCRIPTS_ROOT))

from analyze_camera_bag import TimingStats, image_header_stamp_ns  # noqa: E402


class CameraBagAnalyzerTest(unittest.TestCase):
    def test_reads_little_endian_image_header_stamp(self) -> None:
        payload = b"\x00\x01\x00\x00" + struct.pack("<iI", 123, 456)
        self.assertEqual(image_header_stamp_ns(payload), 123_000_000_456)

    def test_rejects_invalid_cdr_or_nanoseconds(self) -> None:
        with self.assertRaises(ValueError):
            image_header_stamp_ns(b"short")
        with self.assertRaises(ValueError):
            image_header_stamp_ns(b"\x00\x02\x00\x00" + b"\x00" * 8)
        with self.assertRaises(ValueError):
            image_header_stamp_ns(
                b"\x00\x01\x00\x00" + struct.pack("<iI", 1, 1_000_000_000)
            )

    def test_timing_summary(self) -> None:
        stats = TimingStats()
        stats.add(1_000_000_000)
        stats.add(1_033_000_000)
        stats.add(1_067_000_000)
        summary = stats.summary()
        self.assertEqual(summary["count"], 3)
        self.assertAlmostEqual(summary["observed_hz"], 2 / 0.067)
        self.assertEqual(summary["max_gap_ms"], 34.0)


if __name__ == "__main__":
    unittest.main()
