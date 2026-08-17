from __future__ import annotations

import unittest

from arx5_collection.pi05_dataset.images import decode_yuyv


class ImageDecodeTest(unittest.TestCase):
    def test_decodes_bt601_black_and_white_pair(self) -> None:
        rgb = decode_yuyv(bytes((16, 128, 235, 128)), width=2, height=1, step=4)

        self.assertEqual(rgb.shape, (1, 2, 3))
        self.assertTrue((rgb[0, 0] == (0, 0, 0)).all())
        self.assertTrue((rgb[0, 1] == (255, 255, 255)).all())

    def test_rejects_odd_width(self) -> None:
        with self.assertRaises(ValueError):
            decode_yuyv(b"", width=1, height=1, step=2)


if __name__ == "__main__":
    unittest.main()
