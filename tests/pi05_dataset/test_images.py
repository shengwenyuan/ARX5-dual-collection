from __future__ import annotations

import unittest
from types import SimpleNamespace

from arx5_collection.pi05_dataset.images import decode_color_message


class ImageDecodeTest(unittest.TestCase):
    def message(
        self, encoding: str, data: bytes, *, width: int, height: int, step: int
    ) -> SimpleNamespace:
        return SimpleNamespace(
            encoding=encoding,
            data=data,
            width=width,
            height=height,
            step=step,
        )

    def test_reads_rgb8_without_changing_channel_order(self) -> None:
        rgb = decode_color_message(
            self.message(
                "rgb8",
                bytes((255, 0, 0, 0, 0, 255, 99, 99)),
                width=2,
                height=1,
                step=8,
            )
        )

        self.assertEqual(rgb.shape, (1, 2, 3))
        self.assertEqual(rgb.tolist(), [[[255, 0, 0], [0, 0, 255]]])

    def test_decodes_historical_yuyv(self) -> None:
        rgb = decode_color_message(
            self.message(
                "yuv422_yuy2",
                bytes((16, 128, 235, 128)),
                width=2,
                height=1,
                step=4,
            )
        )

        self.assertEqual(rgb.shape, (1, 2, 3))
        self.assertTrue((rgb[0, 0] == (0, 0, 0)).all())
        self.assertTrue((rgb[0, 1] == (255, 255, 255)).all())

    def test_rejects_invalid_historical_yuyv(self) -> None:
        with self.assertRaises(ValueError):
            decode_color_message(
                self.message("yuyv", b"", width=1, height=1, step=2)
            )

    def test_rejects_unknown_encoding(self) -> None:
        with self.assertRaises(ValueError):
            decode_color_message(
                self.message("bgr8", b"\x00\x00\x00", width=1, height=1, step=3)
            )


if __name__ == "__main__":
    unittest.main()
