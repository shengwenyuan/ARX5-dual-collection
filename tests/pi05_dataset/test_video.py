from __future__ import annotations

import unittest

from arx5_collection.pi05_dataset.video import VideoEncodingConfig


class VideoEncodingConfigTest(unittest.TestCase):
    def test_reports_explicit_svt_policy(self) -> None:
        config = VideoEncodingConfig(preset=10, threads=8)

        self.assertEqual(
            config.as_report(),
            {
                "codec": "libsvtav1",
                "pixel_format": "yuv420p",
                "gop": 2,
                "crf": 30,
                "preset": 10,
                "threads": 8,
            },
        )

    def test_rejects_out_of_range_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "preset"):
            VideoEncodingConfig(preset=14)
        with self.assertRaisesRegex(ValueError, "threads"):
            VideoEncodingConfig(threads=-1)


if __name__ == "__main__":
    unittest.main()
