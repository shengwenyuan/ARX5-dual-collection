from __future__ import annotations

from pathlib import Path
import unittest

from arx5_collection.pi05_dataset.video import VideoEncodingConfig


class VideoEncodingConfigTest(unittest.TestCase):
    def test_dataset_entrypoints_pin_pyav_decoder(self) -> None:
        for path in (
            "src/arx5_collection/pi05_dataset/exporter.py",
            "src/arx5_collection/pi05_dataset/validate.py",
            "scripts/w3/smoke_pi05_policy.py",
        ):
            self.assertIn('video_backend="pyav"', Path(path).read_text())

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
