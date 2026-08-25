from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import textwrap
import unittest

from arx5_collection.streaming_conversion.config import StreamingConversionConfig


class StreamingConfigTest(unittest.TestCase):
    def test_loads_platform_independent_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(_profile())

            config = StreamingConversionConfig.load(path)

        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.source.root, Path("/mnt/bos/datainfra-demo"))
        self.assertEqual(
            config.source.include_paths,
            (Path("fold_cloth/2026-08-21"), Path("fold_cloth/2026-08-22")),
        )
        self.assertEqual(config.source.block, ("aborted", "logs"))
        self.assertEqual(config.runtime.workers, 20)
        self.assertEqual(config.recipe.task, "folding the cloth")
        self.assertEqual(
            config.output.dated_path(date(2026, 8, 25)),
            Path("/mnt/pfs/swy/dataset/lerobot/fold_cloth_2026-08-25"),
        )

    def test_rejects_non_positive_worker_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(_profile().replace("workers = 20", "workers = 0"))

            with self.assertRaisesRegex(ValueError, "positive integer"):
                StreamingConversionConfig.load(path)

    def test_rejects_absolute_include_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(
                _profile().replace(
                    '"fold_cloth/2026-08-21",',
                    '"/fold_cloth/2026-08-21",',
                )
            )

            with self.assertRaisesRegex(ValueError, "normalized relative paths"):
                StreamingConversionConfig.load(path)

    def test_rejects_block_path_instead_of_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "streaming.toml"
            path.write_text(_profile().replace('block = ["aborted", "logs"]', 'block = ["bad/logs"]'))

            with self.assertRaisesRegex(ValueError, "one path component"):
                StreamingConversionConfig.load(path)


def _profile() -> str:
    return textwrap.dedent(
        """
        schema_version = 1

        [source]
        root = "/mnt/bos/datainfra-demo"
        include_paths = [
          "fold_cloth/2026-08-21",
          "fold_cloth/2026-08-22",
        ]
        block = ["aborted", "logs"]

        [runtime]
        streaming_root = "/mnt/pfs/swy/dataset/streaming"
        workers = 20

        [output]
        lerobot_root = "/mnt/pfs/swy/dataset/lerobot"
        dataset_name = "fold_cloth"
        repo_id = "local/fold_cloth"

        [recipe]
        name = "pi05-equal-eef-v2"
        profile = "config/fold_cloth.toml"
        task = "folding the cloth"
        """
    )


if __name__ == "__main__":
    unittest.main()
